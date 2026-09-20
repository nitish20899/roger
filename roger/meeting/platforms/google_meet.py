"""Google Meet.

The join is the same one a person does: open the link, type a name if asked, turn the camera off, press
"Ask to join", then wait to be admitted. Nothing here touches audio -- ``browser.py`` has already replaced
the microphone by the time any of this runs.
"""
from __future__ import annotations

import logging
from typing import Any

from ..base import ChatMessage, Participant, State
from ..browser import Platform, platform
from ..dom import click_any, fill_any, first_visible, has_text, texts

log = logging.getLogger("roger.meeting.meet")

NAME_INPUT = ['input[aria-label="Your name"]', 'input[placeholder="Your name"]', 'input[type="text"][aria-label*="name" i]']
JOIN_BUTTON = [
    'button:has-text("Ask to join")', 'button:has-text("Join now")', 'button:has-text("Join anyway")',
    '[role="button"]:has-text("Ask to join")', '[role="button"]:has-text("Join now")',
]
CAMERA_OFF = ['button[aria-label*="Turn off camera" i]', 'div[role="button"][aria-label*="Turn off camera" i]', 'button[data-is-muted="false"][aria-label*="camera" i]']
LEAVE_BUTTON = ['button[aria-label*="Leave call" i]', 'button[aria-label*="Leave the call" i]', '[data-tooltip*="Leave call" i]']
DISMISS = [
    'button:has-text("Got it")', 'button:has-text("Dismiss")', 'button:has-text("Continue without microphone")',
    'button:has-text("Accept all")', 'button:has-text("Reject all")', 'button:has-text("Use without an account")',
]
CHAT_TOGGLE = ['button[aria-label*="Chat with everyone" i]', 'button[aria-label*="Open chat" i]', '[data-panel-id="2"]']
CHAT_INPUT = ['textarea[aria-label*="Send a message" i]', 'textarea[placeholder*="Send a message" i]', 'textarea[aria-label*="message" i]']
CHAT_SEND = ['button[aria-label*="Send a message" i]', 'button[aria-label="Send message"]']
PEOPLE_TOGGLE = ['button[aria-label*="People" i]', '[data-panel-id="1"]']
PEOPLE_ROW = '[role="listitem"][data-participant-id], div[data-participant-id]'
# Meet marks the talking tile rather than naming the speaker anywhere, so the name comes off that tile.
SPEAKING_TILE = [
    '[data-participant-id][class*="speaking"]',
    'div[data-participant-id]:has([class*="IisKdb"])',
    '[role="listitem"][aria-label*="is speaking" i]',
]
TILE_NAME = '[data-self-name], [class*="participant-name"], [class*="dwSJ2e"]'

WAITING = ("asking to be let in", "you'll join when someone", "waiting for the host", "ask to join")
REJECTED = ("you can't join this", "no one responded", "denied your request", "you were removed", "the call ended", "you've left the meeting")


@platform
class GoogleMeet(Platform):
    name = "google-meet"
    hosts = ("meet.google.com",)

    def __init__(self) -> None:
        self._chat_open = False
        self._people_open = False

    async def prepare(self, page: Any) -> None:
        for sel in DISMISS:
            await click_any(page, [sel], timeout_ms=600, what=sel)

    async def join(self, page: Any, url: str, bot_name: str) -> None:
        # The name field only appears for a guest; a signed-in profile skips straight to the join button.
        if await fill_any(page, NAME_INPUT, bot_name, timeout_ms=6000):
            log.info("joining as a guest called %r", bot_name)
        else:
            log.info("joining with the browser profile's signed-in account")
        await click_any(page, CAMERA_OFF, timeout_ms=2500, what="camera off")
        if not await click_any(page, JOIN_BUTTON, timeout_ms=15000, what="join"):
            raise RuntimeError("Google Meet: could not find the join button (the page may have changed, or the link is wrong)")
        log.info("asked to join; waiting to be admitted")

    async def poll_state(self, page: Any) -> State | None:
        if await first_visible(page, LEAVE_BUTTON, timeout_ms=600) is not None:
            return State.JOINED
        if await has_text(page, REJECTED):
            return State.FAILED
        if await has_text(page, WAITING):
            return State.WAITING_ROOM
        return None

    async def send_chat(self, page: Any, text: str) -> bool:
        if not self._chat_open:
            self._chat_open = await click_any(page, CHAT_TOGGLE, timeout_ms=2500, what="chat panel")
        if not await fill_any(page, CHAT_INPUT, text, timeout_ms=3000):
            self._chat_open = False
            return False
        if not await click_any(page, CHAT_SEND, timeout_ms=1200, what="send"):
            await page.keyboard.press("Enter")
        return True

    async def read_participants(self, page: Any) -> list[Participant]:
        if not self._people_open:
            self._people_open = await click_any(page, PEOPLE_TOGGLE, timeout_ms=1500, what="people panel")
        out: list[Participant] = []
        for i, name in enumerate(await texts(page, PEOPLE_ROW)):
            name = name.split("\n")[0].strip()
            if name:
                out.append(Participant(id=f"meet-{i}-{name.lower()}", name=name))
        return out

    async def read_chat(self, page: Any) -> list[ChatMessage]:
        return []

    async def active_speaker(self, page: Any) -> str | None:
        tile = await first_visible(page, SPEAKING_TILE, timeout_ms=300)
        if tile is None:
            return None
        try:
            name = await tile.locator(TILE_NAME).first.inner_text(timeout=600)
        except Exception:
            try:
                name = await tile.get_attribute("aria-label") or ""
            except Exception:
                return None
        name = " ".join(str(name).split()).replace(" is speaking", "").strip()
        return name or None

    async def leave(self, page: Any) -> None:
        if not await click_any(page, LEAVE_BUTTON, timeout_ms=3000, what="leave"):
            log.debug("no leave button; closing the page instead")
        await page.close()
