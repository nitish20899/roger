"""OpenAI GPT-Live: the bot's ears, voice and turn-taking, over one full-duplex WebSocket.

GPT-Live listens and speaks at the same time on a single connection, so it replaces the separate
transcription and text-to-speech stages a chained pipeline needs. Reasoning is *delegated*: the model
decides when it needs help and hands the work to a backend, while keeping the conversation going.

Roger uses **Responses delegation**, which the guide recommends over running the backend yourself. The
backend model, its prompt and its tools are declared in ``session.start``; GPT-Live then supplies the
conversation to that model itself and speaks the result. That matters for a meeting: the delegation
event carries no task text, so a client-side backend has to reconstruct the question from the transcript
and can easily answer the wrong one. Here OpenAI passes the conversation across and the question is
never reconstructed. Web search is a hosted tool; Roger's own tools (see :mod:`roger.tools`) arrive as
``response.event`` envelopes, are run here, and are returned with ``response.item.create``.

Protocol (https://developers.openai.com/api/docs/guides/live):
  * connect to ``wss://api.openai.com/v1/live/sessions`` with ``Authorization: Bearer``, no query string
  * send ``session.start`` first, wait for ``session.started`` before anything else
  * audio both ways is base64 in JSON: ``session.input_audio.append`` out, ``session.output_audio.delta`` in;
    the output stream is continuous for the whole session -- real-time silence when the model is not talking --
    so whether it is *speaking* is a question for :class:`roger.speaker.Speaker`, which sees the samples
  * ``session.close`` then wait for ``session.closed`` to get final usage

Everything here is transport. What the model *does* is in :mod:`roger.prompts`.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import time
from typing import Any, Awaitable, Callable, Optional

import websockets

from .config import Settings

log = logging.getLogger("roger.live")

LIVE_URL = "wss://api.openai.com/v1/live/sessions"

APPEND_MAX_CHARS = 1800  # the append events cap content at 500 tokens; stay well inside it
TURN_GAP_MS = 900.0  # silence in the transcript timeline that ends someone's turn
TURN_IDLE_S = 1.2  # wall-clock idle after the last fragment that also ends a turn
SETTLE_IDLE_S = 0.45  # a delegation may not wait the full turn gap; this is enough to call a turn done
SETTLE_MAX_S = 2.0  # never hold a delegation longer than this waiting for the words to land
CLOSE_TIMEOUT_S = 15.0  # how long to wait for session.closed before giving up on final usage
KEEPALIVE_GAP_S = 0.25  # no real audio for this long and silence is sent instead

AudioCallback = Callable[[bytes], Awaitable[None]]
TurnCallback = Callable[[str], Awaitable[None]]
ToolCallback = Callable[[str, str], Awaitable[str]]  # (name, arguments json) -> result text


class _TranscriptStream:
    """Reassembles ``*_transcript.delta`` fragments into whole turns.

    The deltas carry text plus a position on the session timeline, but no turn boundaries and no
    done event, so a turn is closed either by a gap in that timeline or by wall-clock silence.
    """

    def __init__(self, on_turn: TurnCallback, gap_ms: float = TURN_GAP_MS) -> None:
        self.on_turn = on_turn
        self.gap_ms = gap_ms
        self.buf = ""
        self.last_end_ms: Optional[float] = None
        self.last_at = 0.0

    async def add(self, delta: str, start_ms: Optional[float], end_ms: Optional[float]) -> None:
        if self.buf and self.last_end_ms is not None and start_ms is not None and start_ms - self.last_end_ms > self.gap_ms:
            await self.flush()
        self.buf += delta
        if end_ms is not None:
            self.last_end_ms = end_ms
        self.last_at = time.time()

    async def flush(self) -> None:
        text, self.buf = self.buf.strip(), ""
        self.last_end_ms = None
        if text:
            await self.on_turn(text)

    async def flush_if_idle(self, idle_s: float = TURN_IDLE_S) -> None:
        if self.buf and time.time() - self.last_at >= idle_s:
            await self.flush()

    async def settle(self, max_wait: float = SETTLE_MAX_S, idle_s: float = SETTLE_IDLE_S) -> None:
        """Wait for the utterance in flight to finish, then close it.

        GPT-Live raises a delegation as soon as it understands the request -- which is often a second
        before the speaker has stopped and the transcript turn has been closed. Answering right then means
        answering from a transcript that does not yet contain the question.
        """
        deadline = time.time() + max_wait
        while self.buf and time.time() < deadline:
            await asyncio.sleep(0.1)
            await self.flush_if_idle(idle_s)
        if self.buf:
            await self.flush()


class LiveSession:
    """One GPT-Live conversation for the meeting, reconnecting for as long as the bridge runs."""

    def __init__(
        self,
        s: Settings,
        on_output_audio: AudioCallback,
        on_heard: TurnCallback,
        on_said: TurnCallback,
        on_tool_call: ToolCallback,
        instructions: Callable[[], str],
        history: Callable[[], list[dict]],
        backend: Callable[[], dict],
    ) -> None:
        self.s = s
        self.on_output_audio = on_output_audio
        self.instructions = instructions
        self.history = history
        self.backend = backend
        self.on_tool_call = on_tool_call
        self.heard = _TranscriptStream(on_heard)
        self.said = _TranscriptStream(on_said)

        self.ws: Optional[websockets.ClientConnection] = None
        self.task: Optional[asyncio.Task] = None
        self.ready = asyncio.Event()
        self.session_id: Optional[str] = None
        self.held = False  # asked to stay silent until called by name
        self.buf = bytearray()
        self.frame = s.chunk_bytes
        self.silence = b"\x00" * s.chunk_bytes
        self.last_fed_at = 0.0
        self.seq = 0
        self.usage_s = 0.0
        self.context_ratio = 0.0
        self.closing = False
        self.closed = asyncio.Event()
        self.auth_failed = False
        self.delegations = 0
        self.tool_calls = 0
        self.pending_calls: dict[str, list[dict]] = {}  # delegation id -> function calls awaiting results

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if not self.task:
            self.task = asyncio.create_task(self._run())

    def _event_id(self, prefix: str) -> str:
        self.seq += 1
        return f"{prefix}_{self.seq}"

    def _session_config(self) -> dict[str, Any]:
        cfg: dict[str, Any] = {
            "model": self.s.live_model,
            "instructions": self.instructions(),
            "audio": {
                "format": {"type": "audio/pcm", "rate": self.s.sample_rate},
                "output": {"voice": self.s.voice},
            },
            # The API runs the backend model and passes it the conversation; Roger only runs the tools.
            "delegation": {"type": "responses", "responses": self.backend()},
        }
        seed = self.history()
        if seed:
            cfg["input"] = seed
        return cfg

    async def _send(self, event: dict[str, Any]) -> bool:
        ws = self.ws
        if ws is None or not self.ready.is_set():
            return False
        try:
            await ws.send(json.dumps(event))
            return True
        except Exception as e:
            log.warning("send %s failed: %s", event.get("type"), e)
            self.ready.clear()
            return False

    async def _run(self) -> None:
        if not self.s.openai_api_key:
            log.error("OPENAI_API_KEY missing: Roger cannot hear or speak")
            return
        backoff = 1.0
        while not self.closing:
            try:
                async with websockets.connect(
                    LIVE_URL,
                    additional_headers={"Authorization": f"Bearer {self.s.openai_api_key}"},
                    max_size=16 * 1024 * 1024,
                ) as ws:
                    self.ws = ws
                    await ws.send(json.dumps({"type": "session.start", "event_id": self._event_id("start"), "session": self._session_config()}))
                    await self._receive(ws)
                    backoff = 1.0
            except Exception as e:
                if self.closing:
                    break
                if self._is_auth_error(e):
                    log.error("GPT-Live rejected the API key (%s). Check OPENAI_API_KEY and that your project has access to %s.", e, self.s.live_model)
                    self.auth_failed = True
                    return
                log.warning("connection error: %s (retry in %.0fs)", e, backoff)
            finally:
                self.ready.clear()
                self.ws = None
            if self.closing:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 15)

    @staticmethod
    def _is_auth_error(e: Exception) -> bool:
        text = str(e).lower()
        return "401" in text or "403" in text or "invalid_api_key" in text or "unauthorized" in text

    async def _receive(self, ws: websockets.ClientConnection) -> None:
        helpers = [asyncio.create_task(self._idle_flush()), asyncio.create_task(self._keepalive())]
        try:
            async for raw in ws:
                try:
                    evt = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                await self._handle(evt)
        finally:
            for h in helpers:
                h.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(*helpers, return_exceptions=True)

    async def _keepalive(self) -> None:
        """Keep the input stream unbroken with silence when no meeting audio is arriving.

        The session timeline only advances while audio is being appended, and appended context is
        delivered against that timeline. Without this, a bot that has not joined a meeting yet -- or one
        whose audio has briefly stalled -- would take instructions and results that never arrive, and
        report ``context_injection_incomplete`` when it closes.
        """
        while True:
            await asyncio.sleep(0.1)
            if self.ready.is_set() and not self.closing and time.time() - self.last_fed_at > KEEPALIVE_GAP_S:
                self.last_fed_at = time.time()
                await self._send({"type": "session.input_audio.append", "audio": base64.b64encode(self.silence).decode()})

    async def _idle_flush(self) -> None:
        """Close a transcript turn that has simply gone quiet (no fragment ever marks the end)."""
        while True:
            await asyncio.sleep(0.3)
            await self.heard.flush_if_idle()
            await self.said.flush_if_idle()

    # ------------------------------------------------------------------ the Responses backend
    async def update_backend(self) -> bool:
        """Push new backend settings (a briefing that has just arrived) without restarting the session."""
        return await self._send({"type": "session.update", "event_id": self._event_id("update"), "session": {"delegation": {"type": "responses", "responses": self.backend()}}})

    async def _on_response_event(self, delegation_id: str, ev: dict) -> None:
        """One nested Responses event. Function calls are collected, then answered as a batch."""
        kind = ev.get("type")
        if kind == "response.output_item.done":
            item = ev.get("item") or {}
            if item.get("type") == "function_call" and item.get("call_id"):
                # Only the finished item carries call_id, name and arguments together.
                self.pending_calls.setdefault(delegation_id, []).append(item)
        elif kind == "response.completed":
            calls = self.pending_calls.pop(delegation_id, [])
            if calls:
                asyncio.create_task(self._run_tools(calls))
        elif kind in ("response.failed", "response.incomplete"):
            log.warning("backend %s: %s", kind, str(ev.get("response", {}).get("error") or "")[:200])
        elif kind == "error":
            log.warning("backend error: %s", str(ev)[:200])

    async def _run_tools(self, calls: list[dict]) -> None:
        """Run every pending call, return each result, then let the backend continue."""
        for item in calls:
            name = str(item.get("name") or "")
            try:
                result = await self.on_tool_call(name, item.get("arguments") or "{}")
            except Exception as e:
                log.warning("tool %s raised: %s", name, e)
                result = f"{name} failed: {e}"
            self.tool_calls += 1
            log.info("tool   %s -> %d chars", name, len(result))
            await self._send({
                "type": "response.item.create",
                "event_id": self._event_id("tool"),
                "item": {"type": "function_call_output", "call_id": item["call_id"], "output": result[:20000]},
            })
        await self._send({"type": "response.create", "event_id": self._event_id("continue")})

    async def _handle(self, evt: dict) -> None:
        kind = evt.get("type")
        if kind == "session.started":
            session = evt.get("session") or {}
            self.session_id = session.get("id")
            self.ready.set()
            log.info("session %s ready (%s, voice=%s, %d Hz)", self.session_id, self.s.live_model, self.s.voice, self.s.sample_rate)
        elif kind == "session.output_audio.delta":
            pcm = base64.b64decode(evt.get("delta") or "")
            if pcm:
                await self.on_output_audio(pcm)
        elif kind == "session.input_transcript.delta":
            await self.heard.add(evt.get("delta") or "", evt.get("start_ms"), evt.get("end_ms"))
        elif kind == "session.output_transcript.delta":
            await self.said.add(evt.get("delta") or "", evt.get("start_ms"), evt.get("end_ms"))
        elif kind == "session.delegation.created":
            delegation = evt.get("delegation") or {}
            self.delegations += 1
            log.info("delegation %s -> %s at %s ms", str(delegation.get("id"))[:16], delegation.get("target"), evt.get("offset_ms"))
        elif kind == "response.event":
            await self._on_response_event(str(evt.get("delegation_id") or ""), evt.get("event") or {})
        elif kind == "session.usage.updated":
            self.usage_s = float((evt.get("usage") or {}).get("seconds") or self.usage_s)
            self.context_ratio = float((evt.get("context_window") or {}).get("usage_ratio") or 0.0)
            if self.context_ratio > 0.9:
                log.info("context window at %.0f%%; GPT-Live is rolling the conversation over", self.context_ratio * 100)
        elif kind == "session.closed":
            self.usage_s = float((evt.get("usage") or {}).get("seconds") or self.usage_s)
            log.info("session closed (%s) after %.0f s of voice", evt.get("reason"), self.usage_s)
            self.closed.set()
        elif kind == "error":
            err = evt.get("error") or {}
            log.error("live error %s/%s: %s%s", err.get("type"), err.get("code"), err.get("message"), f" [{err['param']}]" if err.get("param") else "")
        elif kind == "info":
            log.info("live: %s", evt.get("message"))
        elif kind in ("session.commentary.appended", "session.thinking.appended", "session.instructions.appended", "session.updated"):
            log.debug("%s (%s)", kind, evt.get("client_event_id"))
        else:
            log.debug("live event %s", kind)

    # ------------------------------------------------------------------ audio in
    async def feed(self, pcm: bytes) -> None:
        """Meeting audio towards the model. Buffered so one message carries ~100 ms."""
        if self.closing:
            return
        self.buf += pcm
        if len(self.buf) < self.frame or not self.ready.is_set():
            return
        self.last_fed_at = time.time()
        chunk, self.buf = bytes(self.buf), bytearray()
        if len(chunk) % 2:  # PCM16 messages must hold whole samples: carry the odd byte over
            self.buf += chunk[-1:]
            chunk = chunk[:-1]
        if chunk:
            await self._send({"type": "session.input_audio.append", "audio": base64.b64encode(chunk).decode()})

    async def settle_input(self) -> None:
        """Make sure what is being said right now is in the transcript before the backend reads it."""
        await self.heard.settle()

    def pending_input(self) -> str:
        """The words heard so far in an utterance that has not been closed yet."""
        return self.heard.buf.strip()

    # ------------------------------------------------------------------ context and results
    @staticmethod
    def _clip(text: str) -> str:
        text = " ".join(text.split())
        return text if len(text) <= APPEND_MAX_CHARS else text[: APPEND_MAX_CHARS - 1].rsplit(" ", 1)[0] + "…"

    async def commentary(self, content: str, delegation_id: Optional[str] = None) -> bool:
        """A result the model should say out loud, in its own words."""
        if not content.strip():
            return False
        return await self._send({"type": "session.commentary.append", "event_id": self._event_id("comment"), "delegation_id": delegation_id, "content": self._clip(content)})

    async def thinking(self, content: str, delegation_id: Optional[str] = None) -> bool:
        """Facts or progress the model can use later, without saying them now."""
        if not content.strip():
            return False
        return await self._send({"type": "session.thinking.append", "event_id": self._event_id("think"), "delegation_id": delegation_id, "content": self._clip(content)})

    async def instruct(self, content: str, delegation_id: Optional[str] = None) -> bool:
        """A behaviour change: greet now, stop talking, stay quiet. Can interrupt speech in progress."""
        if not content.strip():
            return False
        return await self._send({"type": "session.instructions.append", "event_id": self._event_id("instr"), "delegation_id": delegation_id, "content": self._clip(content)})

    async def hold(self, name: str) -> bool:
        """"Hold on, Roger": go silent but keep listening.

        Deliberately *not* ``session.input_audio.mute``. Muting the input would make the model deaf, and
        the only way out of a hold is hearing its own name, so a muted bot could never be called back.
        """
        self.held = True
        return await self.instruct(
            f"Stop speaking immediately. Say nothing at all from now on -- no replies, no acknowledgements, "
            f'no listening sounds -- until someone says the name "{name}". Keep listening and following the '
            f"conversation the whole time, so you understand what has been said when you are called back."
        )

    async def resume(self, name: str) -> bool:
        self.held = False
        return await self.instruct(f'You have been called back by name. You may speak again, starting with whoever just said "{name}".')

    # ------------------------------------------------------------------ shutdown
    async def close(self) -> None:
        """Graceful close: ask, then wait for ``session.closed`` so final usage is confirmed."""
        self.closing = True
        if self.ws is not None and self.ready.is_set():
            await self._send({"type": "session.close", "event_id": self._event_id("close")})
            try:
                await asyncio.wait_for(self.closed.wait(), timeout=CLOSE_TIMEOUT_S)
            except asyncio.TimeoutError:
                log.warning("no session.closed within %.0f s; final voice usage is unconfirmed", CLOSE_TIMEOUT_S)
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        if self.ws is not None:
            with contextlib.suppress(Exception):
                await self.ws.close()
