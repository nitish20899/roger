"""
Attention manager: decides whether an utterance in a multi-party meeting is directed at the bot.

Human-like rules:
  * Saying the bot's name always gets its attention (and opens a conversation window).
  * While a conversation with the bot is open, people do not need to repeat its name. A fast
    classifier looks at the last few turns and decides whether each new utterance is for the bot,
    for someone else, or just people talking among themselves.
  * The window stays open as long as the exchange continues, closes on a dismissal ("thanks Roger",
    "that's all"), when people clearly turn to each other, or after a quiet period.
  * A mute phrase ("Roger, mute") closes it hard until the name is used again.

Design for latency: the caller runs `decide()` in parallel with drafting the answer and releases audio
only when the decision is YES, so the classifier costs no extra time on the happy path.

Everything here is deterministic except `Classifier.classify`, which is injectable so the state machine
can be unit-tested without a model.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

log = logging.getLogger("roger.attention")


@dataclass
class Turn:
    speaker: str  # participant name or "Someone"
    text: str
    is_bot: bool = False
    t: float = field(default_factory=time.time)


@dataclass
class Decision:
    addressed: bool
    confidence: float
    reason: str
    source: str  # "wake_word" | "mute" | "dismissal" | "idle" | "rule" | "classifier" | "fallback"
    engage: Optional[bool] = None  # True opens/extends the window, False closes it, None leaves it


ClassifyFn = Callable[[str, list[Turn], Turn], Awaitable[dict]]


class Attention:
    def __init__(
        self,
        bot_first_name: str,
        wake_words: list[str],
        classify: Optional[ClassifyFn] = None,
        window_s: float = 30.0,
        max_unaddressed: int = 3,
        threshold: float = 0.6,
    ) -> None:
        self.name = bot_first_name
        self.wake = [w.lower() for w in wake_words if w.strip()]
        self.classify = classify
        self.window_s = window_s
        self.max_unaddressed = max_unaddressed
        self.threshold = threshold

        self.engaged_until = 0.0
        self.muted = False
        self.last_asker: Optional[str] = None
        self.last_bot_turn_at = 0.0
        self.unaddressed_streak = 0
        self.history: list[Turn] = []
        self.decisions: list[dict] = []

    # ------------------------------------------------------------------ helpers
    def _wake_re(self) -> re.Pattern:
        return re.compile(r"\b(" + "|".join(re.escape(w) for w in self.wake) + r")\b", re.I)

    def has_wake_word(self, text: str) -> bool:
        return bool(self.wake) and bool(self._wake_re().search(text))

    def _mute_phrase(self, text: str) -> bool:
        low = text.lower()
        return self.has_wake_word(text) and bool(re.search(r"\b(mute|go quiet|stop listening|be quiet|shut up|stay out)\b", low))

    def _dismissal(self, text: str) -> bool:
        low = text.lower().strip(" .!,")
        pats = [
            r"^(ok(ay)?|alright|great|cool|perfect|got it|thanks?( you)?|thank you|cheers)(,? " + re.escape(self.name.lower()) + r")?$",
            r"\bthat'?s (all|it|everything)\b",
            r"\bthanks?,? " + re.escape(self.name.lower()) + r"\b",
            r"\b(let'?s|we can) move on\b",
            r"\bback to (us|the agenda|the meeting)\b",
        ]
        return any(re.search(p, low) for p in pats)

    def engaged(self) -> bool:
        return not self.muted and time.time() < self.engaged_until

    def open_window(self, asker: Optional[str] = None) -> None:
        self.engaged_until = time.time() + self.window_s
        self.unaddressed_streak = 0
        if asker:
            self.last_asker = asker

    def close_window(self) -> None:
        self.engaged_until = 0.0
        self.unaddressed_streak = 0

    def note_bot_turn(self, text: str) -> None:
        self.last_bot_turn_at = time.time()
        self.history.append(Turn("bot", text, is_bot=True))
        self.history = self.history[-40:]
        self.open_window(self.last_asker)  # answering keeps the conversation open

    def _record(self, turn: Turn, d: Decision) -> Decision:
        self.decisions.append({"t": turn.t, "speaker": turn.speaker, "text": turn.text, **d.__dict__})
        self.decisions = self.decisions[-200:]
        log.info("attention: %s conf=%.2f via %s (%s) | %s: %s", "YES" if d.addressed else "no ", d.confidence, d.source, d.reason, turn.speaker, turn.text[:80])
        return d

    def fallback_guess(self, speaker: str, text: str) -> bool:
        """Conservative rule-based guess used when the classifier is unavailable or too slow."""
        low = text.strip().lower()
        words = low.split()
        second_person = bool(re.search(r"\b(you|your|yours)\b", low))
        question = "?" in text or bool(re.match(r"^(what|why|how|when|where|which|who|can|could|would|will|do|does|did|is|are|tell|explain)\b", low))
        same_asker = self.last_asker is not None and speaker == self.last_asker and speaker != "Someone"
        recent_bot = time.time() - self.last_bot_turn_at < 15
        return (second_person and recent_bot) or (same_asker and question) or (question and recent_bot and len(words) >= 3)

    # ------------------------------------------------------------------ main entry
    async def decide(self, speaker: str, text: str) -> Decision:
        turn = Turn(speaker, text)
        self.history.append(turn)
        self.history = self.history[-40:]
        text_stripped = text.strip()
        words = text_stripped.split()

        # 1. hard controls
        if self._mute_phrase(text):
            self.muted = True
            self.close_window()
            return self._record(turn, Decision(False, 1.0, "mute phrase", "mute", engage=False))
        if self.has_wake_word(text):
            self.muted = False
            self.open_window(speaker)
            if self._dismissal(text) and len(words) <= 6:
                self.close_window()
                return self._record(turn, Decision(False, 0.95, "thanks/dismissal with name", "dismissal", engage=False))
            return self._record(turn, Decision(True, 1.0, "name mentioned", "wake_word", engage=True))
        if self.muted:
            return self._record(turn, Decision(False, 1.0, "muted until name is used", "mute"))

        # 2. nothing open: people are talking among themselves
        if not self.engaged():
            return self._record(turn, Decision(False, 0.95, "no open conversation with the bot", "idle"))

        # 3. conversation open: cheap rules first
        if self._dismissal(text) and len(words) <= 8:
            self.close_window()
            return self._record(turn, Decision(False, 0.9, "dismissal", "dismissal", engage=False))
        if len(words) <= 1:
            return self._record(turn, Decision(False, 0.8, "one-word utterance", "rule"))

        # 4. classifier with context (falls back to conservative rules if unavailable)
        if self.classify is not None:
            try:
                ctx = [h for h in self.history[:-1] if time.time() - h.t < 120][-8:]
                res = await self.classify(self.name, ctx, turn)
                addressed = bool(res.get("addressed"))
                conf = float(res.get("confidence", 0.5))
                reason = str(res.get("reason", ""))[:120]
                if addressed and conf < self.threshold:
                    addressed = False
                    reason = f"low confidence ({conf:.2f}): {reason}"
                if res.get("closes_conversation"):
                    self.close_window()
                    return self._record(turn, Decision(False, conf, reason or "people turned to each other", "classifier", engage=False))
                if addressed:
                    self.open_window(speaker)
                    return self._record(turn, Decision(True, conf, reason, "classifier", engage=True))
                self.unaddressed_streak += 1
                if self.unaddressed_streak >= self.max_unaddressed:
                    self.close_window()
                    return self._record(turn, Decision(False, conf, f"{reason}; window closed after {self.max_unaddressed} unaddressed turns", "classifier", engage=False))
                return self._record(turn, Decision(False, conf, reason, "classifier"))
            except Exception as e:  # never let the classifier break the meeting
                log.warning("attention: classifier failed (%s); using fallback rules", e)

        # 5. fallback rules while engaged: questions and second-person cues from the last asker
        if self.fallback_guess(speaker, text_stripped):
            self.open_window(speaker)
            return self._record(turn, Decision(True, 0.65, "follow-up cue (fallback rule)", "fallback", engage=True))
        self.unaddressed_streak += 1
        if self.unaddressed_streak >= self.max_unaddressed:
            self.close_window()
        return self._record(turn, Decision(False, 0.6, "no follow-up cue (fallback rule)", "fallback"))


# ---------------------------------------------------------------------- classifier prompt (shared by providers)

CLASSIFIER_SYSTEM = """You decide whether the newest utterance in a live multi-person meeting is directed at an AI teammate named {name}.
The conversation window with {name} is open: {name} was just addressed or just spoke. In an open window people do NOT repeat the name.

