"""In-memory, speaker-attributed transcript of the meeting."""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class Utterance:
    speaker: str
    text: str
    t: float = field(default_factory=time.time)


class Transcript:
    def __init__(self) -> None:
        self.items: list[Utterance] = []

    def add(self, speaker: str, text: str) -> Utterance:
        u = Utterance(speaker, text)
        self.items.append(u)
        return u

    def window(self, seconds: float = 180, max_items: int = 30) -> str:
        """The last few minutes as ``Speaker: text`` lines, oldest first."""
        cutoff = time.time() - seconds
        recent = [u for u in self.items if u.t >= cutoff][-max_items:]
        return "\n".join(f"{u.speaker}: {u.text}" for u in recent) or "(nothing yet)"
