"""Find coding-agent sessions on this machine, so one can be attached as the deep brain.

Two engines are supported and they store their history quite differently:

``claude``  ``~/.claude/projects/<project path with non-alphanumerics as dashes>/<session-id>.jsonl``
``codex``   ``~/.codex/sessions/<yyyy>/<mm>/<dd>/rollout-<stamp>-<session-id>.jsonl``, with the project
            folder recorded in the first line's ``session_meta`` and the title in ``~/.codex/session_index.jsonl``

Both are reduced to :class:`SessionInfo` so the rest of Roger does not care which one it got.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

ENGINES = ("claude", "codex")


@dataclass
class SessionInfo:
    id: str
    path: Path
    size_mb: float
    age_h: float
    lines: int
    first_message: str
    title: str = ""  # the name given to the session in its own client, if any
    engine: str = "claude"

    @property
    def label(self) -> str:
        return self.title or self.first_message


# --------------------------------------------------------------------------- Claude Code


def sessions_dir(project_dir: str | Path) -> Path:
    """Claude Code stores sessions under ``~/.claude/projects/<project path with non-alphanumerics as dashes>/``."""
    encoded = "".join(c if c.isalnum() else "-" for c in str(Path(project_dir).expanduser().resolve()))
    return Path.home() / ".claude" / "projects" / encoded


def list_claude_sessions(project_dir: str | Path) -> list[SessionInfo]:
    root = sessions_dir(project_dir)
    if not root.exists():
        return []
    out: list[SessionInfo] = []
    for f in sorted(root.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        first = ""
        title = ""
        n = 0
        with f.open(errors="replace") as fh:
            for line in fh:
                n += 1
                if first and '"custom-title"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") == "custom-title" and rec.get("customTitle"):
                    title = str(rec["customTitle"]).strip()  # the last one wins
                elif not first and rec.get("type") == "user":
                    content = rec.get("message", {}).get("content", "")
                    if isinstance(content, list):
                        content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
                    first = " ".join(str(content).split())[:90]
        out.append(SessionInfo(f.stem, f, f.stat().st_size / 1e6, (time.time() - f.stat().st_mtime) / 3600, n, first, title, "claude"))
    return out


# --------------------------------------------------------------------------- Codex


def codex_home() -> Path:
    return Path(os.getenv("CODEX_HOME", "~/.codex")).expanduser()


def _codex_titles() -> dict[str, str]:
    """``session_index.jsonl`` maps a session id to the thread name shown in Codex."""
    index = codex_home() / "session_index.jsonl"
    titles: dict[str, str] = {}
    if not index.exists():
        return titles
    with index.open(errors="replace") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("id") and rec.get("thread_name"):
                titles[str(rec["id"])] = str(rec["thread_name"]).strip()  # later entries win
    return titles


def _codex_head(path: Path) -> tuple[str, str, str, int]:
    """``(session_id, cwd, first user message, line count)`` from a rollout file."""
    session_id = cwd = first = ""
    n = 0
    with path.open(errors="replace") as fh:
        for line in fh:
            n += 1
            if session_id and cwd and first:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = rec.get("payload") or {}
            if rec.get("type") == "session_meta":
                session_id = str(payload.get("session_id") or payload.get("id") or "")
                cwd = str(payload.get("cwd") or "")
            elif not first:
                # user turns appear as response items with a content list of input_text parts
                if payload.get("role") == "user" or rec.get("type") in ("user_message", "event_msg"):
                    content = payload.get("content") or payload.get("message") or ""
                    if isinstance(content, list):
                        content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
                    text = " ".join(str(content).split())
                    if text and not text.startswith("<"):
                        first = text[:90]
    return session_id, cwd, first, n


def list_codex_sessions(project_dir: str | Path | None = None) -> list[SessionInfo]:
    """Codex sessions, newest first, optionally only those started in ``project_dir``."""
    root = codex_home() / "sessions"
    if not root.exists():
        return []
    want = str(Path(project_dir).expanduser().resolve()) if project_dir else None
    titles = _codex_titles()
    out: list[SessionInfo] = []
    seen: set[str] = set()  # resuming a session writes another rollout file under the same id
    for f in sorted(root.rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            session_id, cwd, first, n = _codex_head(f)
        except OSError:
            continue
        if not session_id or session_id in seen:
            continue
        if want and cwd and Path(cwd).resolve(strict=False) != Path(want):
            continue
        seen.add(session_id)
        out.append(
            SessionInfo(session_id, f, f.stat().st_size / 1e6, (time.time() - f.stat().st_mtime) / 3600, n, first, titles.get(session_id, ""), "codex")
        )
    return out


# --------------------------------------------------------------------------- either engine


def list_sessions(project_dir: str | Path, engine: str = "claude") -> list[SessionInfo]:
    if engine == "codex":
        return list_codex_sessions(project_dir)
    return list_claude_sessions(project_dir)


def list_all_sessions(project_dir: str | Path) -> list[SessionInfo]:
    """Both engines together, newest first."""
    return sorted(list_claude_sessions(project_dir) + list_codex_sessions(project_dir), key=lambda r: r.age_h)


def find_session(project_dir: str | Path, query: str, engine: str | None = None) -> SessionInfo | None:
    """Match a session by id prefix or by (case-insensitive) title; newest wins on ties."""
    q = query.strip().lower()
    rows = list_sessions(project_dir, engine) if engine else list_all_sessions(project_dir)
    for r in rows:
        if r.id.lower().startswith(q) or (r.title and q in r.title.lower()):
            return r
    return None


# --------------------------------------------------------------------------- reading a session for context

SKIP_PREFIXES = ("<command-", "<local-command", "<system-reminder", "[Request interrupted", "Caveat:")


def _clean(text: str, limit: int = 4000) -> str:
    text = " ".join(str(text).split())
    return text[:limit]


def _claude_turns(path: Path) -> list[tuple[str, str]]:
    """``(role, text)`` pairs from a Claude Code session, tool calls and thinking left out."""
    turns: list[tuple[str, str]] = []
    with path.open(errors="replace") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") not in ("user", "assistant") or rec.get("isSidechain"):
                continue
            message = rec.get("message") or {}
            content = message.get("content")
            parts: list[str] = []
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "text" and c.get("text"):
                        parts.append(str(c["text"]))
            text = _clean(" ".join(parts))
            if text and not text.startswith(SKIP_PREFIXES):
                turns.append((rec["type"], text))
    return turns


def _codex_turns(path: Path) -> list[tuple[str, str]]:
    """``(role, text)`` pairs from a Codex rollout file."""
    turns: list[tuple[str, str]] = []
    with path.open(errors="replace") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = rec.get("payload") or {}
            role = payload.get("role")
            if role not in ("user", "assistant"):
                continue
            content = payload.get("content")
            parts: list[str] = []
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("text"):
                        parts.append(str(c["text"]))
            text = _clean(" ".join(parts))
            if text and not text.startswith(SKIP_PREFIXES):
                turns.append((role, text))
    return turns


def transcript_text(info: SessionInfo, max_chars: int = 40000) -> str:
    """The conversation in a session, newest kept, as ``You:`` / ``Assistant:`` lines.

    This is the whole point of attaching a session: what was discussed, decided and built in it is the
    context the meeting needs. Tool calls, thinking blocks and harness noise are dropped -- they are
    bulk without meaning here. The tail is kept because recent work is what people ask about.
    """
    turns = _codex_turns(info.path) if info.engine == "codex" else _claude_turns(info.path)
    lines = [f"{'You' if r == 'user' else 'Assistant'}: {t}" for r, t in turns]
    out: list[str] = []
    total = 0
    for line in reversed(lines):
        if total + len(line) > max_chars:
            break
        out.append(line)
        total += len(line)
    return "\n".join(reversed(out))
