"""Driving a web UI that was never meant to be driven.

Google and Microsoft reword buttons and reshuffle DOM attributes without warning, so nothing here looks
for one selector. Every action takes a *list* of candidates, oldest-reliable last, and uses the first one
that is actually on screen. When a join breaks after a redesign, the fix is usually one more string in a
list in ``platforms/``, not new logic.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Sequence

log = logging.getLogger("roger.meeting.dom")

SHORT_MS = 1500


async def first_visible(page: Any, selectors: Sequence[str], timeout_ms: int = SHORT_MS) -> Any | None:
    """The first of ``selectors`` that is visible, as a Locator, or ``None``."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=timeout_ms)
            return loc
        except Exception:
            continue
    return None


async def click_any(page: Any, selectors: Sequence[str], timeout_ms: int = SHORT_MS, what: str = "") -> bool:
    """Click the first visible candidate. Returns whether anything was clicked."""
    loc = await first_visible(page, selectors, timeout_ms)
    if loc is None:
        return False
    try:
        await loc.click(timeout=timeout_ms * 2)
        if what:
            log.debug("clicked %s", what)
        return True
    except Exception as e:
        log.debug("clicking %s failed: %s", what or selectors[0], e)
        return False


async def fill_any(page: Any, selectors: Sequence[str], text: str, timeout_ms: int = SHORT_MS) -> bool:
    loc = await first_visible(page, selectors, timeout_ms)
    if loc is None:
        return False
    try:
        await loc.fill(text, timeout=timeout_ms * 2)
        return True
    except Exception:
        try:
            await loc.click(timeout=timeout_ms)
            await page.keyboard.type(text, delay=20)
            return True
        except Exception:
            return False


async def any_visible(page: Any, selectors: Sequence[str], timeout_ms: int = 400) -> bool:
    return await first_visible(page, selectors, timeout_ms) is not None


async def has_text(page: Any, needles: Iterable[str]) -> bool:
    """Is any of this text on the page? Used for the states that have no stable element."""
    try:
        body = (await page.inner_text("body", timeout=1500)).lower()
    except Exception:
        return False
    return any(n.lower() in body for n in needles)


async def texts(page: Any, selector: str, limit: int = 60) -> list[str]:
    """Trimmed text of every match, for reading participant and chat lists."""
    try:
        found = await page.locator(selector).all_inner_texts()
    except Exception:
        return []
    out: list[str] = []
    for t in found[:limit]:
        t = " ".join(t.split())
        if t:
            out.append(t)
    return out
