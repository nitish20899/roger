"""The tools GPT-Live's backend can call, and the code that runs them.

GPT-Live's Responses backend does the reasoning; these are the hands. They are declared to the session
as function tools and executed here, in this process, so nothing about the repository or the meeting
leaves the machine except the snippets the backend actually asks for.

Everything is read-only and confined to ``PROJECT_DIR``. There is deliberately no tool that edits a
file, runs a command or pushes anything: a meeting bot with the repository open should be able to
answer questions about the code and nothing else.
"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Awaitable, Callable

log = logging.getLogger("roger.tools")

MAX_RESULTS = 40
MAX_FILE_CHARS = 12000
MAX_MATCH_CHARS = 6000
SEARCH_TIMEOUT_S = 20
CHAT_LIMIT = 450

# Noise that is never what a question in a meeting is about.
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next", ".pytest_cache", ".mypy_cache", "target", "vendor"}


def tool_definitions(deep: bool, chat: bool) -> list[dict[str, Any]]:
    """The function tools handed to the Responses backend in ``session.start``."""
    tools: list[dict[str, Any]] = []
    if deep:
        tools += [
            {
                "type": "function",
                "name": "search_repo",
                "description": (
                    "Search the project's files for a string or regular expression. Use this first for any question "
                    "about how something works, where something lives, or whether something exists. Returns matching "
                    "lines with their file and line number."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Text or regular expression to look for."},
                        "file_glob": {"type": "string", "description": "Optional filter such as '*.py' or 'src/**/*.ts'."},
                    },
                    "required": ["query"],
                },
            },
            {
                "type": "function",
                "name": "read_file",
                "description": "Read a file from the project, or a slice of one. Use after search_repo to see the code around a match.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Path relative to the project root."},
                        "start_line": {"type": "integer", "description": "1-based first line. Omit for the start of the file."},
                        "line_count": {"type": "integer", "description": "How many lines to read. Omit for a sensible default."},
                    },
                    "required": ["path"],
                },
            },
            {
                "type": "function",
                "name": "list_files",
                "description": "List files in the project matching a glob, to get your bearings or confirm a path.",
                "parameters": {
                    "type": "object",
                    "properties": {"file_glob": {"type": "string", "description": "A glob such as '*.py', 'roger/*', or '**/test_*.py'."}},
                    "required": ["file_glob"],
                },
            },
        ]
    if chat:
        tools.append(
            {
                "type": "function",
                "name": "post_chat",
                "description": (
                    "Put text into the meeting chat. ONLY for code, file paths, URLs or a list that cannot be spoken aloud. "
                    "Never use it for an ordinary answer. Plain text, under 450 characters."
                ),
                "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            }
        )
    return tools


class Toolbox:
    """Runs the function calls the backend asks for."""

    def __init__(self, project_dir: str, post_chat: Callable[[str], Awaitable[None]]) -> None:
        self.root = Path(project_dir).expanduser().resolve()
        self.post_chat = post_chat
        self.calls = 0

    # ---------------------------------------------------------------- safety
    def _resolve(self, rel: str) -> Path | None:
        """Resolve a path inside the project, or ``None`` if it escapes it."""
        try:
            p = (self.root / str(rel).lstrip("/")).resolve()
        except (OSError, ValueError):
            return None
        return p if p == self.root or self.root in p.parents else None

    def _walk(self, pattern: str = "*") -> list[Path]:
        out: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            for name in filenames:
                p = Path(dirpath) / name
                rel = str(p.relative_to(self.root))
                if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(name, pattern):
                    out.append(p)
            if len(out) > 4000:
                break
        return out

    # ---------------------------------------------------------------- tools
    async def search_repo(self, query: str, file_glob: str | None = None) -> str:
        if not query.strip():
            return "No query given."
        rg = shutil.which("rg")
        if rg:
            argv = [rg, "--line-number", "--no-heading", "--color=never", "--max-count", "4", "-i", "-e", query]
            for d in SKIP_DIRS:
                argv += ["--glob", f"!{d}/**"]
            if file_glob:
                argv += ["--glob", file_glob]
            argv.append(str(self.root))
            try:
                proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, cwd=self.root)
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=SEARCH_TIMEOUT_S)
                lines = [ln.replace(str(self.root) + "/", "") for ln in out.decode(errors="replace").splitlines() if ln.strip()]
            except (asyncio.TimeoutError, OSError):
                lines = []
        else:
            lines = await asyncio.to_thread(self._grep, query, file_glob)
        if not lines:
            return f"No matches for {query!r}."
        return f"{len(lines)} match(es):\n" + "\n".join(lines[:MAX_RESULTS])[:MAX_MATCH_CHARS]

    def _grep(self, query: str, file_glob: str | None) -> list[str]:
        import re

        try:
            rx = re.compile(query, re.I)
        except re.error:
            rx = re.compile(re.escape(query), re.I)
        found: list[str] = []
        for p in self._walk(file_glob or "*"):
            try:
                if p.stat().st_size > 2_000_000:
                    continue
                with p.open(errors="replace") as fh:
                    for i, line in enumerate(fh, 1):
                        if rx.search(line):
                            found.append(f"{p.relative_to(self.root)}:{i}:{line.strip()[:200]}")
                            if len(found) >= MAX_RESULTS:
                                return found
            except OSError:
                continue
        return found

    async def read_file(self, path: str, start_line: int | None = None, line_count: int | None = None) -> str:
        p = self._resolve(path)
        if p is None:
            return f"{path} is outside the project."
        if not p.is_file():
            return f"{path} does not exist."
        try:
            text = await asyncio.to_thread(p.read_text, errors="replace")
        except OSError as e:
            return f"Could not read {path}: {e}"
        lines = text.splitlines()
        start = max(1, start_line or 1)
        count = line_count or (len(lines) if start_line is None else 120)
        chunk = lines[start - 1 : start - 1 + count]
        body = "\n".join(f"{start + i}: {ln}" for i, ln in enumerate(chunk))
        if len(body) > MAX_FILE_CHARS:
            body = body[:MAX_FILE_CHARS] + "\n... (truncated)"
        return f"{path} (lines {start}-{start + len(chunk) - 1} of {len(lines)}):\n{body}"

    async def list_files(self, file_glob: str) -> str:
        paths = await asyncio.to_thread(self._walk, file_glob or "*")
        rel = sorted(str(p.relative_to(self.root)) for p in paths)
        if not rel:
            return f"Nothing matches {file_glob!r}."
        return f"{len(rel)} file(s):\n" + "\n".join(rel[:MAX_RESULTS * 3])

    async def chat(self, text: str) -> str:
        text = str(text).strip()
        if not text:
            return "Nothing to post."
        await self.post_chat(text[: CHAT_LIMIT * 3])
        return "Posted to the meeting chat."

    # ---------------------------------------------------------------- dispatch
    async def run(self, name: str, arguments: str | dict) -> str:
        """Execute one function call and return its result as text for the backend."""
        self.calls += 1
        args: dict[str, Any] = arguments if isinstance(arguments, dict) else {}
        if isinstance(arguments, str) and arguments.strip():
            try:
                args = json.loads(arguments)
            except json.JSONDecodeError:
                return "The arguments were not valid JSON."
        try:
            if name == "search_repo":
                return await self.search_repo(str(args.get("query", "")), args.get("file_glob"))
            if name == "read_file":
                return await self.read_file(str(args.get("path", "")), args.get("start_line"), args.get("line_count"))
            if name == "list_files":
                return await self.list_files(str(args.get("file_glob", "*")))
            if name == "post_chat":
                return await self.chat(str(args.get("text", "")))
        except Exception as e:  # a tool must never take the meeting down
            log.warning("tool %s failed: %s", name, e)
            return f"{name} failed: {e}"
        return f"There is no tool called {name}."
