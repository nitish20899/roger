"""The meeting layer: routing, the event bus, and the wire format the page speaks.

None of this needs a browser. What is worth testing without one is that URLs reach the right platform,
that events fan out, and that the binary frames the injected script sends are parsed the way it sends
them -- which is the one place a silent mistake would cost a whole meeting.
"""
from __future__ import annotations

import asyncio
import base64
import json
import struct

import pytest

from roger.config import Settings
from roger.meeting import (
    AttendeeMeeting, BrowserMeeting, Event, EventBus, MeetingClient, Participant, State,
    UnsupportedMeeting, browser_available, create_client, known_platforms, platform_for, supported_platforms,
)
from roger.meeting.platforms.google_meet import GoogleMeet
from roger.meeting.platforms.teams import Teams


def run(coro):
    return asyncio.run(coro)


def settings(**kw) -> Settings:
    s = Settings()
    s.openai_api_key = "sk-test"
    for k, v in kw.items():
        setattr(s, k, v)
    return s


# --------------------------------------------------------------------------- routing


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://meet.google.com/abc-defg-hij", GoogleMeet),
        ("https://meet.google.com/abc-defg-hij?authuser=1", GoogleMeet),
        ("https://teams.microsoft.com/l/meetup-join/19%3ameeting_x", Teams),
        ("https://teams.live.com/meet/9352819", Teams),
        ("https://teams.microsoft.us/l/meetup-join/x", Teams),
        ("https://zoom.us/j/123456", None),
        ("https://example.com/not-a-meeting", None),
        ("nonsense", None),
    ],
)
def test_platform_routing(url, expected):
    assert platform_for(url) is expected


def test_browser_handles_exactly_its_platforms():
    assert BrowserMeeting.handles("https://meet.google.com/abc-defg-hij")
    assert not BrowserMeeting.handles("https://zoom.us/j/1")


def test_hostname_match_is_not_a_substring_match():
    """``meet.google.com.evil.test`` must not look like Google Meet."""
    assert not BrowserMeeting.handles("https://meet.google.com.evil.test/abc")
    assert not GoogleMeet.handles("https://notmeet.google.com.evil.test/abc")


def test_every_platform_declares_itself():
    for p in known_platforms():
        assert p.name and p.hosts, p
    assert "google-meet" in supported_platforms()
    assert "teams" in supported_platforms()


# --------------------------------------------------------------------------- provider choice


def test_browser_is_the_default_and_needs_no_keys():
    s = settings()
    assert s.meeting_provider == "browser"
    assert s.problems() == []
    assert isinstance(create_client(s), BrowserMeeting)


def test_attendee_is_opt_in():
    s = settings(meeting_provider="attendee", attendee_api_key="tok")
    assert isinstance(create_client(s), AttendeeMeeting)


def test_attendee_without_a_key_is_reported():
    s = settings(meeting_provider="attendee")
    assert any("ATTENDEE_API_KEY" in p for p in s.problems())


def test_unknown_provider_is_rejected():
    with pytest.raises(RuntimeError, match="not one of"):
        create_client(settings(meeting_provider="carrier-pigeon"))


def test_auto_prefers_the_browser_when_there_is_no_hosted_key():
    s = settings(meeting_provider="auto")
    assert isinstance(create_client(s), BrowserMeeting)


def test_auto_falls_back_to_the_hosted_service_without_playwright():
    s = settings(meeting_provider="auto", attendee_api_key="tok")
    expected = BrowserMeeting if browser_available() else AttendeeMeeting
    assert isinstance(create_client(s), expected)


def test_joining_an_unsupported_url_says_what_is_supported():
    """A bad URL is a client error, so the server can answer 400 rather than 500."""
    with pytest.raises(UnsupportedMeeting, match="google-meet"):
        create_client(settings(), "https://zoom.us/j/1")
    assert issubclass(UnsupportedMeeting, ValueError)


# --------------------------------------------------------------------------- the event bus


