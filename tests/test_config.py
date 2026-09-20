import os

import pytest

from roger.config import Settings


@pytest.fixture
def clean_env(monkeypatch):
    for k in list(os.environ):
        if k in {"OPENAI_API_KEY", "FAST_MODEL", "BOT_NAME", "BOT_FIRST_NAME", "WAKE_WORDS", "OWNER_NAME",
                 "CLAUDE_SESSION_ID", "CODEX_SESSION_ID", "PROJECT_DIR", "REPO_TOOLS", "WEB_SEARCH",
                 "VOICE", "LIVE_MODEL", "AUDIO_RATE", "ECHO_SUPPRESS", "GREETING"}:
            monkeypatch.delenv(k, raising=False)
    return monkeypatch


def test_the_backend_model_defaults_to_luna(clean_env):
    assert Settings.from_env().fast_model == "gpt-5.6-luna"
    clean_env.setenv("FAST_MODEL", "gpt-5.6-sol")
    assert Settings.from_env().fast_model == "gpt-5.6-sol"


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


def test_sessions_are_context_not_agents(clean_env):
    """Claude Code and Codex sessions are read for context; neither is ever run."""
    s = Settings.from_env()
    assert s.claude_session_id is None and s.codex_session_id is None
    clean_env.setenv("CLAUDE_SESSION_ID", "abc")
    clean_env.setenv("CODEX_SESSION_ID", "def")
    s = Settings.from_env()
    assert s.claude_session_id == "abc" and s.codex_session_id == "def"


def test_repo_tools_and_web_search_default_on(clean_env):
    s = Settings.from_env()
    assert s.repo_tools and s.web_search
    clean_env.setenv("WEB_SEARCH", "0")
    assert Settings.from_env().web_search is False


def test_attach_session_from_the_command_line(clean_env, tmp_path):
    s = Settings.from_env()
    s.attach_session("abc123", str(tmp_path), "claude")
    assert s.claude_session_id == "abc123" and s.project_dir == str(tmp_path.resolve())
    s.attach_session("codex789", None, "codex")
    assert s.codex_session_id == "codex789"
    assert s.claude_session_id == "abc123"  # both can be attached at once


def test_openai_key_is_required_because_gpt_live_is_the_voice(clean_env):
    problems = Settings.from_env().problems()
    assert any("OPENAI_API_KEY" in p for p in problems)
    clean_env.setenv("OPENAI_API_KEY", "y")
    assert not any("OPENAI_API_KEY" in p for p in Settings.from_env().problems())


def test_an_unknown_voice_is_reported(clean_env):
    clean_env.setenv("OPENAI_API_KEY", "y")
    assert not any("VOICE" in p for p in Settings.from_env().problems())
    clean_env.setenv("VOICE", "george")
    assert any("not a GPT-Live voice" in p for p in Settings.from_env().problems())
    clean_env.setenv("VOICE", "cedar")
    assert not any("VOICE" in p for p in Settings.from_env().problems())


def test_echo_suppression_is_off_by_default(clean_env):
    """The meeting's mixed stream does not carry the bot's own voice, and gating it would cost barge-in."""
    clean_env.setenv("OPENAI_API_KEY", "y")
    assert Settings.from_env().echo_suppress is False
    clean_env.setenv("ECHO_SUPPRESS", "1")
    assert Settings.from_env().echo_suppress is True
