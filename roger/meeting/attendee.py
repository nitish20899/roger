"""The hosted alternative: let Attendee's service run the participant.

Roger does not need this. :class:`~roger.meeting.browser.BrowserMeeting` joins calls on your own machine
on the OpenAI key alone, and that is the default. This is kept because it is one file behind the same
interface, and because a hosted participant is a useful fallback if browser automation has a bad day in
front of an audience -- set ``MEETING_PROVIDER=attendee`` and nothing else changes.

It talks to https://docs.attendee.dev over HTTP and one WebSocket. No Attendee source is used here.
"""
from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any, Optional

import httpx
from aiohttp import WSMsgType, web

from ..audio import PARTICIPANT_RATE
from .base import Event, MeetingClient, Participant, State, register

log = logging.getLogger("roger.meeting.attendee")

WS_PATH = "/ws/attendee"
CHAT_LIMIT = 480  # Google Meet truncates around 500 characters

STATES = {
    "joining": State.JOINING,
    "waiting_room": State.WAITING_ROOM,
    "joined_recording": State.JOINED,
    "joined_not_recording": State.JOINED,
    "joined_recording_paused": State.JOINED,
    "ended": State.LEFT,
    "data_deleted": State.LEFT,
    "fatal_error": State.FAILED,
}


@register
class AttendeeMeeting(MeetingClient):
    name = "attendee"
    ws_path = WS_PATH
    hosts = ("meet.google.com", "teams.microsoft.com", "teams.live.com", "zoom.us")

    def __init__(self, s: Any) -> None:
        super().__init__()
        self.s = s
        self.http = httpx.AsyncClient(
            timeout=30, headers={"Authorization": f"Token {s.attendee_api_key}", "Content-Type": "application/json"}
        )
        self.bot_id: Optional[str] = None
        self._bot_file: Path = Path(s.state_dir) / "bot.id"
        self._ws: web.WebSocketResponse | None = None
        self.frames_in = 0
        self.frames_out = 0

    # ------------------------------------------------------------------ joining
    async def join(self, url: str) -> None:
        if not self.s.attendee_api_key:
            raise RuntimeError("ATTENDEE_API_KEY missing (or use the default MEETING_PROVIDER=browser, which needs no such key)")
        if not self.s.public_url:
            raise RuntimeError("the hosted participant has to reach this machine: set PUBLIC_URL, or install cloudflared")
        self.meeting_url = url
        await self._set_state(State.JOINING)
        ws_url = self.s.public_url.replace("https://", "wss://").replace("http://", "ws://")
        ws_settings: dict[str, Any] = {"audio": {"url": f"{ws_url}{WS_PATH}", "sample_rate": self.s.sample_rate}}
        if self.s.per_participant_audio:
            # Attendee caps these at 16 kHz; they only drive speaker attribution, so the rate need not match.
            ws_settings["per_participant_audio"] = {"url": f"{ws_url}{WS_PATH}", "sample_rate": min(self.s.sample_rate, PARTICIPANT_RATE)}
        body: dict[str, Any] = {"meeting_url": url, "bot_name": self.s.bot_name, "websocket_settings": ws_settings, "metadata": {"app": "roger"}}

        r = await self.http.post(f"{self.s.attendee_base}/bots", json=body)
        if r.status_code >= 300 and self.s.per_participant_audio:
            log.warning("create with per-participant audio failed %s; retrying with mixed audio only", r.status_code)
            body["websocket_settings"].pop("per_participant_audio", None)
            r = await self.http.post(f"{self.s.attendee_base}/bots", json=body)
        if r.status_code >= 300:
            await self._set_state(State.FAILED)
            raise RuntimeError(f"Attendee create bot {r.status_code}: {r.text[:400]}")

        self.bot_id = r.json()["id"]
        self._bot_file.parent.mkdir(parents=True, exist_ok=True)
        self._bot_file.write_text(self.bot_id)  # survive a restart: the bot reconnects on its own
        log.info("bot created %s", self.bot_id)

    async def adopt_existing(self) -> bool:
        """After a restart, pick up the bot created earlier if it is still in a call."""
        if not self._bot_file.exists() or not self.s.attendee_api_key:
            return False
        bot_id = self._bot_file.read_text().strip()
        if not bot_id:
            return False
        r = await self.http.get(f"{self.s.attendee_base}/bots/{bot_id}")
        state = STATES.get(r.json().get("state", "")) if r.status_code == 200 else None
        if state is not None and state.live:
            self.bot_id = bot_id
            await self._set_state(state)
            log.info("adopted existing bot %s (%s)", bot_id, state.value)
            return True
        return False

    async def poll_state(self) -> State | None:
        if not self.bot_id:
            return None
        try:
            r = await self.http.get(f"{self.s.attendee_base}/bots/{self.bot_id}")
        except Exception as e:
            log.debug("state poll: %s", e)
            return None
        return STATES.get(r.json().get("state", "")) if r.status_code == 200 else None

    # ------------------------------------------------------------------ the service's socket
    async def handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        """``/ws/attendee``: meeting audio in, the bot's voice out."""
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=16 * 1024 * 1024)
        await ws.prepare(request)
        self._ws = ws
        log.info("attendee audio websocket connected")
        try:
            async for msg in ws:
                if msg.type is not WSMsgType.TEXT:
                    continue
                try:
                    evt = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                data = evt.get("data") or {}
                chunk = data.get("chunk")
                if not chunk:
                    continue
                trig = evt.get("trigger", "")
                self.frames_in += 1
                if trig == "realtime_audio.mixed":
                    await self.events.emit(Event.AUDIO, base64.b64decode(chunk), int(data.get("sample_rate") or self.s.sample_rate))
                elif trig == "realtime_audio.per_participant":
                    await self.events.emit(
                        Event.PARTICIPANT_AUDIO, str(data.get("participant_uuid")),
                        base64.b64decode(chunk), int(data.get("sample_rate") or PARTICIPANT_RATE),
                    )
        finally:
            if self._ws is ws:
                self._ws = None
            log.info("attendee audio websocket closed after %d frames", self.frames_in)
        return ws

    # ------------------------------------------------------------------ being a participant
    async def send_audio(self, pcm: bytes) -> None:
        if self._ws is None or self._ws.closed or not pcm:
            return
        frame = json.dumps({"trigger": "realtime_audio.bot_output", "data": {"chunk": base64.b64encode(pcm).decode(), "sample_rate": self.s.sample_rate}})
        try:
            await self._ws.send_str(frame)
            self.frames_out += 1
        except Exception as e:
            log.warning("send_audio: %s", e)

    async def send_chat(self, text: str) -> None:
        if not self.bot_id:
            log.info("chat (no bot): %s", text)
            return
        for i in range(0, len(text), CHAT_LIMIT):
            part = text[i : i + CHAT_LIMIT]
            r = await self.http.post(f"{self.s.attendee_base}/bots/{self.bot_id}/send_chat_message", json={"to": "everyone", "message": part})
            if r.status_code >= 300:
                log.warning("chat failed %s %s", r.status_code, r.text[:200])
            else:
                log.info("chat   %s: %s", self.s.bot_name, part)

    async def participants(self) -> list[Participant]:
        if not self.bot_id:
            return []
        r = await self.http.get(f"{self.s.attendee_base}/bots/{self.bot_id}/participants")
        if r.status_code != 200:
            return list(self._participants.values())
        people = [Participant(id=p["uuid"], name=p.get("name") or "Someone") for p in r.json().get("results", []) if p.get("uuid")]
        for p in people:
            await self._saw_participant(p)
        return people

    async def leave(self) -> None:
        if self.bot_id:
            r = await self.http.post(f"{self.s.attendee_base}/bots/{self.bot_id}/leave")
            log.info("leave -> %s", r.status_code)
            if r.status_code < 300:
                self.bot_id = None
                self._bot_file.unlink(missing_ok=True)
        await self._set_state(State.LEFT)

    async def close(self) -> None:
        await self.http.aclose()

    def health(self) -> dict:
        return {
            "provider": self.name, "state": self.state.value, "bot_id": self.bot_id,
            "page_connected": self._ws is not None and not self._ws.closed,
            "frames_in": self.frames_in, "frames_out": self.frames_out,
        }
