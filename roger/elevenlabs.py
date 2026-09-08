"""ElevenLabs account status. Hearing (Scribe) and speaking (Flash) both draw from the same credit pool."""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

import httpx

SUBSCRIPTION_URL = "https://api.elevenlabs.io/v1/user/subscription"
BILLING_URL = "https://elevenlabs.io/app/subscription"
LOW_CREDITS = 1000  # below this a single meeting will not get far


@dataclass
class Quota:
    tier: str
    used: int
    limit: int
    resets: _dt.date | None

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    @property
    def low(self) -> bool:
        return self.remaining < LOW_CREDITS

    def describe(self) -> str:
        reset = f", resets {self.resets.isoformat()}" if self.resets else ""
        return f"{self.remaining:,} of {self.limit:,} credits left ({self.tier} tier{reset})"


def _parse(d: dict) -> Quota:
    reset = d.get("next_character_count_reset_unix")
    return Quota(
        tier=str(d.get("tier") or "unknown"),
        used=int(d.get("character_count") or 0),
        limit=int(d.get("character_limit") or 0),
        resets=_dt.datetime.fromtimestamp(reset).date() if reset else None,
    )


def fetch_quota(api_key: str, timeout: float = 10.0) -> Quota:
    r = httpx.get(SUBSCRIPTION_URL, headers={"xi-api-key": api_key}, timeout=timeout)
    r.raise_for_status()
    return _parse(r.json())


async def fetch_quota_async(http: httpx.AsyncClient, api_key: str) -> Quota:
    r = await http.get(SUBSCRIPTION_URL, headers={"xi-api-key": api_key}, timeout=10.0)
    r.raise_for_status()
    return _parse(r.json())


def exhausted_message(context: str) -> str:
    return f"ElevenLabs credits exhausted: {context}. Hearing and speaking both use credits. Add credits or upgrade at {BILLING_URL}"