def test_events_reach_every_listener_in_order():
    async def body():
        bus, seen = EventBus(), []
        bus.on(Event.AUDIO, lambda pcm, rate: seen.append(("sync", pcm, rate)))

        async def later(pcm, rate):
            seen.append(("async", pcm, rate))

        bus.on(Event.AUDIO, later)
        await bus.emit(Event.AUDIO, b"\x01\x02", 24000)
        assert seen == [("sync", b"\x01\x02", 24000), ("async", b"\x01\x02", 24000)]

    run(body())


def test_one_broken_listener_does_not_stop_the_others():
    async def body():
        bus, seen = EventBus(), []

        def boom(*_):
            raise RuntimeError("no")

        bus.on(Event.AUDIO, boom)
        bus.on(Event.AUDIO, lambda *a: seen.append(a))
        await bus.emit(Event.AUDIO, b"x", 24000)
        assert seen == [(b"x", 24000)]

    run(body())


def test_off_removes_a_listener():
    async def body():
        bus, seen = EventBus(), []
        fn = bus.on(Event.CHAT, lambda m: seen.append(m))
        bus.off(Event.CHAT, fn)
        await bus.emit(Event.CHAT, "ignored")
        assert seen == []

    run(body())


# --------------------------------------------------------------------------- state


def test_state_groups():
    assert State.JOINED.live and State.WAITING_ROOM.live and State.JOINING.live
    assert State.LEFT.final and State.FAILED.final
    assert not State.IDLE.live and not State.IDLE.final


def test_state_changes_are_announced_once():
    async def body():
        m = BrowserMeeting(settings())
        seen = []
        m.on(Event.STATE, lambda st: seen.append(st))
        await m._set_state(State.JOINING)
        await m._set_state(State.JOINING)  # unchanged: no second event
        await m._set_state(State.JOINED)
        assert seen == [State.JOINING, State.JOINED]

    run(body())


def test_a_participant_is_announced_once_and_updated_on_change():
    async def body():
        m = BrowserMeeting(settings())
        seen = []
        m.on(Event.PARTICIPANT, lambda p, joined: seen.append((p.name, joined)))
        await m._saw_participant(Participant("1", "Ada"))
        await m._saw_participant(Participant("1", "Ada"))       # identical: silent
        await m._saw_participant(Participant("1", "Ada Lovelace"))  # renamed: an update
        assert seen == [("Ada", True), ("Ada Lovelace", False)]

    run(body())


# --------------------------------------------------------------------------- the page wire format


def page_frame(stream_id: int, pcm: bytes) -> str:
    """Exactly what bridge.js hands the binding: one JSON message with base64 PCM."""
    return json.dumps({"type": "audio", "stream": stream_id, "pcm": base64.b64encode(pcm).decode()})


def test_stream_zero_is_the_mix_and_the_rest_are_people():
    async def body():
        m = BrowserMeeting(settings())
        mixed, per = [], []
        m.on(Event.AUDIO, lambda pcm, rate: mixed.append((pcm, rate)))
        m.on(Event.PARTICIPANT_AUDIO, lambda who, pcm, rate: per.append((who, pcm, rate)))

        await m._on_page_event(page_frame(0, b"\x11\x22\x33\x44"))
        await m._on_page_event(json.dumps({"type": "track", "stream_id": 7, "track_id": "track-abc"}))
        await m._on_page_event(page_frame(7, b"\x55\x66\x77\x88"))

        assert mixed == [(b"\x11\x22\x33\x44", 24000)]
        assert per == [("track-abc", b"\x55\x66\x77\x88", 24000)]

    run(body())


def test_audio_from_an_unannounced_stream_still_counts():
    async def body():
        """Audio can arrive before the track message; it must not be dropped."""
        m = BrowserMeeting(settings())
        per = []
        m.on(Event.PARTICIPANT_AUDIO, lambda who, pcm, rate: per.append(who))
        await m._on_page_event(page_frame(3, b"\x00\x01\x02\x03"))
        assert per == ["stream-3"]

    run(body())


def test_an_empty_or_corrupt_frame_is_ignored_rather_than_crashing():
    async def body():
        m = BrowserMeeting(settings())
        seen = []
        m.on(Event.AUDIO, lambda *a: seen.append(a))
        await m._on_page_event(json.dumps({"type": "audio", "stream": 0, "pcm": ""}))
        await m._on_page_event(json.dumps({"type": "audio", "stream": 0, "pcm": "!!!not base64!!!"}))
        assert seen == []

    run(body())


