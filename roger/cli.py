"""Command line: ``roger run <meeting-url>`` is the whole product; the rest is for testing and operating it."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import shutil
import sys
from pathlib import Path

import httpx

from . import __version__
from .config import LIVE_VOICES, Settings, load_env
from .sessions import find_session, list_all_sessions, list_sessions, sessions_dir


def setup_logging(s: Settings) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-16s %(message)s", datefmt="%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)
    try:
        s.state_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(s.state_dir / "roger.log", maxBytes=5_000_000, backupCount=3)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)-16s %(message)s"))
        root.addHandler(fh)
    except OSError:
        pass
    for noisy in ("httpx", "httpx2", "httpcore", "websockets", "aiohttp.access", "claude_agent_sdk"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def fail(msg: str, code: int = 2) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


# --------------------------------------------------------------------------- server side


def cmd_serve(s: Settings, meeting_url: str | None, leave_on_exit: bool) -> None:
    problems = s.problems()
    # Without a meeting to join, anything about getting into one is a warning, not a stopper: the monitor
    # page and `roger ask` are worth having on their own.
    soft = {"ATTENDEE_API_KEY", "cloudflared", "PUBLIC_URL"}
    hard = [p for p in problems if not (not meeting_url and any(k in p for k in soft))]
    for p in problems:
        if p not in hard:
            print(f"warning: {p} (local testing still works)", file=sys.stderr)
    if hard:
        for p in hard:
            print(f"error: {p}", file=sys.stderr)
        print("Fix the .env file (see .env.example) or run `roger doctor`.", file=sys.stderr)
        sys.exit(2)

    setup_logging(s)
    log = logging.getLogger("roger")
    from .server import public_url, serve

    s.public_url = public_url(s)
    if s.hosted_participant and not s.public_url:
        log.warning("no public URL: the hosted participant cannot reach this machine, so it cannot join a meeting")
    asyncio.run(serve(s, meeting_url=meeting_url, leave_on_exit=leave_on_exit))


# --------------------------------------------------------------------------- client side (talks to a running server)


def client(s: Settings) -> httpx.Client:
    return httpx.Client(base_url=f"http://127.0.0.1:{s.port}", timeout=60)


def call(s: Settings, method: str, path: str, body: dict | None = None) -> dict:
    try:
        with client(s) as c:
            r = c.request(method, path, json=body)
    except httpx.ConnectError:
        fail(f"no server on port {s.port}. Start one with `roger serve` (or use `roger run <meeting-url>`)")
    try:
        data = r.json()
    except ValueError:
        data = {"status": r.status_code, "body": r.text[:300]}
    if r.status_code >= 300:
        fail(data.get("error") or json.dumps(data), code=1)
    return data


def cmd_join(s: Settings, meeting_url: str) -> None:
    data = call(s, "POST", "/join", {"meeting_url": meeting_url})
    print(f"bot {data['bot_id']} is joining. If it lands in the waiting room, admit \"{s.bot_name}\" from the meeting.")


def cmd_leave(s: Settings) -> None:
    call(s, "POST", "/leave")
    print("asked the bot to leave")


def cmd_say(s: Settings, text: str) -> None:
    call(s, "POST", "/say", {"text": text})
    print("queued")


def cmd_ask(s: Settings, text: str, speaker: str) -> None:
    call(s, "POST", "/ask", {"text": text, "speaker": speaker})
    print("sent; watch the server log (and open /monitor to hear the reply)")


def cmd_status(s: Settings) -> None:
    print(json.dumps(call(s, "GET", "/health"), indent=2))


# --------------------------------------------------------------------------- local commands


def cmd_sessions(project_dir: str | None) -> None:
    project = Path(project_dir or Path.cwd()).expanduser().resolve()
    rows = list_all_sessions(project)
    if not rows:
        fail(f"no Claude Code or Codex sessions found for {project} (looked in {sessions_dir(project)} and ~/.codex/sessions)", code=1)
    print(f"Coding sessions for {project} (newest first):\n")
    for r in rows:
        name = f"[{r.title}]  " if r.title else ""
        print(f"{r.engine:6}  {r.id}  {r.size_mb:6.1f} MB  {r.age_h:7.1f} h ago  {r.lines:6d} lines  {name}{r.first_message}")
    print("\nAttach one with `roger run <url> --session <id or title>` (add --engine codex for a Codex one),")
    print("or put CLAUDE_SESSION_ID / CODEX_SESSION_ID and PROJECT_DIR in .env. Sessions are read for context, never run.")


def _chromium_installed() -> bool:
    """Has `playwright install chromium` been run? It downloads into a well-known cache."""
    import glob

    roots = [Path.home() / "Library/Caches/ms-playwright", Path.home() / ".cache/ms-playwright"]
    return any(glob.glob(str(r / "chromium*")) for r in roots if r.exists())


def cmd_doctor(s: Settings, env_files: list[Path]) -> None:
    ok, bad = "ok ", "!! "
    lines: list[tuple[str, str, str]] = []

    lines.append((ok if env_files else bad, ".env", ", ".join(map(str, env_files)) if env_files else "not found: copy .env.example to .env in this directory (or to ~/.roger/.env) and fill in your keys"))
    lines.append((ok if s.openai_api_key else bad, "OpenAI key", "set" if s.openai_api_key else "missing (https://platform.openai.com/api-keys): GPT-Live needs it to hear and speak"))

    from .meeting import browser_available, supported_platforms

    if s.hosted_participant:
        hosted = "app.attendee.dev" in s.attendee_base
        lines.append((ok, "Participant", "attendee (hosted service)"))
        lines.append((ok if s.attendee_api_key else bad, "Attendee key", "set" if s.attendee_api_key else "missing (https://app.attendee.dev)"))
        lines.append((ok, "Attendee", s.attendee_base + ("" if hosted else "  (self-hosted)")))
    else:
        have = browser_available()
        lines.append((ok, "Participant", "browser on this machine" + ("" if s.browser_headless else " (visible)")))
        lines.append((ok if have else bad, "Playwright", "installed" if have else "missing: pip install 'roger-meeting-agent[browser]' && playwright install chromium"))
        if have:
            lines.append((ok if _chromium_installed() else bad, "Chromium", "installed" if _chromium_installed() else "not downloaded yet: playwright install chromium"))
        lines.append((ok, "Platforms", supported_platforms()))
    voice_ok = s.voice in LIVE_VOICES or s.voice.startswith("voice_")
    lines.append((ok if voice_ok else bad, "Voice", f"{s.live_model} / {s.voice}" + ("" if voice_ok else f"  (unknown voice; try one of: {', '.join(LIVE_VOICES[:8])} ...)")))
    lines.append((ok, "Backend model", s.fast_model))
    if s.public_url:
        lines.append((ok, "Public URL", s.public_url))
    elif s.hosted_participant:
        cf = shutil.which("cloudflared")
        lines.append((ok if cf else bad, "Tunnel", f"cloudflared at {cf}" if cf else "cloudflared not found: brew install cloudflared, or set PUBLIC_URL"))
    from .context import attached_sessions

    found = attached_sessions(s)
    if s.claude_session_id or s.codex_session_id:
        for r in found:
            lines.append((ok, f"{r.engine.capitalize()} session", f"{r.id[:8]}  [{r.label[:50]}]  {r.lines} lines, {r.age_h:.1f} h old"))
        for engine, wanted in (("claude", s.claude_session_id), ("codex", s.codex_session_id)):
            if wanted and not any(r.engine == engine for r in found):
                lines.append((bad, f"{engine.capitalize()} session", f"{wanted!r} not found for {s.project_dir}; run `roger sessions`"))
    else:
        lines.append((ok, "Sessions", "none attached; the briefing will come from the repository alone"))
    lines.append((ok, "Repo tools", f"search and read under {s.project_dir}" if s.repo_tools else "off (REPO_TOOLS=0)"))
    lines.append((ok, "Web search", "on" if s.web_search else "off (WEB_SEARCH=0)"))
    lines.append((ok, "Briefing model", s.briefing_model))
    lines.append((ok, "Audio", f"{s.sample_rate} Hz, {s.output_buffer_ms} ms buffer" + (", echo suppression on" if s.echo_suppress else "")))
    lines.append((ok, "State dir", str(s.state_dir)))
    lines.append((ok, "Persona", f"{s.bot_name}; wake words: {', '.join(s.wake_words)}"))

    width = max(len(name) for _, name, _ in lines)
    for mark, name, detail in lines:
        print(f"  {mark} {name.ljust(width)}  {detail}")
    problems = [m for m, _, _ in lines if m == bad]
    if problems:
        print(f"\n{len(problems)} problem(s). See .env.example for every setting.")
        sys.exit(1)
    print("\nAll good. Try: roger run https://meet.google.com/xxx-xxxx-xxx")


def apply_deep_flags(s: Settings, session: str | None, project: str | None, engine: str | None = None) -> None:
    """--session / --project / --engine: attach a coding session as context from the command line."""
    if not session and not project:
        return
    project_dir = str(Path(project).expanduser().resolve()) if project else s.project_dir
    found_engine = engine or "claude"
    if session == "latest":
        rows = list_sessions(project_dir, engine) if engine else list_all_sessions(project_dir)
        if not rows:
            fail(f"no sessions found for {project_dir}; pass --project <dir> or a session id")
        session, found_engine = rows[0].id, rows[0].engine
        print(f"attaching the newest {found_engine} session for {project_dir}: {session[:8]}  ({rows[0].age_h:.1f} h old: {rows[0].label[:60]})")
    elif session:
        found = find_session(project_dir, session, engine)
        if not found:
            fail(f"no session matching {session!r} for {project_dir}; run `roger sessions <project-dir>` to see ids and titles")
        found_engine = found.engine
        if found.id != session:
            print(f"attaching {found_engine} session {found.id[:8]}  [{found.label[:60]}]")
        session = found.id
    s.attach_session(session, project, found_engine)


# --------------------------------------------------------------------------- entry point


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="roger", description="Roger: a voice AI teammate that joins your Google Meet, Microsoft Teams and Zoom calls.")
    p.add_argument("--env", metavar="FILE", help="path to a .env file (default: ./.env, then the repository root)")
    p.add_argument("--version", action="version", version=f"roger {__version__}")
    sub = p.add_subparsers(dest="command", required=True, metavar="command")

    def deep_flags(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--session", metavar="ID", help="a Claude Code or Codex session to read for context: an id (or its first characters), a title, or 'latest' for the newest in the project")
        parser.add_argument("--project", metavar="DIR", help="the project folder: what the repo tools search (overrides PROJECT_DIR)")
        parser.add_argument("--engine", choices=["claude", "codex"], help="which client the session belongs to (default: search both)")

    sp = sub.add_parser("run", help="start everything, send the bot into a meeting, leave when you press Ctrl-C")
    sp.add_argument("meeting_url", help="Google Meet, Microsoft Teams or Zoom link")
    deep_flags(sp)

    sp = sub.add_parser("serve", help="start the server (and a Cloudflare quick tunnel) and keep it running")
    deep_flags(sp)

    sp = sub.add_parser("join", help="send the bot into a meeting (needs `roger serve` running)")
    sp.add_argument("meeting_url")

    sub.add_parser("leave", help="make the bot leave the meeting")

    sp = sub.add_parser("say", help="make the bot say something")
    sp.add_argument("text")

    sp = sub.add_parser("ask", help="simulate someone talking to the bot, no meeting needed")
    sp.add_argument("text")
    sp.add_argument("--speaker", default="Tester")

    sub.add_parser("status", help="show the running server's health as JSON")

    sp = sub.add_parser("sessions", help="list Claude Code sessions you can attach as the deep brain")
    sp.add_argument("project_dir", nargs="?", help="project folder (default: current directory)")

    sub.add_parser("doctor", help="check configuration, keys and tools")
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    env_files = load_env(args.env)
    s = Settings.from_env()

    if args.command in ("run", "serve"):
        apply_deep_flags(s, args.session, args.project, args.engine)
    if args.command == "run":
        cmd_serve(s, args.meeting_url, leave_on_exit=True)
    elif args.command == "serve":
        cmd_serve(s, None, leave_on_exit=False)
    elif args.command == "join":
        cmd_join(s, args.meeting_url)
    elif args.command == "leave":
        cmd_leave(s)
    elif args.command == "say":
        cmd_say(s, args.text)
    elif args.command == "ask":
        cmd_ask(s, args.text, args.speaker)
    elif args.command == "status":
        cmd_status(s)
    elif args.command == "sessions":
        cmd_sessions(args.project_dir)
    elif args.command == "doctor":
        cmd_doctor(s, env_files)


if __name__ == "__main__":
    main()
