"""What the bot knows before anyone speaks: the briefing.

Roger attaches to the coding sessions you have been working in -- Claude Code, Codex, or both -- but it
never *runs* either of them. Their rollout files are read as text, which is all they are, and one
OpenAI call turns that plus the repository into a briefing. So the bot arrives knowing what was
discussed and decided, without a second vendor, a second bill, or write access to your real sessions.

The briefing is cached per session so restarting during a meeting is quick.
"""
from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path

from .config import Settings
from .prompts import BRIEFING_PROMPT, NO_BRIEFING
from .sessions import SessionInfo, find_session, list_sessions, transcript_text

log = logging.getLogger("roger.context")

BRIEFING_MAX_AGE_S = 6 * 3600
SESSION_CHARS = 36000  # per session, before the model sees it
REPO_CHARS = 6000


def _repo_snapshot(project_dir: str) -> str:
    """A little of what the repository says about itself, so a briefing is grounded even with no session."""
    root = Path(project_dir).expanduser()
    parts: list[str] = []
    for name in ("README.md", "README.rst", "CHANGELOG.md", "pyproject.toml", "package.json"):
        f = root / name
        if f.is_file():
            try:
                parts.append(f"--- {name} ---\n{f.read_text(errors='replace')[:REPO_CHARS // 2]}")
            except OSError:
                continue
    try:
        top = sorted(p.name + ("/" if p.is_dir() else "") for p in root.iterdir() if not p.name.startswith("."))
        parts.append("--- top level ---\n" + ", ".join(top[:60]))
    except OSError:
        pass
    return "\n\n".join(parts)[:REPO_CHARS]


def attached_sessions(s: Settings) -> list[SessionInfo]:
    """The sessions configured as context, in the order they should be read."""
    out: list[SessionInfo] = []
    for engine, wanted in (("claude", s.claude_session_id), ("codex", s.codex_session_id)):
        if not wanted:
            continue
        found = find_session(s.project_dir, wanted, engine)
        if found is None and wanted == "latest":
            rows = list_sessions(s.project_dir, engine)
            found = rows[0] if rows else None
        if found is None:
            log.warning("%s session %r not found for %s", engine, wanted, s.project_dir)
            continue
        out.append(found)
    return out


def _cache_path(s: Settings, sessions: list[SessionInfo]) -> Path:
    key = "-".join(sorted(f"{r.engine}:{r.id[:8]}" for r in sessions)) or "repo:" + s.project_dir
    return s.state_dir / f"briefing-{hashlib.md5(key.encode()).hexdigest()[:10]}.txt"


async def build_briefing(s: Settings) -> str:
    """Read the attached sessions and the repository, and write the briefing. Cached for a few hours."""
    sessions = attached_sessions(s)
    cache = _cache_path(s, sessions)
    if cache.exists() and time.time() - cache.stat().st_mtime < BRIEFING_MAX_AGE_S and cache.stat().st_size > 400:
        text = cache.read_text()
        log.info("using cached briefing (%d chars, %s)", len(text), cache.name)
        return text

    blocks: list[str] = []
    for r in sessions:
        body = transcript_text(r, SESSION_CHARS)
        if body:
            label = f"{r.engine} session {r.id[:8]}" + (f" ({r.title})" if r.title else "")
            blocks.append(f"===== transcript of your {label} =====\n{body}")
            log.info("read %s: %d chars of conversation", label, len(body))
    repo = _repo_snapshot(s.project_dir)
    if repo:
        blocks.append(f"===== the repository at {s.project_dir} =====\n{repo}")
    if not blocks:
        log.warning("no session transcripts and nothing readable in %s; going in without a briefing", s.project_dir)
        return NO_BRIEFING

    t0 = time.time()
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=s.openai_api_key)
    try:
        r = await client.responses.create(
            model=s.briefing_model,
            instructions=BRIEFING_PROMPT,
            input="\n\n".join(blocks),
            max_output_tokens=3000,
            reasoning={"effort": "low"} if s.briefing_model.startswith(("gpt-5", "o")) else None,
        )
        text = (r.output_text or "").strip()
    except Exception as e:
        log.warning("briefing failed (%s); going in without one", e)
        return NO_BRIEFING
    finally:
        await client.close()

    if len(text) < 300:
        log.warning("briefing came back too short (%d chars); going in without one", len(text))
        return NO_BRIEFING
    log.info("briefing ready (%d chars, %.1fs, %s)", len(text), time.time() - t0, s.briefing_model)
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(text)
    except OSError:
        pass
    return text
