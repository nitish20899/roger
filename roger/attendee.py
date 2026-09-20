"""Client for the Attendee meeting-bot API (https://docs.attendee.dev).

Attendee runs the actual meeting participant (a browser in their cloud). We ask it to join a URL, it streams
the meeting audio to our WebSocket, plays the bot's voice back into the call, and posts chat messages.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

import httpx

from .audio import PARTICIPANT_RATE
from .config import Settings

log = logging.getLogger("roger.attendee")

LIVE_STATES = ("joining", "joined_recording", "joined_not_recording", "waiting_room", "joined_recording_paused")
FINAL_STATES = ("ended", "fatal_error", "data_deleted")
CHAT_LIMIT = 480  # Google Meet truncates around 500 characters


class Attendee:
    def __init__(self, s: Settings) -> None:
        self.s = s
        self.http = httpx.AsyncClient(timeout=30, headers={"Authorization": f"Token {s.attendee_api_key}", "Content-Type": "application/json"})
        self.bot_id: Optional[str] = None
        self._bot_file: Path = s.state_dir / "bot.id"

    async def create_bot(self, meeting_url: str, public_url: str) -> str:
        if not self.s.attendee_api_key:
            raise RuntimeError("ATTENDEE_API_KEY missing")
        ws_url = public_url.replace("https://", "wss://").replace("http://", "ws://")
        ws_settings: dict[str, Any] = {"audio": {"url": f"{ws_url}/ws/attendee", "sample_rate": self.s.sample_rate}}
        if self.s.per_participant_audio:
            # Attendee caps these streams at 16 kHz. They only drive the energy meter that puts names on the
            # transcript, so the rate does not have to match the session's.
            ws_settings["per_participant_audio"] = {"url": f"{ws_url}/ws/attendee", "sample_rate": min(self.s.sample_rate, PARTICIPANT_RATE)}
        body: dict[str, Any] = {
            "meeting_url": meeting_url,
            "bot_name": self.s.bot_name,
            "websocket_settings": ws_settings,
            "metadata": {"app": "roger"},
        }
        r = await self.http.post(f"{self.s.attendee_base}/bots", json=body)
        if r.status_code >= 300 and self.s.per_participant_audio:
            log.warning("create with per-participant audio failed %s: %s; retrying with mixed audio only", r.status_code, r.text[:200])
            body["websocket_settings"].pop("per_participant_audio", None)
            r = await self.http.post(f"{self.s.attendee_base}/bots", json=body)
        if r.status_code >= 300:
            raise RuntimeError(f"Attendee create bot {r.status_code}: {r.text[:400]}")
        self.bot_id = r.json()["id"]
        self._bot_file.parent.mkdir(parents=True, exist_ok=True)
        self._bot_file.write_text(self.bot_id)  # survive server restarts: the bot reconnects on its own
        log.info("bot created %s", self.bot_id)
        return self.bot_id

    async def adopt_existing(self) -> None:
        """After a restart, pick up the bot we created earlier if it is still in a call."""
        if not self._bot_file.exists() or not self.s.attendee_api_key:
            return
        bot_id = self._bot_file.read_text().strip()
        if not bot_id:
            return
        r = await self.http.get(f"{self.s.attendee_base}/bots/{bot_id}")
        state = r.json().get("state") if r.status_code == 200 else None
        if state in LIVE_STATES:
            self.bot_id = bot_id
            log.info("adopted existing bot %s (state %s)", bot_id, state)

    async def state(self) -> Optional[str]:
        if not self.bot_id:
            return None
        r = await self.http.get(f"{self.s.attendee_base}/bots/{self.bot_id}")
        return r.json().get("state") if r.status_code == 200 else f"http {r.status_code}"

    async def chat(self, text: str) -> None:
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

    async def participants(self) -> dict[str, str]:
        """participant uuid -> display name."""
        if not self.bot_id:
            return {}
        r = await self.http.get(f"{self.s.attendee_base}/bots/{self.bot_id}/participants")
        if r.status_code != 200:
            return {}
        return {p.get("uuid"): p.get("name") or "Someone" for p in r.json().get("results", []) if p.get("uuid")}

    async def leave(self) -> None:
        if self.bot_id:
            r = await self.http.post(f"{self.s.attendee_base}/bots/{self.bot_id}/leave")
            log.info("leave -> %s", r.status_code)
            if r.status_code < 300:
                self.bot_id = None
                self._bot_file.unlink(missing_ok=True)

    async def close(self) -> None:
        await self.http.aclose()
