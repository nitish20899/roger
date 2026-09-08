"""ElevenLabs text-to-speech, streamed as 16 kHz PCM."""
from __future__ import annotations

from typing import Any, AsyncIterator

import httpx

from .audio import CHUNK_BYTES, SAMPLE_RATE
from .config import Settings


class TTS:
    def __init__(self, s: Settings) -> None:
        self.s = s
        self.http = httpx.AsyncClient(timeout=30)
        self.url = f"https://api.elevenlabs.io/v1/text-to-speech/{s.voice_id}/stream"

    async def stream_pcm(self, text: str) -> AsyncIterator[bytes]:
        if not self.s.elevenlabs_api_key:
            raise RuntimeError("ELEVENLABS_API_KEY missing")
        body: dict[str, Any] = {
            "text": text,
            "model_id": self.s.tts_model,
            "voice_settings": {
                "stability": self.s.voice_stability,
                "similarity_boost": 0.75,
                "style": self.s.voice_style,
                "use_speaker_boost": True,
            },
        }
        if self.s.tts_model.startswith(("eleven_flash", "eleven_turbo")):
            body["language_code"] = self.s.language
        async with self.http.stream(
            "POST",
            self.url,
            params={"output_format": f"pcm_{SAMPLE_RATE}"},
            headers={"xi-api-key": self.s.elevenlabs_api_key, "Content-Type": "application/json"},
            json=body,
        ) as r:
            if r.status_code != 200:
                detail = (await r.aread()).decode(errors="replace")[:300]
                raise RuntimeError(f"ElevenLabs TTS {r.status_code}: {detail}")
            async for chunk in r.aiter_bytes(CHUNK_BYTES):
                if chunk:
                    yield chunk

    async def render(self, text: str) -> bytes:
        return b"".join([c async for c in self.stream_pcm(text)])

    async def close(self) -> None:
        await self.http.aclose()
