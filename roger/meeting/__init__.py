"""Getting Roger into a meeting, and everything else out of the business of how.

One interface -- :class:`~roger.meeting.base.MeetingClient` -- covers hearing, speaking, chat and who is
in the room. Two providers implement it:

``browser``   a real Chromium on this machine, driven by Playwright. The default, and the reason Roger
              needs no meeting-bot service: your OpenAI key is the only key involved.
``attendee``  the hosted service at attendee.dev, over HTTP. Optional, and a useful fallback.

Adding a platform (Zoom, Webex) is a :class:`~roger.meeting.browser.Platform` subclass in ``platforms/``:
seven methods that press buttons, no audio and no state machine. Adding a *consumer* -- a recorder, a
note-taker, a second listener -- is a subscription:

    meeting.on(Event.AUDIO, my_handler)

Nothing above this package knows which provider or platform is in play.
"""
from __future__ import annotations

import importlib
import importlib.util
import logging
from typing import Any

from .base import (
    ChatMessage, Event, EventBus, MeetingClient, Participant, State, UnsupportedMeeting,
    client_for, register, registered,
)
from .browser import BrowserMeeting, Platform, platform, platform_for, known_platforms
from .attendee import AttendeeMeeting

# Importing the package is what registers Google Meet and Teams; nothing here uses the names.
importlib.import_module(".platforms", __name__)

log = logging.getLogger("roger.meeting")

PROVIDERS: dict[str, type[MeetingClient]] = {
    BrowserMeeting.name: BrowserMeeting,
    AttendeeMeeting.name: AttendeeMeeting,
}
DEFAULT_PROVIDER = BrowserMeeting.name


def create_client(s: Any, url: str | None = None) -> MeetingClient:
    """The meeting client for this configuration.

    ``MEETING_PROVIDER`` names one explicitly. ``auto`` picks the browser unless there is no Playwright
    and an Attendee key is configured, which is the one case where the hosted service is the better guess.
    """
    want = (getattr(s, "meeting_provider", "") or DEFAULT_PROVIDER).strip().lower()
    if want == "auto":
        want = DEFAULT_PROVIDER if browser_available() or not s.attendee_api_key else AttendeeMeeting.name
    cls = PROVIDERS.get(want)
    if cls is None:
        raise RuntimeError(f"MEETING_PROVIDER={want!r} is not one of: {', '.join(PROVIDERS)}")
    if url and not cls.handles(url):
        raise UnsupportedMeeting(f"the {want} participant does not handle {url!r}; supported here: {supported_platforms()}")
    return cls(s)


def browser_available() -> bool:
    """Is Playwright importable? (Whether a browser is *installed* only shows up at join time.)"""
    try:
        return importlib.util.find_spec("playwright.async_api") is not None
    except (ImportError, ValueError):
        return False


def supported_platforms() -> str:
    return ", ".join(f"{p.name} ({'/'.join(p.hosts)})" for p in known_platforms())


__all__ = [
    "AttendeeMeeting", "BrowserMeeting", "ChatMessage", "DEFAULT_PROVIDER", "Event", "EventBus",
    "MeetingClient", "PROVIDERS", "Participant", "Platform", "State", "UnsupportedMeeting", "browser_available",
    "client_for", "create_client", "platform", "platform_for", "known_platforms", "register", "registered",
    "supported_platforms",
]
