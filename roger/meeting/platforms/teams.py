"""Microsoft Teams.

Teams has more doors than Meet: a meetup-join link wants to open the desktop app, the live.microsoft.com
links behave differently again, and a guest may be asked to sign in before the pre-join screen appears.
The extra work here is getting past those and onto the pre-join screen; after that it is the same three
steps as anywhere else -- name, camera off, join.
"""
from __future__ import annotations

import logging
from typing import Any

from ..base import Participant, State
from ..browser import Platform, platform
from ..dom import click_any, dismiss_all, fill_any, first_visible, has_text, texts

log = logging.getLogger("roger.meeting.teams")

# "Continue on this browser" -- the one that keeps us out of the desktop app.
BROWSER_BUTTON = [
    'button:has-text("Continue on this browser")', 'a:has-text("Continue on this browser")',
    'button:has-text("Join on the web instead")', 'button:has-text("Use the web app instead")',
    '[data-tid="joinOnWeb"]',
]
GUEST_BUTTON = ['button:has-text("Join as a guest")', 'button:has-text("Continue as guest")', '[data-tid="join-as-guest"]']
NAME_INPUT = [
    '[data-tid="prejoin-display-name-input"]', 'input[placeholder="Type your name"]',
    'input[placeholder*="your name" i]', 'input[aria-label*="your name" i]', 'input[name="displayName"]',
]
JOIN_BUTTON = [
    '[data-tid="prejoin-join-button"]', 'button:has-text("Join now")', 'button:has-text("Join meeting")',
    '[aria-label*="Join now" i]',
]
CAMERA_OFF = ['[data-tid="toggle-video"][aria-pressed="true"]', 'button[aria-label*="Turn camera off" i]', '[data-tid="prejoin-video-button"][aria-pressed="true"]']
MIC_ON = ['button[aria-label*="Unmute" i]', '[data-tid="toggle-mute"][aria-pressed="false"]', 'button[aria-label*="Turn microphone on" i]']
LEAVE_BUTTON = ['[data-tid="hangup-main-btn"]', 'button[aria-label*="Leave" i]', '#hangup-button', 'button[title*="Leave" i]']
DISMISS = ['button:has-text("Accept all")', 'button:has-text("Reject all")', 'button:has-text("Got it")', 'button[aria-label="Close"]', 'button:has-text("Dismiss")']
CHAT_TOGGLE = ['[data-tid="chat-button"]', 'button[aria-label*="Chat" i]', '#chat-button']
CHAT_INPUT = ['[data-tid="ckeditor"]', 'div[contenteditable="true"][role="textbox"]', 'div[aria-label*="Type a message" i]']
CHAT_SEND = ['[data-tid="newMessageCommands-send"]', 'button[aria-label*="Send" i]']
PEOPLE_TOGGLE = ['[data-tid="roster-button"]', 'button[aria-label*="People" i]', 'button[aria-label*="Participants" i]']
PEOPLE_ROW = '[data-tid="roster-participant"], [data-tid="participant-item"], li[role="listitem"]'
SPEAKING_TILE = [
    '[data-tid="participant-speaking-indicator"]',
    '[aria-label*="is speaking" i]',
    '[data-tid^="participant-tile"][class*="speaking"]',
]
TILE_NAME = '[data-tid="participant-name"], [class*="displayName"]'

WAITING = ("someone will let you in", "waiting for someone", "you're in the lobby", "when the meeting starts", "waiting in the lobby")
REJECTED = ("didn't let you in", "you were removed", "the meeting has ended", "couldn't join", "we couldn't connect you", "sorry, we couldn't")


@platform
class Teams(Platform):
    name = "teams"
    hosts = ("teams.microsoft.com", "teams.live.com", "teams.microsoft.us")

    def __init__(self) -> None:
        self._chat_open = False
        self._people_open = False

    async def prepare(self, page: Any) -> None:
        await dismiss_all(page, DISMISS)
        # The desktop-app interstitial can appear before or after the consent banner, so try it twice.
        if await click_any(page, BROWSER_BUTTON, timeout_ms=6000, what="continue on this browser"):
            await page.wait_for_load_state("domcontentloaded", timeout=30000)
            await dismiss_all(page, DISMISS)
        await click_any(page, GUEST_BUTTON, timeout_ms=2500, what="join as guest")

    async def join(self, page: Any, url: str, bot_name: str) -> None:
        await click_any(page, BROWSER_BUTTON, timeout_ms=2500, what="continue on this browser")
        if await fill_any(page, NAME_INPUT, bot_name, timeout_ms=20000):
            log.info("joining as a guest called %r", bot_name)
        else:
            log.info("no name field; joining with the browser profile's signed-in account")
        await click_any(page, CAMERA_OFF, timeout_ms=2500, what="camera off")
        if await click_any(page, MIC_ON, timeout_ms=2500, what="microphone on"):
            log.info("microphone was muted; turned it on so Roger can be heard")
        await dismiss_all(page, DISMISS)
        if not await click_any(page, JOIN_BUTTON, timeout_ms=20000, what="join"):
            present = await first_visible(page, JOIN_BUTTON, timeout_ms=2000) is not None
            raise RuntimeError(
                "Teams: the join button was on screen but could not be clicked (something is covering it)"
                if present else
                "Teams: no join button on this page -- the link may need a signed-in account. "
                "Run with BROWSER_HEADLESS=0 to watch what it sees."
            )
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

    async def read_participants(self, page: Any) -> list[Participant]:
        if not self._people_open:
            self._people_open = await click_any(page, PEOPLE_TOGGLE, timeout_ms=1500, what="people panel")
        out: list[Participant] = []
        for i, name in enumerate(await texts(page, PEOPLE_ROW)):
            name = name.split("\n")[0].strip()
            if name:
                out.append(Participant(id=f"teams-{i}-{name.lower()}", name=name))
        return out

    async def leave(self, page: Any) -> None:
        if not await click_any(page, LEAVE_BUTTON, timeout_ms=3000, what="leave"):
            log.debug("no leave button; closing the page instead")
        await page.close()
