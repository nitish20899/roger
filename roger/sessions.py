"""Find Claude Code sessions on this machine, so one can be attached as the deep brain."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SessionInfo:
    id: str
    path: Path
    size_mb: float
    age_h: float
    lines: int
    first_message: str
    title: str = ""  # the name given to the session in Claude Code, if any

    @property
    def label(self) -> str:
        return self.title or self.first_message


def sessions_dir(project_dir: str | Path) -> Path:
    """Claude Code stores sessions under ``~/.claude/projects/<project path with non-alphanumerics as dashes>/``."""
    encoded = "".join(c if c.isalnum() else "-" for c in str(Path(project_dir).expanduser().resolve()))
    return Path.home() / ".claude" / "projects" / encoded


def list_sessions(project_dir: str | Path) -> list[SessionInfo]:
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
        out.append(SessionInfo(f.stem, f, f.stat().st_size / 1e6, (time.time() - f.stat().st_mtime) / 3600, n, first, title))
    return out


def find_session(project_dir: str | Path, query: str) -> SessionInfo | None:
    """Match a session by id prefix or by (case-insensitive) title; newest wins on ties."""
    q = query.strip().lower()
    for r in list_sessions(project_dir):
        if r.id.lower().startswith(q) or (r.title and q in r.title.lower()):
            return r
    return None
