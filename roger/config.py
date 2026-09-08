"""Configuration. Everything comes from environment variables, normally through a ``.env`` file.

``.env.example`` at the repository root documents every variable. Only three keys are required:
``ATTENDEE_API_KEY``, ``ELEVENLABS_API_KEY`` and one of ``OPENAI_API_KEY`` / ``ANTHROPIC_API_KEY``.
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

FAST_MODEL_DEFAULTS = {"openai": "gpt-4.1-mini", "anthropic": "claude-haiku-4-5"}
CLASSIFIER_MODEL_DEFAULTS = {"openai": "gpt-4.1-mini", "anthropic": "claude-haiku-4-5"}


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

    # voice (ElevenLabs)
    elevenlabs_api_key: str | None = None
    voice_id: str = "JBFqnCBsd6RMkjVDRZzb"  # "George", a stock voice
    tts_model: str = "eleven_flash_v2_5"
    stt_model: str = "scribe_v2_realtime"
    stt_silence_s: float = 0.6
    voice_stability: float = 0.6
    voice_style: float = 0.0
    language: str = "en"

    # brains
    fast_provider: str = "openai"  # "openai" or "anthropic": who answers on the voice path
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    fast_model: str = "gpt-4.1-mini"
    classifier_model: str = "gpt-4.1-mini"

    # persona
    bot_name: str = "Roger"
    bot_first_name: str = "Roger"
    owner_name: str | None = None
    wake_words: list[str] = field(default_factory=lambda: ["roger", "rodger", "rojer"])
    attention_window_s: float = 30.0
    greeting: str | None = None

    # audio and visuals
    per_participant_audio: bool = True
    orb: bool = True
    audio_out: str = "page"  # "page": the orb page plays the voice; "ws": realtime_audio.bot_output frames
    orb_colors: str = "#FFC7A1,#DF5B37"
    orb_bg: str = "#0a0d10"
    orb_size: int = 520
    barge_in_energy: bool = False

    # deep brain (Claude Code through the Agent SDK)
    deep_enabled: bool = False
    claude_session_id: str | None = None
    project_dir: str = field(default_factory=lambda: str(Path.cwd()))
    deep_model: str = "claude-sonnet-5"
    deep_auth: str = "subscription"  # or "api": bill ANTHROPIC_API_KEY instead of the Claude Code login

    # ------------------------------------------------------------------ construction
    @classmethod
    def from_env(cls) -> "Settings":
        s = cls()
        s.port = int(env("PORT", str(s.port)))
        s.public_url = (env("PUBLIC_URL") or None) and env("PUBLIC_URL").rstrip("/")
        s.state_dir = Path(env("ROGER_STATE_DIR", str(s.state_dir))).expanduser()

        s.attendee_api_key = env("ATTENDEE_API_KEY")
        s.attendee_base = (env("ATTENDEE_BASE", s.attendee_base) or s.attendee_base).rstrip("/")

        s.elevenlabs_api_key = env("ELEVENLABS_API_KEY")
        s.voice_id = env("ELEVENLABS_VOICE_ID", s.voice_id)
        s.tts_model = env("ELEVENLABS_TTS_MODEL", s.tts_model)
        s.stt_model = env("ELEVENLABS_STT_MODEL", s.stt_model)
        s.stt_silence_s = float(env("STT_SILENCE_S", str(s.stt_silence_s)))
        s.voice_stability = float(env("VOICE_STABILITY", str(s.voice_stability)))
        s.voice_style = float(env("VOICE_STYLE", str(s.voice_style)))
        s.language = env("LANGUAGE", s.language)

        s.openai_api_key = env("OPENAI_API_KEY")
        s.anthropic_api_key = env("ANTHROPIC_API_KEY")
        provider = env("FAST_PROVIDER")
        if provider not in ("openai", "anthropic"):
            provider = "openai" if s.openai_api_key or not s.anthropic_api_key else "anthropic"
        s.fast_provider = provider
        s.fast_model = env("FAST_MODEL", FAST_MODEL_DEFAULTS[provider])
        s.classifier_model = env("CLASSIFIER_MODEL", CLASSIFIER_MODEL_DEFAULTS[provider])

        s.bot_name = env("BOT_NAME", s.bot_name).strip()
        s.bot_first_name = env("BOT_FIRST_NAME", s.bot_name.split()[0] if s.bot_name.split() else "Roger").strip()
        s.owner_name = env("OWNER_NAME")
        first = s.bot_first_name.lower()
        default_wake = ",".join([first] + MISHEARINGS.get(first, []))
        s.wake_words = [w.strip().lower() for w in env("WAKE_WORDS", default_wake).split(",") if w.strip()]
        s.attention_window_s = float(env("ATTENTION_WINDOW_S", str(s.attention_window_s)))
        s.greeting = env("GREETING")

        s.per_participant_audio = flag("PER_PARTICIPANT_AUDIO", True)
        s.orb = flag("ORB", True)
        s.audio_out = env("AUDIO_OUT", "page" if s.orb else "ws")
        if not s.orb:
            s.audio_out = "ws"  # without the page there is nothing else to play the voice
        s.orb_colors = env("ORB_COLORS", s.orb_colors)
        s.orb_bg = env("ORB_BG", s.orb_bg)
        s.orb_size = int(env("ORB_SIZE", str(s.orb_size)))
        s.barge_in_energy = flag("BARGE_IN_ENERGY", False)

        s.claude_session_id = env("CLAUDE_SESSION_ID")
        s.project_dir = str(Path(env("PROJECT_DIR", s.project_dir)).expanduser())
        deep = (env("DEEP_AGENT", "auto") or "auto").lower()
        s.deep_enabled = bool(s.claude_session_id or env("PROJECT_DIR")) if deep == "auto" else deep in ("1", "true", "yes", "on")
        s.deep_model = env("DEEP_MODEL", s.deep_model)
        s.deep_auth = env("DEEP_AUTH", "api" if s.anthropic_api_key and not s.claude_session_id else "subscription")
        return s

    def attach_session(self, session_id: str | None = None, project_dir: str | None = None) -> None:
        """Turn the deep brain on for a Claude Code session and/or project folder (command-line overrides)."""
        if project_dir:
            self.project_dir = str(Path(project_dir).expanduser().resolve())
        if session_id:
            self.claude_session_id = session_id
        self.deep_enabled = True
        if not os.getenv("DEEP_AUTH"):
            self.deep_auth = "subscription"

    # ------------------------------------------------------------------ derived
    @property
    def owner_possessive(self) -> str:
        """``"Alice's"`` or ``"the team's"``."""
        return f"{self.owner_name}'s" if self.owner_name else "the team's"

    def problems(self) -> list[str]:
        """Human-readable configuration problems that would stop a meeting from working."""
        out: list[str] = []
        if not self.attendee_api_key:
            out.append("ATTENDEE_API_KEY is missing (sign up at https://app.attendee.dev, then Settings > API keys)")
        if not self.elevenlabs_api_key:
            out.append("ELEVENLABS_API_KEY is missing (https://elevenlabs.io > profile > API keys)")
        if self.fast_provider == "openai" and not self.openai_api_key:
            out.append("FAST_PROVIDER is openai but OPENAI_API_KEY is missing")
        if self.fast_provider == "anthropic" and not self.anthropic_api_key:
            out.append("FAST_PROVIDER is anthropic but ANTHROPIC_API_KEY is missing")
        if not self.public_url and not shutil.which("cloudflared"):
            out.append("cloudflared is not installed and PUBLIC_URL is empty: install cloudflared (brew install cloudflared) or set PUBLIC_URL to a public https URL that reaches this machine")
        if self.audio_out not in ("page", "ws"):
            out.append(f"AUDIO_OUT must be 'page' or 'ws', not {self.audio_out!r}")
        return out

    def summary(self) -> dict:
        """Non-secret view of the configuration, for logs and /health."""
        return {
            "bot_name": self.bot_name,
            "wake_words": self.wake_words,
            "fast_provider": self.fast_provider,
            "fast_model": self.fast_model,
            "classifier_model": self.classifier_model,
            "voice_id": self.voice_id,
            "tts_model": self.tts_model,
            "stt_model": self.stt_model,
            "deep_agent": self.deep_enabled,
            "deep_model": self.deep_model if self.deep_enabled else None,
            "claude_session": (self.claude_session_id or "")[:8] or None,
            "audio_out": self.audio_out,
            "orb": self.orb,
            "keys": {
                "attendee": bool(self.attendee_api_key),
                "elevenlabs": bool(self.elevenlabs_api_key),
                "openai": bool(self.openai_api_key),
                "anthropic": bool(self.anthropic_api_key),
            },
        }
