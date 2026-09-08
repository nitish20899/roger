"""Deterministic tests for the attention state machine (the classifier is mocked)."""
import asyncio
import time

from roger.attention import Attention


def make(classify=None, **kw) -> Attention:
    return Attention("Roger", ["roger", "rodger"], classify=classify, window_s=30, **kw)


async def yes(name, ctx, turn):
    return {"addressed": True, "confidence": 0.9, "reason": "follow-up"}


async def no(name, ctx, turn):
    return {"addressed": False, "confidence": 0.9, "reason": "people talking to each other"}


async def closes(name, ctx, turn):
    return {"addressed": False, "confidence": 0.9, "closes_conversation": True, "reason": "new topic"}


async def boom(name, ctx, turn):
    raise RuntimeError("model down")


def run(coro):
    return asyncio.run(coro)


def test_idle_ignores_everything_without_the_name():
    a = make(classify=yes)
    d = run(a.decide("Alice", "what do you think about the port?"))
    assert not d.addressed and d.source == "idle"


def test_wake_word_opens_window_and_follow_up_is_classified():
    async def go():
        a = make(classify=yes)
        d = await a.decide("Alice", "Roger, which port does the bridge use?")
        assert d.addressed and d.source == "wake_word"
        a.note_bot_turn("It listens on port eight seven eight seven.")
        d = await a.decide("Alice", "and can you change that?")
        assert d.addressed and d.source == "classifier"

    run(go())


def test_three_unaddressed_turns_close_the_window():
    async def go():
        b = make(classify=no)
        await b.decide("Bob", "Roger, hello")
        for _ in range(3):
            d = await b.decide("Carol", "so Bob, did you finish the report?")
            assert not d.addressed
        assert not b.engaged()
        d = await b.decide("Carol", "what about the budget?")
        assert d.source == "idle"

    run(go())


def test_dismissal_and_mute():
    async def go():
        c = make(classify=yes)
        await c.decide("Dan", "Roger, summarize the plan")
        d = await c.decide("Dan", "thanks Roger")
        assert not d.addressed and d.source == "dismissal" and not c.engaged()
        d = await c.decide("Dan", "Roger, mute for a bit")
        assert not d.addressed and c.muted
        d = await c.decide("Dan", "Roger?")
        assert d.addressed and not c.muted

    run(go())


def test_classifier_failure_falls_back_to_rules():
    async def go():
        e = make(classify=boom)
        await e.decide("Eve", "Roger, what's the latency?")
        e.note_bot_turn("About one second.")
        d = await e.decide("Eve", "can you make it faster?")
        assert d.addressed and d.source == "fallback"
        d = await e.decide("Frank", "anyway, lunch?")
        assert not d.addressed

    run(go())


def test_classifier_can_close_the_conversation():
    async def go():
        f = make(classify=closes)
        await f.decide("Gus", "Roger, are you there?")
        d = await f.decide("Gus", "ok team, next agenda item")
        assert not d.addressed and not f.engaged()

    run(go())


def test_window_expires():
    async def go():
        g = make(classify=yes)
        await g.decide("Hal", "Roger, ping")
        g.engaged_until = time.time() - 1
        d = await g.decide("Hal", "and what about that?")
        assert d.source == "idle"

    run(go())
