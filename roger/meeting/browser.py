"""A real meeting participant, running in a browser on this machine.

Roger opens Chromium, walks into the call the way a person would, and swaps the microphone for one that
carries GPT-Live's voice. No meeting-bot service is involved and nothing but OpenAI is billed.

The trick is entirely in the page. ``assets/bridge.js`` is injected before the meeting app's own scripts,
patches ``getUserMedia`` to hand out a synthetic microphone, and taps every inbound WebRTC audio track.
Everything here is the Python half: launching the browser, carrying audio both ways, and turning what
comes back into events.

Audio crosses on a Playwright binding rather than a WebSocket, and that is a scar. A page script cannot
open a socket to ``127.0.0.1`` from inside Google Meet -- the site's Content-Security-Policy forbids it,
and the attempt takes the renderer down with it, which reads as "the page crashed" and nothing else. A
binding is installed by the driver over the DevTools protocol, so no page policy applies.

What differs between Google Meet and Teams is only which buttons to press, and that lives in
``platforms/``. This file has no product-specific knowledge at all.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlparse

from ..audio import rms
from .base import ChatMessage, Event, MeetingClient, Participant, State, UnsupportedMeeting, register

log = logging.getLogger("roger.meeting.browser")

ASSETS = Path(__file__).resolve().parent / "assets"
BINDING = "__rogerSend"
FRAME_MS = 100

# Chromium is told to behave like a person at a desk with a webcam: grant the media prompt, allow audio to
# start without a click, and leave no obvious automation fingerprint.
CHROME_ARGS = [
    "--use-fake-ui-for-media-stream",
    "--use-fake-device-for-media-stream",
    "--autoplay-policy=no-user-gesture-required",
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process,Translate",
    "--disable-infobars",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-dev-shm-usage",
]


class Platform(ABC):
    """How one meeting product's web UI is driven.

    A platform is only ever asked to press buttons and read the DOM. It never touches audio, the socket or
    Roger's state machine, so adding Zoom or Webex is a matter of writing these seven methods.
    """

    name: ClassVar[str] = "platform"
    hosts: ClassVar[tuple[str, ...]] = ()

    @classmethod
    def handles(cls, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return any(host == h or host.endswith("." + h) for h in cls.hosts)

    async def prepare(self, page: Any) -> None:
        """Anything needed before the join form: cookie banners, consent, a language switch."""

    @abstractmethod
    async def join(self, page: Any, url: str, bot_name: str) -> None:
        """Fill in the name, turn the camera off and ask to be let in."""

    @abstractmethod
    async def poll_state(self, page: Any) -> State | None:
        """Where are we now? ``None`` means "no idea, keep the state you had"."""

    async def send_chat(self, page: Any, text: str) -> bool:
        return False

    async def read_participants(self, page: Any) -> list[Participant]:
        return []

    async def read_chat(self, page: Any) -> list[ChatMessage]:
        return []

    async def active_speaker(self, page: Any) -> str | None:
        """The name the UI is currently highlighting as talking, if the UI shows one.

        This is how a browser participant gets names onto the transcript. WebRTC hands us one audio track
        per person but no names, and the DOM has the names but no audio, so the two are matched up in
        :meth:`BrowserMeeting._name_speakers`.
        """
        return None

    async def leave(self, page: Any) -> None:
        await page.close()


_PLATFORMS: list[type[Platform]] = []


def platform(cls: type[Platform]) -> type[Platform]:
    """Register a platform so :class:`BrowserMeeting` will drive its URLs."""
    if cls not in _PLATFORMS:
        _PLATFORMS.append(cls)
    return cls


def known_platforms() -> list[type[Platform]]:
    return list(_PLATFORMS)


def platform_for(url: str) -> type[Platform] | None:
    for cls in _PLATFORMS:
        if cls.handles(url):
            return cls
    return None


@register
class BrowserMeeting(MeetingClient):
    """A participant that is a browser on this machine."""

    name = "browser"
    ws_path = None  # nothing dials in: the page is driven over the DevTools protocol

    def __init__(self, s: Any) -> None:
        super().__init__()
        self.s = s
        self.rate = s.sample_rate
        self.platform: Platform | None = None
        self._pw = None
        self._ctx = None
        self.page: Any = None
        self._linked = False   # the page has said hello over the binding
        self._watch: asyncio.Task | None = None
        self._streams: dict[int, str] = {}  # page stream id -> the WebRTC track id behind it
        self._energy: dict[int, tuple[float, float]] = {}  # stream id -> (last frame time, loudness)
        self._named: dict[int, str] = {}  # stream id -> the person we decided it is
        self.frames_in = 0
        self.frames_out = 0
        self.page_connected_at = 0.0

    # ------------------------------------------------------------------ routing
    @classmethod
    def handles(cls, url: str) -> bool:
        return platform_for(url) is not None

    # ------------------------------------------------------------------ the page script
    def _bridge_js(self) -> str:
        cfg = {
            "rate": self.rate,
            "frame_ms": FRAME_MS,
            "bot_name": self.s.bot_name,
            "per_participant": bool(self.s.per_participant_audio),
            "debug": bool(getattr(self.s, "browser_debug", False)),
        }
        return (ASSETS / "bridge.js").read_text().replace("__ROGER_CFG__", json.dumps(cfg))

    # ------------------------------------------------------------------ joining
    async def join(self, url: str) -> None:
        cls = platform_for(url)
        if cls is None:
            raise UnsupportedMeeting(f"no browser platform handles {url!r}; supported: {', '.join(p.name for p in known_platforms())}")
        self.platform = cls()
        self.meeting_url = url
        await self._set_state(State.JOINING)

        try:
            from playwright.async_api import async_playwright
        except ImportError as e:  # pragma: no cover - depends on the install
            raise RuntimeError(
                "the browser participant needs Playwright: pip install 'roger-meeting-agent[browser]' "
                "&& playwright install chromium"
            ) from e

        profile = Path(self.s.state_dir) / "browser-profile"
        profile.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        # A persistent profile is what makes a signed-in bot possible: log in once with BROWSER_HEADLESS=0
        # and the session is still there next time.
        self._ctx = await self._pw.chromium.launch_persistent_context(
            str(profile),
            headless=bool(self.s.browser_headless),
            args=CHROME_ARGS,
            viewport={"width": 1280, "height": 800},
            permissions=["microphone", "camera"],
            ignore_default_args=["--enable-automation", "--mute-audio"],
        )
        # The binding must exist before any page script runs, so it goes on the context, not the page.
        await self._ctx.expose_binding(BINDING, self._on_binding)
        await self._ctx.add_init_script(self._bridge_js())
        self.page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        if getattr(self.s, "browser_debug", False):
            self.page.on("console", lambda m: log.debug("page: %s", m.text))

        log.info("%s: opening %s", self.platform.name, url)
        await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await self.platform.prepare(self.page)
        await self.platform.join(self.page, url, self.s.bot_name)
        self._watch = asyncio.create_task(self._watch_page())

    def _participant_id(self, stream_id: int) -> str:
        return self._named.get(stream_id) or self._streams.get(stream_id) or f"stream-{stream_id}"

    async def _name_speakers(self) -> None:
        """Match the track that is making noise to the name the UI is highlighting.

        Only a clear winner counts: if two people are talking at once, or the UI is not highlighting
        anyone, nothing is learned this round. Bindings are sticky, because a voice does not change tile.
        """
        now = time.time()
        loud = [(sid, level) for sid, (at, level) in self._energy.items() if now - at < 1.2 and level > 250]
        if len(loud) != 1:
            return
        stream_id, _ = loud[0]
        if stream_id in self._named:
            return
        name = await self.platform.active_speaker(self.page)
        if not name or name.strip().lower() == self.s.bot_name.strip().lower():
            return
        self._named[stream_id] = name.strip()
        log.info("stream %d is %s", stream_id, name.strip())
        await self._saw_participant(Participant(id=name.strip(), name=name.strip()))

    async def _watch_page(self) -> None:
        """Poll the page for where we are and who is here. The platform does the reading."""
        last_participants = 0.0
        while self.page and not self.page.is_closed():
            try:
                st = await self.platform.poll_state(self.page)
                if st is not None:
                    await self._set_state(st)
                    if st.final:
                        return
                if self.state is State.JOINED:
                    await self._name_speakers()
                    if time.time() - last_participants > 8:
                        last_participants = time.time()
                        for p in await self.platform.read_participants(self.page):
                            await self._saw_participant(p)
            except Exception as e:
                log.debug("watch: %s", e)
            await asyncio.sleep(1)
        await self._set_state(State.LEFT)

    # ------------------------------------------------------------------ the link to the page
    async def _on_binding(self, _source: Any, raw: str) -> bool:
        """Everything the page sends. One function, because a binding is one function."""
        try:
            await self._on_page_event(raw)
        except Exception as e:
            log.warning("page message failed: %s", e)
        return True

    async def _on_page_audio(self, stream_id: int, payload: str) -> None:
        try:
            pcm = base64.b64decode(payload)
        except (binascii.Error, ValueError):
            return
        if len(pcm) < 2:
            return
        self.frames_in += 1
        if stream_id == 0:
            await self.events.emit(Event.AUDIO, pcm, self.rate)
            return
        self._energy[stream_id] = (time.time(), rms(pcm))
        await self.events.emit(Event.PARTICIPANT_AUDIO, self._participant_id(stream_id), pcm, self.rate)

    async def _on_page_event(self, raw: str) -> None:
        try:
            m = json.loads(raw)
        except json.JSONDecodeError:
            return
        kind = m.get("type")
        if kind == "audio":
            await self._on_page_audio(int(m.get("stream") or 0), str(m.get("pcm") or ""))
        elif kind == "hello":
            self._linked = True
            log.info("page bridge ready: %s Hz, %s-sample frames", m.get("rate"), m.get("frame"))
        elif kind == "audio_format":
            # The page tells us what the browser actually gave it; capture is already at our wire rate.
            log.info("page audio: out %s Hz, capture %s Hz", m.get("out_rate"), m.get("in_rate"))
            self.rate = int(m.get("in_rate") or self.rate)
        elif kind == "track":
            self._streams[int(m["stream_id"])] = str(m.get("track_id") or m["stream_id"])
        elif kind == "track_ended":
            self._streams.pop(int(m["stream_id"]), None)
        elif kind == "chat":
            await self.events.emit(Event.CHAT, ChatMessage(str(m.get("sender") or "Someone"), str(m.get("text") or "")))
        elif kind == "caption":
            await self.events.emit(Event.CAPTION, str(m.get("speaker") or "Someone"), str(m.get("text") or ""))
        elif kind == "participants":
            for row in m.get("people") or []:
                await self._saw_participant(Participant(str(row.get("id") or row.get("name")), str(row.get("name") or "Someone")))

    async def _command(self, **payload: Any) -> None:
        if self.page is None or self.page.is_closed():
            return
        try:
            await self.page.evaluate("m => window.__rogerBridge && window.__rogerBridge.command(m)", json.dumps(payload))
        except Exception as e:
            log.debug("command %s: %s", payload.get("type"), e)

    # ------------------------------------------------------------------ being a participant
    async def send_audio(self, pcm: bytes) -> None:
        """Roger speaks: straight into the page's synthetic microphone."""
        if not pcm or not self._linked or self.page is None or self.page.is_closed():
            return
        try:
            ok = await self.page.evaluate(
                "b => window.__rogerBridge && window.__rogerBridge.play(b)",
                base64.b64encode(pcm).decode(),
            )
        except Exception as e:
            log.debug("send_audio: %s", e)
            return
        if ok:
            self.frames_out += 1

    async def flush_audio(self) -> None:
        """Drop whatever has been handed to the page but not yet spoken (an interruption)."""
        await self._command(type="flush")

    async def send_chat(self, text: str) -> None:
        if not self.page or self.page.is_closed() or self.platform is None:
            log.info("chat (not in a meeting): %s", text)
            return
        try:
            ok = await self.platform.send_chat(self.page, text)
        except Exception as e:
            ok = False
            log.warning("chat failed: %s", e)
        log.info("chat   %s: %s", self.s.bot_name, text) if ok else log.warning("chat not delivered: %s", text)

    async def leave(self) -> None:
        if self.platform and self.page and not self.page.is_closed():
            try:
                await self.platform.leave(self.page)
            except Exception as e:
                log.warning("leave: %s", e)
        await self._set_state(State.LEFT)
        await self.close()

    async def close(self) -> None:
        self._linked = False
        if self._watch:
            self._watch.cancel()
            self._watch = None
        for obj, what in ((self._ctx, "context"), (self._pw, "playwright")):
            if obj is None:
                continue
            try:
                await (obj.close() if what == "context" else obj.stop())
            except Exception as e:
                log.debug("closing %s: %s", what, e)
        self._ctx = self._pw = self.page = None

    def health(self) -> dict:
        return {
            "provider": self.name,
            "platform": self.platform.name if self.platform else None,
            "state": self.state.value,
            "page_connected": self._linked,
            "frames_in": self.frames_in,
            "frames_out": self.frames_out,
            "audio_streams": len(self._streams),
            "named_streams": dict(self._named),
        }
