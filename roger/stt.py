"""ElevenLabs Scribe v2 Realtime speech-to-text over WebSocket, with automatic reconnection."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Awaitable, Callable, Optional

import websockets

from .audio import CHUNK_BYTES, SAMPLE_RATE
from .config import Settings
from .elevenlabs import exhausted_message

log = logging.getLogger("roger.stt")

QUOTA_BACKOFF_S = 60

TextCallback = Callable[[str], Awaitable[None]]


class STT:
    """One realtime transcription session for the meeting's audio.

    ``on_partial`` fires while someone is still talking (used for barge-in); ``on_committed`` fires when
    Scribe's voice-activity detection decides the utterance is over.
    """

    def __init__(self, s: Settings, on_partial: TextCallback, on_committed: TextCallback) -> None:
        self.s = s
        self.on_partial = on_partial
        self.on_committed = on_committed
        self.ws: Optional[websockets.ClientConnection] = None
        self.buf = bytearray()
        self.task: Optional[asyncio.Task] = None
        self.connected = asyncio.Event()
        self.quota_exhausted = False

    def url(self) -> str:
        params = {
            "model_id": self.s.stt_model,
            "audio_format": f"pcm_{SAMPLE_RATE}",
            "language_code": self.s.language,
            "commit_strategy": "vad",
            "vad_silence_threshold_secs": str(self.s.stt_silence_s),
            "include_timestamps": "false",
        }
        return "wss://api.elevenlabs.io/v1/speech-to-text/realtime?" + "&".join(f"{k}={v}" for k, v in params.items())

    def start(self) -> None:
        if not self.task:
            self.task = asyncio.create_task(self._run())

    async def feed(self, pcm: bytes) -> None:
        self.buf += pcm
        if len(self.buf) >= CHUNK_BYTES * 2 and self.ws and self.connected.is_set():  # ~200 ms per message
            chunk, self.buf = bytes(self.buf), bytearray()
            msg = {"message_type": "input_audio_chunk", "audio_base_64": base64.b64encode(chunk).decode(), "sample_rate": SAMPLE_RATE}
            try:
                await self.ws.send(json.dumps(msg))
            except Exception as e:
                log.warning("send failed: %s", e)
                self.connected.clear()

    async def _run(self) -> None:
        if not self.s.elevenlabs_api_key:
            log.warning("ELEVENLABS_API_KEY missing, transcription disabled")
            return
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self.url(), additional_headers={"xi-api-key": self.s.elevenlabs_api_key}, max_size=8 * 1024 * 1024) as ws:
                    self.ws = ws
                    self.connected.set()
                    log.info("connected (%s)", self.s.stt_model)
                    backoff = 1.0
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        mt = msg.get("message_type")
                        if mt == "partial_transcript":
                            if msg.get("text"):
                                await self.on_partial(msg["text"])
                        elif mt == "committed_transcript":
                            self.quota_exhausted = False
                            if msg.get("text"):
                                await self.on_committed(msg["text"])
                        elif mt == "session_started":
                            log.info("session %s", msg.get("session_id"))
                        elif mt == "quota_exceeded" or "insufficient_funds" in str(msg):
                            if not self.quota_exhausted:
                                log.error("%s. Retrying every %d s.", exhausted_message("Roger cannot hear"), QUOTA_BACKOFF_S)
                            self.quota_exhausted = True
                            break
                        elif (mt and mt.endswith("error")) or mt in ("rate_limited", "queue_overflow", "commit_throttled"):
                            log.warning("%s %s", mt, msg.get("error"))
            except Exception as e:
                if "insufficient_funds" in str(e) or "quota" in str(e).lower():
                    self.quota_exhausted = True
                elif not self.quota_exhausted:
                    log.warning("connection error: %s (retry in %.0fs)", e, backoff)
            self.connected.clear()
            self.ws = None
            await asyncio.sleep(QUOTA_BACKOFF_S if self.quota_exhausted else backoff)
            backoff = min(backoff * 2, 15)

    async def close(self) -> None:
        if self.task:
            self.task.cancel()
