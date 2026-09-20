"""PCM helpers. Everything in Roger is 16-bit mono little-endian; the rate is a setting.

24 kHz is the default because GPT-Live generates it natively and the meeting page captures at it, so a
sample crosses the whole path -- ears, model, voice, back into the call -- without being converted once.
"""
from __future__ import annotations

import math
import struct

SAMPLE_RATE = 24000  # default; Settings.sample_rate is what actually runs
FRAME_MS = 100
PARTICIPANT_RATE = 16000  # a hosted participant caps per-speaker streams here; they only feed the energy meter


def chunk_bytes(rate: int, ms: int = FRAME_MS) -> int:
    """Bytes in one frame of 16-bit mono audio."""
    return int(rate * 2 * ms / 1000)


CHUNK_BYTES = chunk_bytes(SAMPLE_RATE)


def rms(pcm: bytes, stride: int = 4) -> float:
    """Root-mean-square level of a PCM16 buffer (0 = silence, ~32767 = full scale).

    Every audio frame of every participant passes through here, so it samples every ``stride``-th value
    rather than all of them. At 16 kHz that is still 400 samples per 100 ms frame -- far more than a
    level needs -- and it keeps the event loop free for the audio going out.
    """
    n = len(pcm) // 2
    if n == 0:
        return 0.0
    samples = struct.unpack("<%dh" % n, pcm[: n * 2])[::stride]
    return math.sqrt(sum(s * s for s in samples) / len(samples))


def tone(seconds: float = 1.0, hz: float = 440.0, amplitude: int = 8000, rate: int = SAMPLE_RATE) -> bytes:
    """A sine tone, used to verify the audio path without any API keys."""
    n = int(rate * seconds)
    return struct.pack("<%dh" % n, *[int(amplitude * math.sin(2 * math.pi * hz * i / rate)) for i in range(n)])


def resample(pcm: bytes, src_hz: int, dst_hz: int) -> bytes:
    """Linear resample of PCM16.

    Only the per-participant fallback needs this, and only for a hosted participant, which will not send
    those streams above 16 kHz. The browser participant captures at the session's rate already: its page
    resamples on the way in, where the browser does it properly and for free. So this runs on a stream
    nobody listens to directly, and linear interpolation is good enough for speech.
    """
    if src_hz == dst_hz or not pcm:
        return pcm
    src = struct.unpack("<%dh" % (len(pcm) // 2), pcm[: len(pcm) // 2 * 2])
    if len(src) < 2:
        return pcm
    n = int(len(src) * dst_hz / src_hz)
    step = (len(src) - 1) / max(1, n - 1)
    out = []
    for i in range(n):
        pos = i * step
        j = int(pos)
        frac = pos - j
        a0 = src[j]
        b0 = src[j + 1] if j + 1 < len(src) else a0
        out.append(int(a0 + (b0 - a0) * frac))
    return struct.pack("<%dh" % len(out), *out)
