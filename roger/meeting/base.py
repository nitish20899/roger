"""What a meeting looks like to the rest of Roger.

Everything above this file -- the bridge, the voice session, anything added later -- talks to a
:class:`MeetingClient` and subscribes to its :class:`EventBus`. Nothing above this file knows whether the
call is Google Meet or Teams, or whether the participant is a browser on this machine or a hosted service.

That is the whole point of the seam. Adding a platform means writing one class and registering it; adding
a *feature* that needs the meeting -- recording, captions, reactions, a second listener -- means
subscribing to an event, not editing the joiner.
"""
from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, ClassVar, Iterable
from urllib.parse import urlparse

log = logging.getLogger("roger.meeting")


class UnsupportedMeeting(ValueError):
    """The URL is not one any configured participant can join. A bad request, not a failure."""


class State(str, Enum):
    """Where the participant is. Values are the strings Roger logs and reports on ``/health``."""

    IDLE = "idle"
    JOINING = "joining"
    WAITING_ROOM = "waiting_room"
    JOINED = "joined"
    LEFT = "left"
    FAILED = "fatal_error"

    @property
    def live(self) -> bool:
        return self in (State.JOINING, State.WAITING_ROOM, State.JOINED)

    @property
    def final(self) -> bool:
        return self in (State.LEFT, State.FAILED)


class Event(str, Enum):
    """The things a meeting tells you about. Subscribe with :meth:`MeetingClient.on`."""

    AUDIO = "audio"                        # (pcm: bytes, rate: int) -- everyone, mixed
    PARTICIPANT_AUDIO = "participant_audio"  # (participant_id: str, pcm: bytes, rate: int)
    CHAT = "chat"                          # (ChatMessage,)
    PARTICIPANT = "participant"            # (Participant, joined: bool)
    STATE = "state"                        # (State,)
    CAPTION = "caption"                    # (speaker: str, text: str) -- the platform's own captions


@dataclass(frozen=True)
class Participant:
    id: str
    name: str = "Someone"
    is_self: bool = False
    is_host: bool = False


@dataclass(frozen=True)
class ChatMessage:
    sender: str
    text: str
    at: float = field(default_factory=time.time)
    is_self: bool = False


Handler = Callable[..., Any]


class EventBus:
    """Fan-out to any number of listeners. A listener that raises never reaches the others."""

    def __init__(self) -> None:
        self._subs: dict[str, list[Handler]] = defaultdict(list)

    def on(self, event: Event | str, fn: Handler) -> Handler:
        self._subs[Event(event).value].append(fn)
        return fn

    def off(self, event: Event | str, fn: Handler) -> None:
        try:
            self._subs[Event(event).value].remove(fn)
        except ValueError:
            pass

    def listeners(self, event: Event | str) -> list[Handler]:
        return list(self._subs[Event(event).value])

    async def emit(self, event: Event | str, *args: Any) -> None:
        """Call every listener. Coroutines are awaited in order, so audio stays in order."""
        for fn in self._subs[Event(event).value]:
            try:
                r = fn(*args)
                if asyncio.iscoroutine(r):
                    await r
            except Exception as e:  # one bad listener must not take the meeting down
                log.warning("listener for %s failed: %s", Event(event).value, e)


class MeetingClient(ABC):
    """One participant in one call.

    Subclasses implement joining and the four things a participant can do: hear, speak, read chat, write
    chat. :class:`~roger.meeting.browser.BrowserMeeting` does it with a real browser on this machine;
    :class:`~roger.meeting.attendee.AttendeeMeeting` hands it to a hosted service. Both look the same here.
    """

    name: ClassVar[str] = "meeting"
    hosts: ClassVar[tuple[str, ...]] = ()
    ws_path: ClassVar[str | None] = None  # a route the server should mount for this provider's audio socket

    def __init__(self) -> None:
        self.events = EventBus()
        self.state = State.IDLE
        self.meeting_url: str | None = None
        self._participants: dict[str, Participant] = {}

    # ------------------------------------------------------------------ routing
    @classmethod
    def handles(cls, url: str) -> bool:
        """Is this client the one for this meeting URL? Matched on hostname by default."""
        host = (urlparse(url).hostname or "").lower()
        return any(host == h or host.endswith("." + h) for h in cls.hosts)

    # ------------------------------------------------------------------ the participant
    @abstractmethod
    async def join(self, url: str) -> None:
        """Enter the call. Returns once the platform has been asked; watch :attr:`Event.STATE` for arrival."""

    @abstractmethod
    async def leave(self) -> None:
        """Leave the call and release whatever was holding it open."""

    @abstractmethod
    async def send_audio(self, pcm: bytes) -> None:
        """Speak: 16-bit mono PCM at the configured rate, into the call."""

    @abstractmethod
    async def send_chat(self, text: str) -> None:
        """Write to the meeting chat."""

    async def participants(self) -> list[Participant]:
        """Who is in the room, as far as this client knows."""
        return list(self._participants.values())

    async def close(self) -> None:
        """Release resources. Does not necessarily leave the call."""

    # ------------------------------------------------------------------ helpers for subclasses
    def on(self, event: Event | str, fn: Handler) -> Handler:
        return self.events.on(event, fn)

    async def _set_state(self, state: State) -> None:
        if state is not self.state:
            self.state = state
            log.info("meeting state: %s", state.value)
            await self.events.emit(Event.STATE, state)

    async def _saw_participant(self, p: Participant) -> None:
        known = self._participants.get(p.id)
        if known == p:
            return
        self._participants[p.id] = p
        await self.events.emit(Event.PARTICIPANT, p, known is None)

    async def _lost_participant(self, participant_id: str) -> None:
        p = self._participants.pop(participant_id, None)
        if p is not None:
            await self.events.emit(Event.PARTICIPANT, p, False)


# --------------------------------------------------------------------------- registry

_REGISTRY: list[type[MeetingClient]] = []


def register(cls: type[MeetingClient]) -> type[MeetingClient]:
    """Make a client eligible for :func:`client_for`. Use as a decorator on the class."""
    if cls not in _REGISTRY:
        _REGISTRY.append(cls)
    return cls


def registered() -> list[type[MeetingClient]]:
    return list(_REGISTRY)


def client_for(url: str, among: Iterable[type[MeetingClient]] | None = None) -> type[MeetingClient] | None:
    """The registered client that handles this meeting URL, or ``None``."""
    for cls in among if among is not None else _REGISTRY:
        try:
            if cls.handles(url):
                return cls
        except Exception:
            continue
    return None


__all__ = [
    "ChatMessage", "Event", "EventBus", "MeetingClient", "Participant", "State", "UnsupportedMeeting",
    "client_for", "register", "registered",
]