def test_a_track_that_ends_is_forgotten():
    async def body():
        m = BrowserMeeting(settings())
        await m._on_page_event(json.dumps({"type": "track", "stream_id": 2, "track_id": "t2"}))
        assert m._streams == {2: "t2"}
        await m._on_page_event(json.dumps({"type": "track_ended", "stream_id": 2}))
        assert m._streams == {}

    run(body())


def test_malformed_page_json_is_ignored():
    async def body():
        m = BrowserMeeting(settings())
        await m._on_page_event("{not json")  # must not raise

    run(body())


def test_chat_from_the_page_becomes_an_event():
    async def body():
        m = BrowserMeeting(settings())
        seen = []
        m.on(Event.CHAT, lambda msg: seen.append((msg.sender, msg.text)))
        await m._on_page_event(json.dumps({"type": "chat", "sender": "Ada", "text": "hello"}))
        assert seen == [("Ada", "hello")]

    run(body())


# --------------------------------------------------------------------------- the injected script


def test_the_page_script_is_configured_not_hardcoded():
    s = settings(sample_rate=24000, bot_name="Roger")
    js = BrowserMeeting(s)._bridge_js()
    assert "__ROGER_CFG__" not in js, "the config placeholder was left unsubstituted"
    assert '"rate": 24000' in js
    assert "getUserMedia" in js and "RTCPeerConnection" in js


def test_the_page_never_opens_a_socket_of_its_own():
    """Google Meet's CSP forbids connecting to 127.0.0.1, and the attempt crashes the renderer.

    Everything therefore crosses on the Playwright binding. A WebSocket reappearing in the page script
    would look fine in tests and fail only inside a real meeting, so it is checked here.
    """
    js = BrowserMeeting(settings())._bridge_js()
    code = "\n".join(ln for ln in js.splitlines() if not ln.strip().startswith("*"))
    assert "new WebSocket" not in code
    assert "__rogerSend" in code


def test_the_javascript_encoder_and_the_python_decoder_agree():
    """Run bridge.js's own encoder under Node and decode it the way BrowserMeeting does.

    This is the one seam where a silent disagreement -- endianness, clipping, base64 -- would cost a whole
    meeting and show up only as noise. Skipped when Node is not around.
    """
    import shutil as _shutil
    import subprocess

    node = _shutil.which("node")
    if not node:
        pytest.skip("node is not installed")

    from roger.meeting.browser import ASSETS

    src = (ASSETS / "bridge.js").read_text()

    def lift(name: str, until: str) -> str:
        head = "const " + name + " = "
        return src[src.index(head) + len(head) : src.index(until)]

    to_pcm16 = lift("toPcm16", ";\n  const fromPcm16")
    to_b64 = lift("b64", ";\n  const unb64")
    script = (
        "const toPcm16 = (" + to_pcm16 + ");"
        "const b64 = (" + to_b64 + ");"
        "const f = new Float32Array([0, 0.5, -0.5, 1.0, -1.0, 2.0, -2.0]);"
        "process.stdout.write(b64(toPcm16(f)));"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr

    pcm = base64.b64decode(out.stdout.strip())
    assert struct.unpack("<" + "h" * (len(pcm) // 2), pcm) == (0, 16383, -16384, 32767, -32768, 32767, -32768)


def test_the_page_script_ships_with_the_package():
    from roger.meeting.browser import ASSETS

    assert (ASSETS / "bridge.js").is_file()


# --------------------------------------------------------------------------- the interface itself


def test_both_providers_implement_the_whole_interface():
    for cls in (BrowserMeeting, AttendeeMeeting):
        assert issubclass(cls, MeetingClient)
        assert not getattr(cls, "__abstractmethods__", None), f"{cls.__name__} is still abstract"
        for method in ("join", "leave", "send_audio", "send_chat", "participants", "close"):
            assert callable(getattr(cls, method)), f"{cls.__name__}.{method}"


def test_only_a_provider_that_is_dialled_into_mounts_a_route():
    assert BrowserMeeting.ws_path is None       # nothing reaches in; the page is driven over CDP
    assert AttendeeMeeting.ws_path == "/ws/attendee"
