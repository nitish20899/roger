"""The audio path, end to end, in a real browser. Opt in with ROGER_BROWSER_TESTS=1.

A page here does exactly what a meeting app does: calls ``getUserMedia`` and sends the track it gets over
an RTCPeerConnection. The far end is a second peer in the same page, so the hook in ``bridge.js`` sees an
inbound remote track just as it would in a real call. Python then speaks a 440 Hz tone and listens for it
coming back.

This exists because of a bug that no unit test could have caught. Chrome hands WebRTC a *silent* track
from a MediaStreamAudioDestinationNode whose AudioContext is not at the browser's native sample rate:
perfectly formed, perfectly empty audio, no error anywhere, in the page or on the wire. Roger ran at
24 kHz and was mute in every meeting. So the fix -- an outbound context at the browser's rate and an
inbound one at ours -- is pinned by a test that would fail if anyone ever puts the rate back.
"""
from __future__ import annotations

import asyncio
import math
import os
import struct

import pytest

from roger.audio import tone
from roger.config import Settings
from roger.meeting import Event, browser_available

pytestmark = pytest.mark.skipif(
    not (os.getenv("ROGER_BROWSER_TESTS") and browser_available()),
    reason="set ROGER_BROWSER_TESTS=1 and install the browser extra to run the browser audio tests",
)

TONE_HZ = 440.0
PAGE = """<!doctype html><meta charset=utf-8><title>loopback</title><body><script>
window.ready = (async () => {
  const mic = await navigator.mediaDevices.getUserMedia({audio: true});
  const pc1 = new RTCPeerConnection(), pc2 = new RTCPeerConnection();
  pc1.onicecandidate = e => e.candidate && pc2.addIceCandidate(e.candidate);
  pc2.onicecandidate = e => e.candidate && pc1.addIceCandidate(e.candidate);
  mic.getAudioTracks().forEach(t => pc1.addTrack(t, mic));
  const o = await pc1.createOffer(); await pc1.setLocalDescription(o); await pc2.setRemoteDescription(o);
  const a = await pc2.createAnswer(); await pc2.setLocalDescription(a); await pc1.setRemoteDescription(a);
  return mic.getAudioTracks().length;
})();
</script></body>"""


def goertzel(pcm: bytes, hz: float, rate: int) -> float:
    """Energy at one frequency: enough to tell a 440 Hz tone from anything else."""
    n = len(pcm) // 2
    if n < 64:
        return 0.0
    samples = struct.unpack("<" + "h" * n, pcm[: n * 2])
    k = int(0.5 + n * hz / rate)
    w = 2 * math.pi * k / n
    coeff, s1, s2 = 2 * math.cos(w), 0.0, 0.0
    for x in samples:
        s1, s2 = x + coeff * s1 - s2, s1
    return math.sqrt(max(0.0, s1 * s1 + s2 * s2 - coeff * s1 * s2)) / n


async def _round_trip(tmp_path, port: int = 8798) -> dict:
    from aiohttp import web
    from playwright.async_api import async_playwright

    from roger.meeting import BrowserMeeting
    from roger.meeting.browser import BINDING, CHROME_ARGS

    s = Settings()
    s.openai_api_key = "sk-test"
    s.browser_headless = True
    meeting = BrowserMeeting(s)

    captured = bytearray()
    meeting.on(Event.AUDIO, lambda pcm, rate: captured.extend(pcm))

    # Served over http://127.0.0.1 rather than a data: URL: getUserMedia only exists in a secure context,
    # and localhost counts as one. Nothing else is served -- the audio crosses on the binding.
    app = web.Application()
    app.add_routes([web.get("/p", lambda r: web.Response(text=PAGE, content_type="text/html"))])
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()

    pw = await async_playwright().start()
    ctx = await pw.chromium.launch_persistent_context(
        str(tmp_path / "profile"), headless=True, args=CHROME_ARGS,
        permissions=["microphone", "camera"], ignore_default_args=["--enable-automation", "--mute-audio"],
    )
    try:
        await ctx.expose_binding(BINDING, meeting._on_binding)
        await ctx.add_init_script(meeting._bridge_js())
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        meeting.page = page
        await page.goto(f"http://127.0.0.1:{port}/p", wait_until="load")

        for _ in range(100):
            if meeting._linked:
                break
            await asyncio.sleep(0.1)
        mic_tracks = await page.evaluate("window.ready")
        await asyncio.sleep(1.5)

        rates = await page.evaluate(
            "() => ({out: window.__rogerBridge.outCtx.sampleRate, inn: window.__rogerBridge.inCtx.sampleRate})"
        )
        captured.clear()
        audio = tone(4.0, hz=TONE_HZ, amplitude=12000, rate=s.sample_rate)
        frame = s.sample_rate * 2 // 10
        for i in range(0, len(audio), frame):
            await meeting.send_audio(audio[i : i + frame])
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.3)

        heard = bytes(captured)
        mid = len(heard) // 2
        window = heard[max(0, mid - 24000) : mid + 24000]   # while the tone was playing
        return {
            "linked": meeting._linked,
            "mic_tracks": mic_tracks,
            "streams": len(meeting._streams),
            "rates": rates,
            "seconds": len(heard) / 2 / s.sample_rate,
            "at_tone": goertzel(window, TONE_HZ, s.sample_rate),
            "off_tone": goertzel(window, 1000.0, s.sample_rate),
        }
    finally:
        meeting.page = None
        await ctx.close()
        await pw.stop()
        await runner.cleanup()


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    return asyncio.run(_round_trip(tmp_path_factory.mktemp("browser")))


def test_the_page_links_to_python_without_a_socket(result):
    """The binding is the transport: a page WebSocket is blocked by Meet's CSP and crashes the renderer."""
    assert result["linked"] is True


def test_the_page_gets_rogers_microphone(result):
    assert result["mic_tracks"] == 1


def test_an_inbound_webrtc_track_is_captured(result):
    assert result["streams"] >= 1


def test_the_outbound_context_runs_at_the_browsers_own_rate(result):
    """The bug this file exists for: a non-native outbound rate silently mutes Roger."""
    assert result["rates"]["out"] >= 44100, result["rates"]


def test_capture_arrives_at_rogers_wire_rate(result):
    assert result["rates"]["inn"] == 24000, result["rates"]


def test_meeting_audio_reaches_python(result):
    assert result["seconds"] > 2.0, result


def test_the_tone_survives_the_whole_round_trip(result):
    """Python -> fake mic -> WebRTC -> capture hook -> Python, and it is still a 440 Hz tone."""
    assert result["at_tone"] > 20, result
    assert result["at_tone"] > result["off_tone"] * 5, result
