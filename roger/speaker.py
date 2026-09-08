"""The bot's voice: turns sentences into paced PCM for the meeting bot and for any monitor / orb pages."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Callable, Optional

from aiohttp import web

from .audio import CHUNK_BYTES, SAMPLE_RATE
from .text import norm_text, speech_clean
from .tts import TTS

log = logging.getLogger("roger.speaker")

OUTPUT_LEAD_S = 0.4  # how far ahead of real time the remote playout queue may run (bounds barge-in lag)


class Speaker:
    """Queue of sentences (or pre-rendered clips) played one after another.

    Audio goes to two places: the Attendee audio WebSocket (``realtime_audio.bot_output`` frames, when
    ``audio_out == "ws"``) and every connected monitor page, including the orb page that is the bot's
    webcam and, in ``audio_out == "page"`` mode, its actual microphone.
    """

    def __init__(self, tts: TTS, audio_out: str = "page") -> None:
        self.tts = tts
        self.audio_out = audio_out
        self.bot_ws: Optional[web.WebSocketResponse] = None
        self.monitors: set[web.WebSocketResponse] = set()
        self.queue: asyncio.Queue[tuple[str, Optional[bytes]]] = asyncio.Queue()
        self.speaking = False
        self.last_spoke_end = 0.0
        self.spoken_log: list[str] = []
        self.recent: list[tuple[float, str]] = []  # (time, normalised text) of recently spoken sentences, for echo detection
        self.first_audio_at: Optional[float] = None
        self.on_said: Optional[Callable[[str], None]] = None  # called when a sentence (not a clip) starts playing
        self._current: Optional[asyncio.Task] = None
        self._runner: Optional[asyncio.Task] = None
        self._play_head = 0.0

    def echo_window(self) -> bool:
        """True while we speak and shortly after, when our own voice can still come back through the meeting."""
        return self.speaking or time.time() < self._play_head + 2.5

    def start(self) -> None:
        if not self._runner:
            self._runner = asyncio.create_task(self._run())

    # ---- output plumbing
    async def _send_all(self, bot_frame: Optional[str], monitor_frame: str) -> None:
        if bot_frame and self.bot_ws is not None and not self.bot_ws.closed:
            try:
                await self.bot_ws.send_str(bot_frame)
            except Exception as e:
                log.warning("bot ws send failed: %s", e)
        for ws in list(self.monitors):
            try:
                await ws.send_str(monitor_frame)
            except Exception:
                self.monitors.discard(ws)

    async def _send_pcm(self, chunk: bytes) -> None:
        b64 = base64.b64encode(chunk).decode()
        bot = json.dumps({"trigger": "realtime_audio.bot_output", "data": {"chunk": b64, "sample_rate": SAMPLE_RATE}})
        mon = json.dumps({"type": "audio", "pcm": b64})
        now = time.time()
        if self._play_head < now:
            self._play_head = now
        ahead = self._play_head - now
        if ahead > OUTPUT_LEAD_S:
            await asyncio.sleep(ahead - OUTPUT_LEAD_S)
        await self._send_all(bot if self.audio_out == "ws" else None, mon)
        self._play_head += len(chunk) / (SAMPLE_RATE * 2)

    async def set_state(self, state: str) -> None:
        await self._send_all(None, json.dumps({"type": "state", "state": state}))

    # ---- public API
    def say(self, text: str) -> None:
        text = speech_clean(text)
        if text:
            self.queue.put_nowait((text, None))

    def play_pcm(self, label: str, pcm: bytes) -> None:
        self.queue.put_nowait((label, pcm))

    async def stop(self, reason: str = "") -> None:
        """Barge-in: drop queued speech and cancel the sentence being spoken."""
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        if self._current and not self._current.done():
            self._current.cancel()
        if self.speaking:
            log.info("stopped (%s)", reason)
        self.speaking = False
        self._play_head = 0.0
        await self._send_all(None, json.dumps({"type": "stop"}))
        await self.set_state("listening")

    # ---- worker
    async def _run(self) -> None:
        while True:
            text, pcm = await self.queue.get()
            self._current = asyncio.create_task(self._speak_one(text, pcm))
            try:
                await self._current
            except asyncio.CancelledError:
                pass
            except Exception as e:
                log.warning("error: %s", e)
            finally:
                if self.queue.empty():
                    self.speaking = False
                    self.last_spoke_end = time.time()
                    await self.set_state("listening")

    async def _speak_one(self, text: str, pcm: Optional[bytes]) -> None:
        self.speaking = True
        await self.set_state("speaking")
        self.spoken_log.append(text)
        self.recent.append((time.time(), norm_text(text)))
        self.recent = self.recent[-12:]
        log.info("speak  %s", text if pcm is None else f"[clip] {text}")
        if pcm is None and self.on_said:
            try:
                self.on_said(text)
            except Exception:
                pass
        if pcm is not None:
            for i in range(0, len(pcm), CHUNK_BYTES):
                await self._send_pcm(pcm[i : i + CHUNK_BYTES])
        else:
            async for chunk in self.tts.stream_pcm(text):
                if self.first_audio_at is None:
                    self.first_audio_at = time.time()
                await self._send_pcm(chunk)
        remaining = self._play_head - time.time()  # let the remote queue drain before the next sentence
        if remaining > 0:
            await asyncio.sleep(remaining)
