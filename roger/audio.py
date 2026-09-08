"""PCM helpers. Everything in Roger is 16-bit mono at 16 kHz."""
from __future__ import annotations

import math
import struct

SAMPLE_RATE = 16000
CHUNK_BYTES = 3200  # 100 ms of 16-bit mono at 16 kHz


def rms(pcm: bytes) -> float:
    """Root-mean-square level of a PCM16 buffer (0 = silence, ~32767 = full scale)."""
    n = len(pcm) // 2
    if n == 0:
        return 0.0
    samples = struct.unpack("<%dh" % n, pcm[: n * 2])
    return math.sqrt(sum(s * s for s in samples) / n)


def tone(seconds: float = 1.0, hz: float = 440.0, amplitude: int = 8000) -> bytes:
    """A sine tone, used to verify the audio path without any API keys."""
    n = int(SAMPLE_RATE * seconds)
    return struct.pack("<%dh" % n, *[int(amplitude * math.sin(2 * math.pi * hz * i / SAMPLE_RATE)) for i in range(n)])
