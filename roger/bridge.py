"""The orchestrator: meeting audio into GPT-Live, delegated questions out to the backends, voice back.

GPT-Live owns hearing, speaking, turn-taking and -- through Responses delegation -- the reasoning too.
What is left here is everything it cannot know by itself:

  * which human is talking (from Attendee's per-participant audio energy), so the transcript has names;
  * what the project is: a briefing read from your Claude Code and Codex sessions;
  * running the tools its backend asks for, against the repository and the meeting chat;
  * the one rule a prompt cannot be trusted with: holding, when someone says "hold on, Roger".
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from .attendee import Attendee
from .audio import PARTICIPANT_RATE, resample, rms
from .config import Settings
from .context import attached_sessions, build_briefing
from .live import LiveSession
from .manners import Manners
from .prompts import NO_BRIEFING, arrival_note, backend_instructions, greeting_instruction, live_instructions
from .speaker import Speaker
from .tools import Toolbox, tool_definitions
from .transcript import Transcript

log = logging.getLogger("roger.bridge")

BRIEFING_SEED_CHARS = 6000  # the session's `input` history is capped at 8,192 rendered tokens


class Bridge:
    def __init__(self, s: Settings) -> None:
        self.s = s
        self.speaker = Speaker(s)
        self.attendee = Attendee(s)
        self.transcript = Transcript()
        self.briefing = NO_BRIEFING
        self.sessions: list = []
        self.greeted = False
        self.bot_state: Optional[str] = None
        self.manners = Manners(s.wake_words)
        self.tools = Toolbox(s.project_dir, self.attendee.chat)
        self.live = LiveSession(
            s,
            on_output_audio=self.speaker.feed,
            on_heard=self.on_heard,
            on_said=self.on_said,
            on_tool_call=self.tools.run,
            instructions=lambda: live_instructions(self.s),
            history=self._seed_history,
            backend=self._backend_config,
        )
        # speaker attribution from per-participant audio energy
        self.energy: dict[str, list[tuple[float, float]]] = {}  # participant uuid -> [(time, rms)]
        self.names: dict[str, str] = {}
        self.names_refreshed = 0.0
        self.mixed_seen = False
        self.suppressed_frames = 0

    def _backend_config(self) -> dict:
        """The Responses backend GPT-Live delegates to: model, prompt and tools."""
        tools = tool_definitions(deep=self.s.repo_tools, chat=True)
        if self.s.web_search:
            tools.append({"type": "web_search"})
        cfg: dict = {
            "model": self.s.fast_model,
            "instructions": backend_instructions(self.s, self.briefing, self.s.repo_tools),
            "tools": tools,
            "tool_choice": "auto",
            "max_output_tokens": 800,
        }
        if self.s.fast_model.startswith(("gpt-5", "o")):
            cfg["reasoning"] = {"effort": self.s.backend_effort}
        return cfg

    def _seed_history(self) -> list[dict]:
        """Project knowledge handed to GPT-Live at startup, so it can hold a conversation about the work."""
        if self.briefing == NO_BRIEFING:
            return []
        return [{"role": "developer", "content": [{"type": "input_text", "text": "Background on the work this meeting is about:\n\n" + self.briefing[:BRIEFING_SEED_CHARS]}]}]

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
        # The briefing seeds the live session and the backend prompt, so read it before opening the session.
        try:
            self.sessions = attached_sessions(self.s)
            self.briefing = await build_briefing(self.s)
        except Exception as e:
            log.warning("briefing unavailable: %s", e)
        self.live.start()

    async def close(self) -> None:
        await self.live.close()
        await self.attendee.close()

    # ------------------------------------------------------------------ audio in
    def _suppressed(self) -> bool:
        """Withhold meeting audio while the bot speaks, if it would otherwise hear itself.

        Off by default: the meeting's mixed stream does not normally carry the bot's own voice, and
        gating it would cost the full-duplex interruptions that make GPT-Live worth using.
        """
        return self.s.echo_suppress and self.speaker.echo_window()

    async def on_audio(self, pcm: bytes, from_mixed: bool = True) -> None:
        """Mixed meeting audio from the Attendee WebSocket."""
        if from_mixed:
            self.mixed_seen = True
        if self._suppressed():
            self.suppressed_frames += 1
            return
        await self.live.feed(pcm)

    async def on_participant_audio(self, uuid: str, pcm: bytes) -> None:
        """One participant's audio: speaker attribution, and the transcription source if there is no mixed stream."""
        now = time.time()
        lst = self.energy.setdefault(uuid, [])
        lst.append((now, rms(pcm)))
        if len(lst) > 400:
            del lst[: len(lst) - 400]
        if uuid not in self.names and now - self.names_refreshed > 5:
            asyncio.create_task(self.refresh_names())
        if not self.mixed_seen:
            # Fallback only: Attendee will not send per-participant streams above 16 kHz, so they have to be
            # brought up to the session's rate or the bot would hear everyone at the wrong pitch.
            await self.on_audio(resample(pcm, min(self.s.sample_rate, PARTICIPANT_RATE), self.s.sample_rate), from_mixed=False)

    async def refresh_names(self) -> None:
        self.names_refreshed = time.time()
        try:
            found = await self.attendee.participants()
            if found:
                self.names.update(found)
        except Exception as e:
            log.debug("participants: %s", e)

    def who_spoke(self, approx_duration_s: float) -> str:
        """Which participant's stream carried the most energy over the utterance just transcribed."""
        if not self.energy:
            return "Someone"
        end = time.time() - 0.4  # transcription lag
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

    # ------------------------------------------------------------------ transcript events from GPT-Live
    async def on_heard(self, text: str) -> None:
        """A finished turn from someone in the room. GPT-Live has already heard it; this is for context."""
        speaker = self.who_spoke(len(text.split()) * 0.35)
        self.transcript.add(speaker, text)
        log.info("heard  %s: %s", speaker, text)
        asyncio.create_task(self._meeting_manners(text))

    async def on_said(self, text: str) -> None:
        """A finished turn the bot spoke, in GPT-Live's own words."""
        self.speaker.note_said(text)
        self.transcript.add(self.s.bot_first_name, text)

    async def _meeting_manners(self, text: str) -> None:
        """Holding. Runs off the speech path and never delays a reply."""
        action = self.manners.heard(text)
        if action == "hold":
            await self.live.hold(self.s.bot_first_name)
            await self.speaker.flush("asked to hold")
            log.info('holding: silent until someone says "%s"', self.s.bot_first_name)
        elif action == "resume":
            await self.live.resume(self.s.bot_first_name)
            log.info("called back by name; speaking again")

    async def speak_exactly(self, text: str) -> None:
        """``roger say``: word-for-word, which is what instructions (not commentary) are for."""
        await self.live.instruct(f"Say this to the meeting now, word for word, and nothing else: {text}")

    async def inject_utterance(self, speaker: str, text: str) -> None:
        """``roger ask``: pretend someone in the room said this, without a meeting."""
        self.transcript.add(speaker, text)
        log.info("heard  %s: %s (injected)", speaker, text)
        await self.live.instruct(f'{speaker} just said to you in the meeting: "{text}". Respond to them now.')

    async def greet(self) -> None:
        """Arrival. By default it says nothing and waits to be spoken to; GREETING opts back in."""
        if self.greeted:
            return
        self.greeted = True
        try:
            await asyncio.wait_for(self.live.ready.wait(), timeout=20)
        except asyncio.TimeoutError:
            log.warning("arrival note skipped: the live session was not ready in time")
            return
        await self.live.thinking(arrival_note(self.s, self.briefing != NO_BRIEFING))
        if self.s.greeting:
            await asyncio.sleep(1.5)
            await self.live.instruct(greeting_instruction(self.s))
            log.info("greeted on arrival (GREETING is set)")
        else:
            log.info("joined quietly; it will speak when someone speaks to it")

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
            "live": {
                "connected": self.live.ready.is_set(),
                "session_id": self.live.session_id,
                "held": self.live.held,
                "speaking": self.speaker.speaking,
                "voice_seconds": round(self.live.usage_s, 1),
                "spoken_seconds": round(self.speaker.voiced_seconds(), 1),
                "context_used": round(self.live.context_ratio, 3),
                "delegations": self.live.delegations,
                "tool_calls": self.live.tool_calls,
                "holding": self.manners.holding,
            },
            "monitors": len(self.speaker.monitors),
            "sessions": [{"engine": r.engine, "id": r.id[:8], "title": r.title} for r in self.sessions],
            "briefing_chars": len(self.briefing) if self.briefing != NO_BRIEFING else 0,
            "tool_calls": self.tools.calls,
            "transcript_items": len(self.transcript.items),
            "participants": self.names,
            "mixed_audio": self.mixed_seen,
            "config": self.s.summary(),
        }