Default rules, in order:
1. If the utterance names another person ("Alice, ...", "Bob, what do you think?") or answers another person, it is NOT for {name}.
2. Otherwise, a question, request, or instruction ("what does it cost?", "can you post the link?", "tell us what changed", "why?",
   "say that again?") IS for {name}. So is a reaction to what {name} said ("that's too slow, no?", "that doesn't sound right").
3. Pure acknowledgements ("ok", "cool", "makes sense", "I agree"), side chatter, apologies for being late, and statements about
   other people are NOT for {name}.
4. closes_conversation is true only when the group has clearly turned to each other or to a new topic ("anyway, about the budget",
   "ok team, next item", "so Hal, how was the call?").

Examples (window open, {name} just answered a question about a port):
- "And can we change that from the env file?" -> addressed true
- "Why that port though?" -> addressed true
- "Alice, did you update the firewall for that?" -> addressed false
- "Ok cool." -> addressed false
- "Anyway, about the budget review next week." -> addressed false, closes_conversation true

Respond with JSON only: {{"addressed": true|false, "confidence": 0.0-1.0, "closes_conversation": true|false, "reason": "<=8 words"}}"""


def classifier_user_message(name: str, ctx: list[Turn], turn: Turn) -> str:
    lines = []
    for h in ctx:
        who = name if h.is_bot else h.speaker
        lines.append(f"{who}: {h.text}")
    return "Recent turns (oldest first):\n" + ("\n".join(lines) or "(none)") + f"\n\nNewest utterance from {turn.speaker}: \"{turn.text}\"\n\nIs it directed at {name}?"


def parse_classifier_json(raw: str) -> dict:
    raw = raw.strip()
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {"addressed": False, "confidence": 0.0, "reason": "unparseable"}
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"addressed": False, "confidence": 0.0, "reason": "unparseable"}
    return {
        "addressed": bool(d.get("addressed", False)),
        "confidence": max(0.0, min(1.0, float(d.get("confidence", 0.5)))),
        "closes_conversation": bool(d.get("closes_conversation", False)),
        "reason": str(d.get("reason", ""))[:160],
    }


def make_openai_classifier(api_key: str, model: str = "gpt-4.1-nano") -> ClassifyFn:
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key)

    async def classify(name: str, ctx: list[Turn], turn: Turn) -> dict:
        extra: dict = {"temperature": 0}
        if model.startswith(("gpt-5", "o")):  # reasoning models: no temperature; keep reasoning off for latency
            extra = {"reasoning_effort": os.getenv("CLASSIFIER_REASONING", "none")}
        r = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": CLASSIFIER_SYSTEM.format(name=name)},
                {"role": "user", "content": classifier_user_message(name, ctx, turn)},
            ],
            response_format={"type": "json_object"},
            max_completion_tokens=48 if not model.startswith(("gpt-5", "o")) else 200,
            **extra,
        )
        return parse_classifier_json(r.choices[0].message.content or "")

    return classify


def make_anthropic_classifier(api_key: Optional[str], model: str = "claude-haiku-4-5") -> ClassifyFn:
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=api_key) if api_key else anthropic.AsyncAnthropic()

    async def classify(name: str, ctx: list[Turn], turn: Turn) -> dict:
        r = await client.messages.create(
            model=model,
            max_tokens=80,
            system=[{"type": "text", "text": CLASSIFIER_SYSTEM.format(name=name), "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": classifier_user_message(name, ctx, turn)}],
        )
        text = "".join(b.text for b in r.content if b.type == "text")
        return parse_classifier_json(text)

    return classify
