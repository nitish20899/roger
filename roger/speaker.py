"""The bot's mouth: GPT-Live's output audio, paced out to the meeting and the monitor page.

GPT-Live streams output audio *continuously* for the whole session: real-time silence while it listens,
speech while it talks. So "is it speaking?" is decided by the energy of the samples, not by whether audio
arrived. Getting that wrong pushes a permanent stream of silence at the meeting and holds the echo
gate shut forever.

That real-time pacing is also why this module is a jitter buffer, and it does two things.

First, a cushion. A text-to-speech API returns a whole sentence far faster than it takes to say it, so the
far end always had audio in hand. GPT-Live hands over roughly a second of audio per second, so there is
none, and any wobble becomes an audible gap. The first ``output_buffer_ms`` of a turn is therefore held
back before anything is sent.

Second, and this is the part that actually removes the stutter: **output is paced, not forwarded**.
Measured arrivals from GPT-Live are bursty -- 300 ms of nothing, then three chunks at once. Passing that
through hands the meeting the same lumpy stream. Instead the play head tracks where the audio sent so far
would finish playing, and frames are released to stay exactly one cushion ahead of the wall clock. A bursty
input becomes an even 100 ms cadence out, and the cushion is what absorbs the bursts.

Silence is not transmitted. Only whole frames leave, at an even 100 ms cadence, because GPT-Live's deltas
are arbitrary sizes and forwarding them raw gives the far end a lumpy, stuttering stream.

Audio goes to the Attendee audio WebSocket as ``realtime_audio.bot_output`` frames, and to any monitor
page open at ``/monitor``, which is how you listen to the bot without joining a meeting.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import time
from collections import deque
from typing import Optional

from aiohttp import web

from .audio import rms
from .config import Settings

log = logging.getLogger("roger.speaker")

SPEECH_RMS = 200  # above this a frame carries voice; GPT-Live's idle stream sits at ~0
PREROLL_FRAMES = 2  # quiet frames kept ahead of speech so the first syllable is not clipped
SILENCE_HANGOVER_S = 0.5  # quiet for this long and the bot has finished its turn
ECHO_TAIL_S = 2.0  # how long after a turn its voice can still come back through the meeting
FRAME_S = 0.1


class Speaker:
    def __init__(self, s: Settings) -> None:
        self.s = s
        self.bot_ws: Optional[web.WebSocketResponse] = None
        self.monitors: set[web.WebSocketResponse] = set()
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()
        self.speaking = False
        self.last_spoke_end = 0.0
        self.first_audio_at: Optional[float] = None
        self._runner: Optional[asyncio.Task] = None
        self._play_head = 0.0
        self.last_voice_at = 0.0
        self.bytes_out = 0
        self.voiced_bytes = 0
        self.rate = s.sample_rate
        self.frame = s.chunk_bytes
        # Round the cushion up, never down: measured delivery from GPT-Live skips a beat now and then, and
        # a cushion shorter than one of those gaps is no cushion at all.
        # The cushion is both the delay before a turn starts and how far ahead of real time output is
        # allowed to run. Tying them together is what turns a bursty input into an even output.
        self.lead_s = max(FRAME_S, s.output_buffer_ms / 1000)
        self.prebuffer_frames = max(1, math.ceil(s.output_buffer_ms / (FRAME_S * 1000)))
        self.hangover_frames = max(1, round(SILENCE_HANGOVER_S / FRAME_S))
        self.max_pending = self.prebuffer_frames + self.hangover_frames + 10  # backstop against a runaway buffer

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
        bot = json.dumps({"trigger": "realtime_audio.bot_output", "data": {"chunk": b64, "sample_rate": self.rate}})
        mon = json.dumps({"type": "audio", "pcm": b64})
        now = time.time()
        if self._play_head < now:
            self._play_head = now
        ahead = self._play_head - now
        if ahead > self.lead_s:
            await asyncio.sleep(ahead - self.lead_s)  # hold the pace: never run further ahead than the cushion
        await self._send_all(bot, mon)
        self._play_head += len(chunk) / (self.rate * 2)
        self.bytes_out += len(chunk)

    async def set_state(self, state: str) -> None:
        await self._send_all(None, json.dumps({"type": "state", "state": state}))

    # ---- public API
    async def feed(self, pcm: bytes) -> None:
        """One chunk of GPT-Live output audio. Returns immediately; the worker paces it out."""
        if pcm:
            self.queue.put_nowait(pcm)

    def play_pcm(self, label: str, pcm: bytes) -> None:
        """Play raw PCM that did not come from the model (the test tone)."""
        log.info("play   [clip] %s", label)
        self.queue.put_nowait(pcm)

    def voiced_seconds(self) -> float:
        return self.voiced_bytes / (self.rate * 2)

    def note_said(self, text: str) -> None:
        log.info("spoke  %s", text)

    def echo_window(self) -> bool:
        """True while the bot speaks and shortly after, when its voice can still come back through the meeting."""
        return self.speaking or time.time() < self.last_voice_at + ECHO_TAIL_S

    async def flush(self, reason: str = "") -> None:
        """Drop audio that has not gone out yet. GPT-Live stops on its own; this clears our lead."""
        dropped = 0
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
                dropped += 1
            except asyncio.QueueEmpty:
                break
        if dropped:
            log.info("flushed %d queued chunks (%s)", dropped, reason)
        self._play_head = 0.0
        self.speaking = False
        await self._send_all(None, json.dumps({"type": "stop"}))

    # ---- worker
    async def _run(self) -> None:
        """Frame, gate on speech, buffer, then release.

        Frames accumulate in ``pending``. Nothing leaves while the bot is quiet. A turn is released once a
        full cushion of *speech* is in hand.

        Counting speech specifically matters. ``pending`` already holds a couple of quiet run-up frames
        when a turn begins, so counting everything in it released the turn on the first voiced frame: the
        far end got a cushion made almost entirely of silence, ran dry as soon as it reached the real
        audio, and every burst gap after that came out as a click.
        """
        buf = bytearray()
        pending: deque[bytes] = deque()
        quiet_run = 0
        speech_frames = 0
        released = False
        while True:
            try:
                pcm = await asyncio.wait_for(self.queue.get(), timeout=SILENCE_HANGOVER_S)
            except asyncio.TimeoutError:
                if released and pending:
                    await self._drain(pending)
                buf, quiet_run, released, speech_frames = bytearray(), quiet_run + self.hangover_frames, False, 0
                pending.clear()
                await self._settle()
                continue

            buf += pcm
            while len(buf) >= self.frame:
                frame, buf = bytes(buf[: self.frame]), buf[self.frame :]
                voiced = rms(frame) >= SPEECH_RMS
                if voiced:
                    quiet_run = 0
                    speech_frames += 1
                    await self._note_voice(len(frame))
                else:
                    quiet_run += 1
                pending.append(frame)
                if not self.speaking and not voiced and len(pending) > PREROLL_FRAMES:
                    pending.popleft()  # keep only a short run-up, never a backlog of silence

            if self.speaking:
                if not released and speech_frames >= self.prebuffer_frames:
                    released = True  # a full cushion of real speech is in hand: start the turn
                if released:
                    await self._drain(pending)
                elif len(pending) > self.max_pending:
                    await self._drain(pending)  # never sit on a backlog, whatever the energy said
                    released = True
                if quiet_run >= self.hangover_frames:
                    await self._drain(pending)  # end of turn: let a short utterance out even if it never filled the cushion
                    released, speech_frames = False, 0
            await self._settle()

    async def _drain(self, pending: "deque[bytes]") -> None:
        while pending:
            try:
                await self._send_pcm(pending.popleft())
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("send error: %s", e)
                return

    async def _note_voice(self, nbytes: int) -> None:
        self.last_voice_at = time.time()
        self.voiced_bytes += nbytes
        if not self.speaking:
            self.speaking = True
            self.first_audio_at = self.last_voice_at
            await self.set_state("speaking")

    async def _settle(self) -> None:
        if self.speaking and time.time() - self.last_voice_at > SILENCE_HANGOVER_S:
            self.speaking = False
            self.last_spoke_end = time.time()
            self._play_head = 0.0  # the next turn rebuilds its own cushion
            await self.set_state("listening")
