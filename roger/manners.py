"""Meeting manners the voice model cannot enforce on its own.

GPT-Live decides for itself when it is being spoken to, and its conversation prompt carries the rules.
There used to be a second opinion here -- a small model that judged every utterance and could cut the
bot off if it answered something not addressed to it. Across live meetings it made 110 judgements and
intervened zero times, so it was costing a model call per utterance to do nothing, and it is gone.

What is left is the one thing a prompt cannot do reliably: **holding**. "Hold on, Roger" has to work
every single time, instantly, with no model in the loop, and it has to keep working until someone says
the name again. That is a handful of regexes, and it belongs in code.
"""
from __future__ import annotations

import re

# Asking the bot to stop. Deliberately generous: this is the one command that must never be missed.
HOLD = re.compile(
    r"\b(mute|go quiet|stop listening|be quiet|quiet for|shut up|stay out|butt out"
    r"|hold on|hold up|hang on|wait|one sec|one second|a sec|a minute|a moment"
    r"|not now|later|stand by|stay back|pause|let us talk|let me talk|we'?ll come back)\b",
    re.I,
)


class Manners:
    """Tracks whether the bot has been told to hold, and when it may speak again."""

    def __init__(self, wake_words: list[str]) -> None:
        self.wake = [w.strip().lower() for w in wake_words if w.strip()]
        self._named = re.compile(r"\b(" + "|".join(re.escape(w) for w in self.wake) + r")\b", re.I) if self.wake else None
        self.holding = False

    def named(self, text: str) -> bool:
        """Was the bot addressed by name? Its name is the only thing that lifts a hold."""
        return bool(self._named and self._named.search(text))

    def is_hold(self, text: str) -> bool:
        """"Hold on, Roger" / "Roger, mute": the name plus an instruction to stop."""
        return self.named(text) and bool(HOLD.search(text))

    def heard(self, text: str) -> str | None:
        """Feed one finished utterance. Returns ``"hold"``, ``"resume"`` or ``None``.

        Order matters: a hold phrase contains the name, so it must be checked before the name alone is
        taken as a call to come back.
        """
        if self.is_hold(text):
            was, self.holding = self.holding, True
            return None if was else "hold"
        if self.holding and self.named(text):
            self.holding = False
            return "resume"
        return None
