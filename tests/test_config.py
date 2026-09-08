import os

import pytest

from roger.config import Settings


@pytest.fixture
def clean_env(monkeypatch):
    for k in list(os.environ):
        if k in {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "FAST_PROVIDER", "FAST_MODEL", "CLASSIFIER_MODEL", "BOT_NAME", "BOT_FIRST_NAME", "WAKE_WORDS", "OWNER_NAME", "CLAUDE_SESSION_ID", "PROJECT_DIR", "DEEP_AGENT", "DEEP_AUTH", "ORB", "AUDIO_OUT"}:
            monkeypatch.delenv(k, raising=False)
    return monkeypatch


def test_provider_follows_the_key_you_have(clean_env):
    clean_env.setenv("ANTHROPIC_API_KEY", "x")
    s = Settings.from_env()
    assert s.fast_provider == "anthropic" and s.fast_model == "claude-haiku-4-5" and s.classifier_model == "claude-haiku-4-5"
    clean_env.setenv("OPENAI_API_KEY", "y")
    s = Settings.from_env()
    assert s.fast_provider == "openai" and s.fast_model == "gpt-4.1-mini"


def test_wake_words_derive_from_the_name(clean_env):
    clean_env.setenv("BOT_NAME", "Roger (AI)")
    s = Settings.from_env()
    assert s.bot_first_name == "Roger" and s.wake_words[0] == "roger" and "rodger" in s.wake_words
    clean_env.setenv("BOT_NAME", "Ada")
    assert Settings.from_env().wake_words == ["ada"]


def test_owner_possessive(clean_env):
    assert Settings.from_env().owner_possessive == "the team's"
    clean_env.setenv("OWNER_NAME", "Alice")
    assert Settings.from_env().owner_possessive == "Alice's"


def test_deep_brain_is_off_unless_configured(clean_env):
    assert Settings.from_env().deep_enabled is False
    clean_env.setenv("CLAUDE_SESSION_ID", "abc")
    s = Settings.from_env()
    assert s.deep_enabled is True and s.deep_auth == "subscription"


def test_no_orb_forces_ws_audio(clean_env):
    clean_env.setenv("ORB", "0")
    assert Settings.from_env().audio_out == "ws"
