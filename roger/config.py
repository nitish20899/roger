"""Configuration. Everything comes from environment variables, normally through a ``.env`` file.

``.env.example`` at the repository root documents every variable. Only three keys are required:
``ATTENDEE_API_KEY`` and ``OPENAI_API_KEY``. Those are the only two services Roger talks to.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PACKAGE_DIR = Path(__file__).resolve().parent
STATIC_DIR = PACKAGE_DIR / "static"
REPO_ROOT = PACKAGE_DIR.parent

# How the transcriber tends to mishear common names. Added to the wake words automatically.
MISHEARINGS: dict[str, list[str]] = {
    "roger": ["rodger", "rojer"],
    "claude": ["cloud", "claud", "clawed", "clod"],
}

LIVE_MODEL = "gpt-live-1"
# Built-in GPT-Live voices (https://developers.openai.com/api/docs/guides/live-conversations#voice-options).
# Masculine-sounding ones, roughly: cedar, ash, verse, echo, ballad, stone, cinder, meridian.
LIVE_VOICES = [
    "alloy", "ash", "ballad", "beacon", "bossa", "cedar", "cinder", "coral", "delta", "echo", "gleam",
    "marin", "meridian", "quartz", "ripple", "sage", "shimmer", "stone", "tempo", "verse", "vesper", "willow",
]


def load_env(path: str | None = None) -> list[Path]:
    """Load ``.env`` files without overriding variables already present in the environment.

    Order: an explicit path (or ``$ROGER_ENV``); otherwise ``./.env`` in the current directory, then the
    repository root (for ``pip install -e .`` checkouts), then ``~/.roger/.env`` so ``roger`` works from any
    directory. Returns the files that were loaded.
    """
    explicit = path or os.getenv("ROGER_ENV")
    state_dir = Path(os.getenv("ROGER_STATE_DIR", "~/.roger")).expanduser()
    candidates = [Path(explicit).expanduser()] if explicit else [Path.cwd() / ".env", REPO_ROOT / ".env", state_dir / ".env"]
    loaded: list[Path] = []
    for p in candidates:
        if p.is_file() and p.resolve() not in {q.resolve() for q in loaded}:
            load_dotenv(p, override=False)
            loaded.append(p)
    return loaded


def env(name: str, default: str | None = None) -> str | None:
    v = os.getenv(name)
    return v if v not in (None, "") else default


def flag(name: str, default: bool) -> bool:
    return (env(name, "1" if default else "0") or "").strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    # server
    port: int = 8787
    public_url: str | None = None
    state_dir: Path = field(default_factory=lambda: Path.home() / ".roger")

    # meeting bot (Attendee)
    attendee_api_key: str | None = None
    attendee_base: str = "https://app.attendee.dev/api/v1"
    attendee_use_login: bool = False  # join Teams / Meet with a signed-in bot account configured in Attendee
    attendee_login_group: str | None = None

    # voice and hearing (OpenAI GPT-Live: one full-duplex session does both)
    live_model: str = LIVE_MODEL
    voice: str = "cedar"  # GPT-Live's male flagship voice; "marin" is its female counterpart
    language: str = "en"
    sample_rate: int = 24000  # GPT-Live's native rate, and what Attendee wants for an OpenAI voice
    # Measured delivery from GPT-Live stalls for up to ~360 ms now and then. The cushion has to be longer
    # than the longest stall or the far end runs dry mid-word, which is heard as distortion rather than a gap.
    output_buffer_ms: int = 500
    echo_suppress: bool = False  # drop incoming audio while the bot speaks, if it ever hears itself

    # the delegated backend
    openai_api_key: str | None = None
    fast_model: str = "gpt-5.6-luna"

    # persona
    bot_name: str = "Roger"
    bot_first_name: str = "Roger"
    owner_name: str | None = None
    wake_words: list[str] = field(default_factory=lambda: ["roger", "rodger", "rojer"])
    greeting: str | None = None

    per_participant_audio: bool = True  # per-speaker streams, so the transcript has names

    # web search on the delegated backend
    web_search: bool = True

    # session context: Claude Code and Codex sessions are READ for context, never run
    claude_session_id: str | None = None
    codex_session_id: str | None = None
    repo_tools: bool = True  # let the backend search and read the project's files
    briefing_model: str = "gpt-5.6-luna"
    backend_effort: str = "none"  # reasoning effort for the delegated backend; latency matters here
    project_dir: str = field(default_factory=lambda: str(Path.cwd()))

    # ------------------------------------------------------------------ construction
    @classmethod
    def from_env(cls) -> "Settings":
        s = cls()
        s.port = int(env("PORT", str(s.port)))
        s.public_url = (env("PUBLIC_URL") or None) and env("PUBLIC_URL").rstrip("/")
        s.state_dir = Path(env("ROGER_STATE_DIR", str(s.state_dir))).expanduser()

        s.attendee_api_key = env("ATTENDEE_API_KEY")
        s.attendee_base = (env("ATTENDEE_BASE", s.attendee_base) or s.attendee_base).rstrip("/")
        s.attendee_use_login = flag("ATTENDEE_USE_LOGIN", False)
        s.attendee_login_group = env("ATTENDEE_LOGIN_GROUP")

        s.live_model = env("LIVE_MODEL", s.live_model)
        s.sample_rate = int(env("AUDIO_RATE", str(s.sample_rate)))
        s.output_buffer_ms = int(env("OUTPUT_BUFFER_MS", str(s.output_buffer_ms)))
        s.voice = (env("VOICE", s.voice) or s.voice).strip()
        s.language = env("LANGUAGE", s.language)
        s.echo_suppress = flag("ECHO_SUPPRESS", False)

        s.openai_api_key = env("OPENAI_API_KEY")
        s.fast_model = env("FAST_MODEL", s.fast_model)

        s.bot_name = env("BOT_NAME", s.bot_name).strip()
        s.bot_first_name = env("BOT_FIRST_NAME", s.bot_name.split()[0] if s.bot_name.split() else "Roger").strip()
        s.owner_name = env("OWNER_NAME")
        first = s.bot_first_name.lower()
        default_wake = ",".join([first] + MISHEARINGS.get(first, []))
        s.wake_words = [w.strip().lower() for w in env("WAKE_WORDS", default_wake).split(",") if w.strip()]
        s.greeting = env("GREETING")

        s.per_participant_audio = flag("PER_PARTICIPANT_AUDIO", True)

        s.web_search = flag("WEB_SEARCH", True)
        s.repo_tools = flag("REPO_TOOLS", True)
        s.briefing_model = env("BRIEFING_MODEL", s.briefing_model)
        s.backend_effort = (env("BACKEND_EFFORT", s.backend_effort) or s.backend_effort).strip().lower()

        s.claude_session_id = env("CLAUDE_SESSION_ID")
        s.codex_session_id = env("CODEX_SESSION_ID")
        s.project_dir = str(Path(env("PROJECT_DIR", s.project_dir)).expanduser())
        return s

    def attach_session(self, session_id: str | None = None, project_dir: str | None = None, engine: str = "claude") -> None:
        """Attach a coding-agent session as context, and/or point at a project folder (command-line overrides)."""
        if project_dir:
            self.project_dir = str(Path(project_dir).expanduser().resolve())
        if session_id:
            if engine == "codex":
                self.codex_session_id = session_id
            else:
                self.claude_session_id = session_id

    # ------------------------------------------------------------------ derived
    @property
    def chunk_bytes(self) -> int:
        """One 100 ms frame of 16-bit mono audio at the configured rate."""
        return int(self.sample_rate * 2 * 0.1)

    @property
    def owner_possessive(self) -> str:
        """``"Alice's"`` or ``"the team's"``."""
        return f"{self.owner_name}'s" if self.owner_name else "the team's"

    def problems(self) -> list[str]:
        """Human-readable configuration problems that would stop a meeting from working."""
        out: list[str] = []
        if not self.attendee_api_key:
            out.append("ATTENDEE_API_KEY is missing (sign up at https://app.attendee.dev, then Settings > API keys)")
        if not self.openai_api_key:
            out.append("OPENAI_API_KEY is missing: GPT-Live is both the ears and the voice (https://platform.openai.com/api-keys)")
        if self.voice not in LIVE_VOICES and not self.voice.startswith("voice_"):
            out.append(f"VOICE is {self.voice!r}, which is not a GPT-Live voice. Pick one of: {', '.join(LIVE_VOICES)}")
        if self.sample_rate not in (8000, 16000, 24000):
            out.append(f"AUDIO_RATE must be 8000, 16000 or 24000, not {self.sample_rate} (GPT-Live accepts 16000 or 24000)")
        elif self.sample_rate == 8000:
            out.append("AUDIO_RATE 8000 is accepted by Attendee but not by GPT-Live over a WebSocket; use 16000 or 24000")
        if not self.public_url and not shutil.which("cloudflared"):
            out.append("cloudflared is not installed and PUBLIC_URL is empty: install cloudflared (brew install cloudflared) or set PUBLIC_URL to a public https URL that reaches this machine")
        return out

    def summary(self) -> dict:
        """Non-secret view of the configuration, for logs and /health."""
        return {
            "bot_name": self.bot_name,
            "wake_words": self.wake_words,
            "fast_model": self.fast_model,
            "live_model": self.live_model,
            "voice": self.voice,
            "sample_rate": self.sample_rate,
            "claude_session": (self.claude_session_id or "")[:8] or None,
            "codex_session": (self.codex_session_id or "")[:8] or None,
            "repo_tools": self.repo_tools,
            "web_search": self.web_search,
            "keys": {"attendee": bool(self.attendee_api_key), "openai": bool(self.openai_api_key)},
        }
