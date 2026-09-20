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
    """Click the first visible candidate. Returns whether anything was clicked.

    A meeting page will happily drop a coach-mark over the button you need -- Google Meet puts a "Got it"
    bubble on top of "Ask to join" -- and a normal click then fails on actionability, not on the element
    being absent. So a blocked click falls back to a forced one and finally to dispatching the event
    directly, which no overlay can intercept.
    """
    loc = await first_visible(page, selectors, timeout_ms)
    if loc is None:
        return False
    for attempt, click in enumerate((
        lambda: loc.click(timeout=max(2000, timeout_ms)),
        lambda: loc.click(timeout=2000, force=True),
        lambda: loc.evaluate("el => el.click()"),
    )):
        try:
            await click()
            log.debug("clicked %s%s", what or selectors[0], "" if attempt == 0 else f" (fallback {attempt})")
            return True
        except Exception as e:
            last = e
    log.debug("could not click %s: %s", what or selectors[0], last)
    return False


async def dismiss_all(page: Any, selectors: Sequence[str], rounds: int = 2) -> int:
    """Clear away banners and coach-marks. They appear on their own schedule, so this runs more than once."""
    cleared = 0
    for _ in range(rounds):
        hit = False
        for sel in selectors:
            if await click_any(page, [sel], timeout_ms=400, what=sel):
                cleared += 1
                hit = True
        if not hit:
            break
    return cleared


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
