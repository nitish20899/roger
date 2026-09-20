"""GPT-Live wire handling: turn reassembly, append limits and the session it opens."""
import asyncio
import json

import pytest

from roger.config import Settings
from roger.live import APPEND_MAX_CHARS, LiveSession, _TranscriptStream


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    for k in ("VOICE", "LIVE_MODEL", "GREETING", "PROJECT_DIR", "CLAUDE_SESSION_ID", "AUDIO_RATE", "OUTPUT_BUFFER_MS"):
        monkeypatch.delenv(k, raising=False)
    return Settings.from_env()


BACKEND = {"model": "gpt-5.6-luna", "instructions": "be useful", "tools": [{"type": "web_search"}], "tool_choice": "auto"}


async def _noop(*_a, **_k):
    return None


async def _tool(_name, _args):
    return "ok"


def session(settings, history=None, backend=None, on_tool=_tool, on_audio=_noop, on_said=_noop):
    return LiveSession(
        settings, on_audio, _noop, on_said, on_tool,
        lambda: "be brief", lambda: history or [], lambda: backend or BACKEND,
    )


def collector():
    turns = []

    async def on_turn(text):
        turns.append(text)

    return turns, on_turn


# --------------------------------------------------------------- transcript reassembly


def test_fragments_join_into_one_turn():
    turns, on_turn = collector()

    async def go():
        s = _TranscriptStream(on_turn)
        await s.add("What does ", 0, 500)
        await s.add("the port default to?", 500, 1200)
        assert turns == []  # no done event: nothing is emitted until the turn actually ends
        await s.flush()

    run(go())
    assert turns == ["What does the port default to?"]


def test_a_gap_in_the_timeline_ends_the_turn():
    turns, on_turn = collector()

    async def go():
        s = _TranscriptStream(on_turn, gap_ms=900)
        await s.add("Morning everyone.", 0, 1000)
        await s.add("Right, shall we start?", 4000, 5000)  # 3 s of silence in between
        assert turns == ["Morning everyone."]
        await s.flush()

    run(go())
    assert turns == ["Morning everyone.", "Right, shall we start?"]


def test_flush_ignores_an_empty_buffer():
    turns, on_turn = collector()

    async def go():
        s = _TranscriptStream(on_turn)
        await s.flush()
        await s.add("   ", None, None)
        await s.flush()

    run(go())
    assert turns == []


# --------------------------------------------------------------- session configuration


def test_session_start_matches_the_live_schema(settings):
    cfg = session(settings)._session_config()
    assert cfg["model"] == "gpt-live-1"
    assert cfg["audio"]["format"] == {"type": "audio/pcm", "rate": 24000}  # GPT-Live's native rate
    assert cfg["audio"]["output"]["voice"] == "cedar"  # the default male voice
    assert cfg["delegation"]["type"] == "responses"  # the API runs the backend and passes it the conversation
    assert cfg["delegation"]["responses"]["model"] == "gpt-5.6-luna"
    assert {"type": "web_search"} in cfg["delegation"]["responses"]["tools"]
    assert "input" not in cfg  # no briefing yet
    json.dumps(cfg)  # must be serialisable as one text frame


def test_the_briefing_is_seeded_as_history(settings):
    history = [{"role": "developer", "content": [{"type": "input_text", "text": "the project is Roger"}]}]
    assert session(settings, history)._session_config()["input"] == history


def test_appends_stay_inside_the_token_limit(settings):
    live = session(settings)
    assert live._clip("a  b\n c") == "a b c"
    clipped = live._clip("word " * 2000)
    assert len(clipped) <= APPEND_MAX_CHARS and clipped.endswith("…")


def test_event_ids_are_unique(settings):
    live = session(settings)
    assert live._event_id("comment") != live._event_id("comment")


# --------------------------------------------------------------- audio framing


