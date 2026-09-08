"""The orchestrator: meeting audio in, attention decisions, spoken answers and chat out."""
from __future__ import annotations

import asyncio
import difflib
import hashlib
import logging
import time
from typing import Optional

from .attendee import Attendee
from .attention import Attention, make_anthropic_classifier, make_openai_classifier
from .audio import rms
from .config import Settings
from .deep import DeepAgent
from .fast import make_fast_responder
from .prompts import BRIDGE_PHRASES, ERROR_LINE, NO_BRIEFING, NO_DEEP_LINE, fast_system, greeting
from .speaker import Speaker
from .stt import STT
from .text import norm_text, speech_clean, take_sentences
from .transcript import Transcript
from .tts import TTS

log = logging.getLogger("roger.bridge")

BRIEFING_MAX_AGE_S = 6 * 3600


class Bridge:
    def __init__(self, s: Settings) -> None:
        self.s = s
        self.tts = TTS(s)
        self.speaker = Speaker(self.tts, s.audio_out)
        self.attendee = Attendee(s)
        self.fast = make_fast_responder(s)
        self.transcript = Transcript()
        self.stt = STT(s, self.on_partial, self.on_committed)
        self.briefing = NO_BRIEFING
        self.deep: Optional[DeepAgent] = None
        self.bridge_clips: list[bytes] = []
        self.current_answer: Optional[asyncio.Task] = None
        self.greeted = False
        self.loud_since: Optional[float] = None
        self.bot_state: Optional[str] = None
        self.attention = Attention(s.bot_first_name, s.wake_words, classify=self._make_classifier(), window_s=s.attention_window_s)
        self.speaker.on_said = self.attention.note_bot_turn
        # speaker attribution from per-participant audio energy
        self.energy: dict[str, list[tuple[float, float]]] = {}  # participant uuid -> [(time, rms)]
        self.names: dict[str, str] = {}
        self.names_refreshed = 0.0
        self.mixed_seen = False
        self.page_mic_seen = False
        self.last_ws_mixed_at = 0.0

    def _make_classifier(self):
        try:
            if self.s.fast_provider == "openai" and self.s.openai_api_key:
                return make_openai_classifier(self.s.openai_api_key, self.s.classifier_model)
            if self.s.anthropic_api_key:
                return make_anthropic_classifier(self.s.anthropic_api_key, self.s.classifier_model)
        except Exception as e:
            log.warning("attention classifier unavailable (%s); rule-based follow-ups only", e)
        return None

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self.s.state_dir.mkdir(parents=True, exist_ok=True)
        self.speaker.start()
        self.greeted = True  # only greet after a fresh join(), never after a restart
        try:
            await self.attendee.adopt_existing()
            if self.attendee.bot_id:
                asyncio.create_task(self._watch_state())
        except Exception as e:
            log.warning("attendee: adopt failed: %s", e)
        if self.s.elevenlabs_api_key:
            asyncio.create_task(self._prerender_clips())
        if self.s.deep_enabled:
            asyncio.create_task(self._start_deep())

    async def close(self) -> None:
        if self.deep:
            await self.deep.close()
        await self.stt.close()
        await self.tts.close()
        await self.attendee.close()

    async def _prerender_clips(self) -> None:
        try:
            for p in BRIDGE_PHRASES:
                self.bridge_clips.append(await self.tts.render(p))
            log.info("tts: %d bridge clips pre-rendered", len(self.bridge_clips))
        except Exception as e:
            log.warning("tts: could not pre-render bridge clips: %s", e)

    def _briefing_cache(self):
        key = (self.s.claude_session_id or "")[:8] or "repo-" + hashlib.md5(self.s.project_dir.encode()).hexdigest()[:8]
        return self.s.state_dir / f"briefing-{key}.txt"

    async def _start_deep(self) -> None:
        try:
            self.deep = DeepAgent(self.s, self.speaker, self.attendee)
            await self.deep.start()
            cache = self._briefing_cache()
            if cache.exists() and time.time() - cache.stat().st_mtime < BRIEFING_MAX_AGE_S and cache.stat().st_size > 500:
                self.briefing = cache.read_text()
                log.info("deep: using cached briefing (%d chars, %s)", len(self.briefing), cache.name)
            else:
                self.briefing = await self.deep.briefing()
                cache.write_text(self.briefing)
        except Exception as e:
            log.warning("deep: not available: %s", e)
            self.deep = None

    # ------------------------------------------------------------------ audio in
    async def on_audio(self, pcm: bytes, from_mixed: bool = True) -> None:
        """Mixed meeting audio from the Attendee WebSocket."""
        if from_mixed:
            self.mixed_seen = True
            self.last_ws_mixed_at = time.time()
        self.stt.start()
        await self.stt.feed(pcm)

    async def on_participant_audio(self, uuid: str, pcm: bytes) -> None:
        """One participant's audio: used for speaker attribution (and as the transcription source if there is no mixed stream)."""
        now = time.time()
        lst = self.energy.setdefault(uuid, [])
        lst.append((now, rms(pcm)))
        if len(lst) > 400:
            del lst[: len(lst) - 400]
        if uuid not in self.names and now - self.names_refreshed > 5:
            asyncio.create_task(self.refresh_names())
        if not self.mixed_seen:
            await self.on_audio(pcm, from_mixed=False)

    async def on_page_mic(self, pcm: bytes) -> None:
        """Meeting audio captured by the orb page's microphone. Ignored while the WebSocket mixed stream is alive."""
        if time.time() - self.last_ws_mixed_at < 2.0:
            return
        if not self.page_mic_seen:
            self.page_mic_seen = True
            log.info("orb: using the page's microphone feed for transcription")
        self.stt.start()
        await self.stt.feed(pcm)
        if self.s.barge_in_energy and self.speaker.speaking:
            if rms(pcm) > 900:
                self.loud_since = self.loud_since or time.time()
                if time.time() - self.loud_since > 0.3:
                    await self.speaker.stop("someone is talking (energy)")
                    self.loud_since = None
            else:
                self.loud_since = None

    async def refresh_names(self) -> None:
        self.names_refreshed = time.time()
        try:
            found = await self.attendee.participants()
            if found:
                self.names.update(found)
        except Exception as e:
            log.debug("participants: %s", e)

    def who_spoke(self, approx_duration_s: float) -> str:
        """Which participant's stream carried the most energy over the utterance just committed."""
        if not self.energy:
            return "Someone"
        end = time.time() - 0.4  # transcription and commit lag
        start = end - max(1.0, min(approx_duration_s, 20.0))
        best, best_e = None, 0.0
        for uuid, lst in self.energy.items():
            name = self.names.get(uuid, "")
            if name and name.strip().lower() == self.s.bot_name.strip().lower():
                continue
            vals = [e for (t, e) in lst if start <= t <= end]
            score = sum(vals) / len(vals) if vals else 0.0
            if score > best_e:
                best, best_e = uuid, score
        if best is None or best_e < 150:
            return "Someone"
        return self.names.get(best) or f"participant {best.rsplit('/', 1)[-1]}"

    # ------------------------------------------------------------------ transcript events
    def is_echo(self, text: str) -> bool:
        """Is this transcript our own voice coming back through the meeting's mixed audio?"""
        n = norm_text(text)
        if len(n) < 3:
            return False
        now = time.time()
        blob = " ".join(t for ts, t in self.speaker.recent if now - ts < 25)
        if not blob:
            return False
        if n in blob:
            return True
        if self.speaker.echo_window():
            words = n.split()
            blob_words = set(blob.split())
            overlap = sum(1 for w in words if w in blob_words) / max(1, len(words))
            if overlap >= 0.6:
                return True
            for _, spoken in self.speaker.recent[-4:]:
                if difflib.SequenceMatcher(None, n, spoken).ratio() >= 0.55:
                    return True
        return False

    async def on_partial(self, text: str) -> None:
        if self.is_echo(text):
            return
        if self.speaker.speaking and len(text.split()) >= 3:
            await self.speaker.stop(f"someone started talking: {text[:40]!r}")
            if self.current_answer and not self.current_answer.done():
                self.current_answer.cancel()

    async def on_committed(self, text: str, speaker: str = "Someone") -> None:
        text = text.strip()
        if not text:
            return
        if self.is_echo(text):
            log.info("echo   (ignored) %s", text)
            return
        # a committed utterance can start with the tail of our own echoed speech: drop those leading sentences
        sentences, rest = take_sentences(text)
        parts = sentences + ([rest] if rest.strip() else [])
        while len(parts) > 1 and self.is_echo(parts[0]):
            log.info("echo   (stripped) %s", parts[0])
            parts.pop(0)
        text = " ".join(parts).strip()
        if speaker == "Someone":
            speaker = self.who_spoke(len(text.split()) * 0.35)
        self.transcript.add(speaker, text)
        log.info("heard  %s: %s", speaker, text)

        # Speculative path: while a conversation with the bot is open, start drafting the answer right away and
        # let the attention classifier decide (in parallel) whether the audio is released.
        speculative = self.attention.engaged() and not self.attention.has_wake_word(text)
        gate: Optional[asyncio.Future] = asyncio.get_running_loop().create_future() if speculative else None
        if speculative:
            if self.current_answer and not self.current_answer.done():
                self.current_answer.cancel()
            self.current_answer = asyncio.create_task(self.answer(speaker, text, gate))
        decision = await self.attention.decide(speaker, text)
        if gate is not None:
            if not gate.done():
                gate.set_result(decision.addressed)
        elif decision.addressed:
            if self.current_answer and not self.current_answer.done():
                self.current_answer.cancel()
            self.current_answer = asyncio.create_task(self.answer(speaker, text, None))

    # ------------------------------------------------------------------ answering
    async def answer(self, speaker: str, question: str, gate: Optional[asyncio.Future] = None) -> None:
        """Draft and speak an answer. With a gate, nothing is released until the gate resolves True."""
        t0 = time.time()
        self.speaker.first_audio_at = None
        if gate is None:
            await self.speaker.set_state("thinking")
        spoke_anything = False

        async def released() -> bool:
            if gate is None:
                return True
            if not gate.done():
                try:
                    await asyncio.wait_for(asyncio.shield(gate), timeout=4.0)
                except asyncio.TimeoutError:
                    guess = self.attention.fallback_guess(speaker, question)
                    log.warning("attention: classifier timed out; rule-based guess = %s", guess)
                    return guess
            return bool(gate.result())

        async def on_sentence(sentence: str) -> None:
            nonlocal spoke_anything
            if not await released():
                raise asyncio.CancelledError("not addressed")
            spoke_anything = True
            self.speaker.say(sentence)

        async def on_tool(name: str, args: dict) -> None:
            nonlocal spoke_anything
            if not await released():
                raise asyncio.CancelledError("not addressed")
            if name == "post_chat":
                chat_text = args.get("text", "")
                if not spoke_anything and chat_text:
                    # the model answered only via chat: speak the gist so the room actually hears an answer
                    sentences, rest = take_sentences(speech_clean(chat_text))
                    gist = " ".join((sentences or [rest])[:2])
                    if gist:
                        self.speaker.say(gist[:350])
                        spoke_anything = True
                await self.attendee.chat(chat_text)
            elif name == "delegate":
                spoke_anything = True
                if self.bridge_clips:
                    i = int(time.time()) % len(self.bridge_clips)
                    self.speaker.play_pcm(BRIDGE_PHRASES[i], self.bridge_clips[i])
                else:
                    self.speaker.say(BRIDGE_PHRASES[0])
                if self.deep and self.deep.available:
                    asyncio.create_task(self.deep.ask(speaker, args.get("question", question), self.transcript.window(120)))
                else:
                    self.speaker.say(NO_DEEP_LINE)
            elif name == "stay_silent":
                log.info("fast: stay_silent")
                await self.speaker.stop("stay_silent")

        try:
            system = fast_system(self.s, self.briefing, bool(self.deep and self.deep.available))
            await self.fast.respond(system, self.transcript.window(180), speaker, question, on_sentence, on_tool)
            if self.speaker.first_audio_at:
                log.info("latency: question committed -> first audio out %.0f ms", (self.speaker.first_audio_at - t0) * 1000)
        except asyncio.CancelledError:
            log.info("answer discarded (%s)", "not addressed" if gate is not None and gate.done() and not gate.result() else "interrupted")
        except Exception as e:
            log.error("answer failed: %s", e)
            self.speaker.say(ERROR_LINE)
        finally:
            if not self.speaker.speaking:
                await self.speaker.set_state("listening")

    async def greet(self) -> None:
        if self.greeted:
            return
        self.greeted = True
        await asyncio.sleep(2)
        self.speaker.say(greeting(self.s))

    # ------------------------------------------------------------------ bot lifecycle
    async def join(self, meeting_url: str) -> str:
        if not self.s.public_url:
            raise RuntimeError("no public URL: start with a Cloudflare quick tunnel (cloudflared installed) or set PUBLIC_URL")
        self.greeted = False
        bot_id = await self.attendee.create_bot(meeting_url, self.s.public_url)
        asyncio.create_task(self._watch_state())
        return bot_id

    async def _watch_state(self) -> None:
        last = None
        for _ in range(1440):  # about two hours at 5 s
            try:
                st = await self.attendee.state()
            except Exception as e:
                st = f"poll error: {e}"
            if st != last:
                log.info("bot state: %s", st)
                last = st
                self.bot_state = st
                if st == "waiting_room":
                    log.info(">>> the bot is in the waiting room: please admit \"%s\" from the meeting", self.s.bot_name)
                if st in ("ended", "fatal_error", "data_deleted"):
                    return
            await asyncio.sleep(5)

    def health(self) -> dict:
        return {
            "public_url": self.s.public_url,
            "bot_id": self.attendee.bot_id,
            "bot_state": self.bot_state,
            "bot_audio_ws": bool(self.speaker.bot_ws and not self.speaker.bot_ws.closed),
            "stt_connected": self.stt.connected.is_set(),
            "monitors": len(self.speaker.monitors),
            "deep_agent": bool(self.deep and self.deep.available),
            "transcript_items": len(self.transcript.items),
            "attention": {"engaged": self.attention.engaged(), "muted": self.attention.muted, "last_asker": self.attention.last_asker, "classifier": self.attention.classify is not None},
            "participants": self.names,
            "mixed_audio": self.mixed_seen,
            "page_mic": self.page_mic_seen,
            "config": self.s.summary(),
        }
