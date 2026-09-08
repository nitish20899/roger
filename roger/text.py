"""Text helpers: sentence chunking for streaming TTS, and normalisation for echo detection."""
from __future__ import annotations

import re

_SENT_END = re.compile(r"(.+?[.!?][\"')\]]?)(?:\s+|$)")
_ABBREV = re.compile(r"\b(e\.g|i\.e|vs|etc|Dr|Mr|Ms)\.$")


def take_sentences(buf: str) -> tuple[list[str], str]:
    """Split completed sentences off the front of ``buf``. Returns ``(sentences, remainder)``.

    Very short fragments, abbreviations and numbered items ("1.") are not treated as sentence ends,
    so the voice does not pause in the middle of a thought.
    """
    out: list[str] = []
    pos = 0  # end of the last emitted sentence
    for m in _SENT_END.finditer(buf):
        cand = buf[pos : m.end(1)].strip()  # everything pending up to this terminator
        if len(cand) < 8 or _ABBREV.search(cand) or re.search(r"\d\.$", cand):
            continue  # not a real sentence end yet: keep accumulating
        out.append(cand)
        pos = m.end()
    return out, buf[pos:]


def speech_clean(text: str) -> str:
    """Strip markdown that would be read aloud literally."""
    text = re.sub(r"`+", "", text)
    text = re.sub(r"[*_#>]+", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def norm_text(text: str) -> str:
    """Lowercase alphanumerics only, single-spaced: the form used to compare heard text with spoken text."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", text.lower())).strip()