def test_audio_is_buffered_and_stays_sample_aligned(settings):
    live = session(settings)
    sent = []

    async def capture(event):
        sent.append(event)
        return True

    async def go():
        live._send = capture
        live.ready.set()
        await live.feed(b"\x01\x02" * 100)  # under one frame: held back
        assert sent == []
        await live.feed(b"\x01\x02" * (live.frame // 2) + b"\x07")  # over one frame, with a stray odd byte

    run(go())
    assert len(sent) == 1 and sent[0]["type"] == "session.input_audio.append"
    assert bytes(live.buf) == b"\x07"  # the odd byte waits for its pair


def test_nothing_is_sent_before_the_session_is_ready(settings):
    live = session(settings)
    sent = []

    async def capture(event):
        sent.append(event)
        return True

    async def go():
        live._send = capture
        await live.feed(b"\x01\x02" * 4000)

    run(go())
    assert sent == []


# --------------------------------------------------------------- server events


def test_output_audio_reaches_the_speaker(settings):
    import base64

    played = []

    async def on_audio(pcm):
        played.append(pcm)

    live = session(settings, on_audio=on_audio)
    blob = b"\x01\x02" * 8

    async def go():
        await live._handle({"type": "session.output_audio.delta", "delta": base64.b64encode(blob).decode()})

    run(go())
    assert played == [blob]



def test_a_function_call_is_run_and_its_result_returned(settings):
    """The documented Responses flow: collect the finished call, return its output, then continue."""
    ran, sent = [], []

    async def on_tool(name, args):
        ran.append((name, args))
        return "roger/config.py:12: PORT = 8787"

    live = session(settings, on_tool=on_tool)

    async def capture(event):
        sent.append(event)
        return True

    async def go():
        live._send = capture
        live.ready.set()
        # a lifecycle event alone must not trigger anything: only the finished item carries the call
        await live._handle({"type": "response.event", "delegation_id": "d1", "event": {"type": "response.created"}})
        await live._handle({"type": "response.event", "delegation_id": "d1", "event": {
            "type": "response.output_item.done",
            "item": {"type": "function_call", "call_id": "call_1", "name": "search_repo", "arguments": '{"query":"PORT"}'},
        }})
        assert ran == [], "must wait for response.completed before running the batch"
        await live._handle({"type": "response.event", "delegation_id": "d1", "event": {"type": "response.completed"}})
        await asyncio.sleep(0.05)

    run(go())
    assert ran == [("search_repo", '{"query":"PORT"}')]
    assert [e["type"] for e in sent] == ["response.item.create", "response.create"]
    assert sent[0]["item"] == {"type": "function_call_output", "call_id": "call_1", "output": "roger/config.py:12: PORT = 8787"}
    assert live.tool_calls == 1


def test_a_responses_delegation_is_counted_not_answered(settings):
    """With Responses delegation the backend answers; Roger only observes the delegation."""
    live = session(settings)

    async def go():
        await live._handle({"type": "session.delegation.created", "offset_ms": 10,
                            "delegation": {"id": "item_1", "target": "responses", "type": "delegation", "response_id": "resp_1"}})

    run(go())
    assert live.delegations == 1


# --------------------------------------------------------------- speaking state


def test_speaking_follows_energy_and_silence_is_not_transmitted(monkeypatch):
    """GPT-Live streams silence continuously, so arriving audio must not mean the bot is talking.

    Getting this wrong holds the echo gate shut and pushes a permanent stream of silence at the meeting.
    """
    import struct

    from roger.speaker import Speaker

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    sp = Speaker(Settings.from_env())
    sent = []
    samples = sp.frame // 2
    quiet = struct.pack("<%dh" % samples, *([0] * samples))
    loud = struct.pack("<%dh" % samples, *([6000, -6000] * (samples // 2)))

    async def capture(chunk):
        sent.append(chunk)

    async def go():
        sp._send_pcm = capture
        sp.start()
        for _ in range(6):  # a quiet stream: nothing should go out at all
            await sp.feed(quiet)
        await asyncio.sleep(0.2)
        assert not sp.speaking and not sp.echo_window()
        assert sent == [], "silence must not be transmitted"

        for _ in range(sp.prebuffer_frames + 3):
            await sp.feed(loud)
        await asyncio.sleep(0.3)
        assert sp.speaking and sp.echo_window()
        assert len(sent) >= sp.prebuffer_frames, "the turn should be released once the cushion is ready"
        assert all(len(c) == sp.frame for c in sent), "only whole 100 ms frames may leave"

    run(go())
    assert sp.voiced_seconds() > 0


def test_a_turn_is_held_back_until_the_cushion_is_ready(monkeypatch):
    """The first frames of a turn are buffered, not dribbled out, so the far end starts with slack."""
    import struct

    from roger.speaker import Speaker

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    sp = Speaker(Settings.from_env())
    sent = []
    samples = sp.frame // 2
    loud = struct.pack("<%dh" % samples, *([6000, -6000] * (samples // 2)))

    async def capture(chunk):
        sent.append(chunk)

    async def go():
        sp._send_pcm = capture
        sp.start()
        await sp.feed(loud)  # one frame of speech: not enough cushion yet
        await asyncio.sleep(0.15)
        assert sent == [], "a single frame must not be released on its own"
        for _ in range(sp.prebuffer_frames):
            await sp.feed(loud)
        await asyncio.sleep(0.25)
        assert len(sent) >= sp.prebuffer_frames

    run(go())


def test_the_cushion_is_made_of_speech_not_silence(monkeypatch):
    """Quiet run-up frames must not count toward the cushion.

    They did once, so the first voiced frame released the turn: the far end got a cushion of silence,
    ran dry on reaching the real audio, and every burst gap after that came out as a click.
    """
    import struct

    from roger.speaker import Speaker

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    sp = Speaker(Settings.from_env())
    sent = []
    samples = sp.frame // 2
    quiet = struct.pack("<%dh" % samples, *([0] * samples))
    loud = struct.pack("<%dh" % samples, *([6000, -6000] * (samples // 2)))

    async def capture(chunk):
        sent.append(chunk)

    async def go():
        sp._send_pcm = capture
        sp.start()
        for _ in range(4):  # run-up silence: trimmed to the pre-roll, never sent
            await sp.feed(quiet)
        await asyncio.sleep(0.15)
        assert sent == []

        await sp.feed(loud)  # first frame of speech must NOT release the turn
        await asyncio.sleep(0.15)
        assert sent == [], "released on one voiced frame: the cushion would be mostly silence"

        for _ in range(sp.prebuffer_frames):
            await sp.feed(loud)
        await asyncio.sleep(0.4)
        voiced_out = sum(1 for c in sent if max(abs(v) for v in struct.unpack("<%dh" % (len(c) // 2), c)) > 1000)
        assert voiced_out >= sp.prebuffer_frames, "a full cushion of real speech should have gone out"

    run(go())
