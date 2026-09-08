"""Command line: ``roger run <meeting-url>`` is the whole product; the rest is for testing and operating it."""
from __future__ import annotations

import argparse
import asyncio
import atexit
import json
import logging
import logging.handlers
import shutil
import sys
from pathlib import Path

import httpx

from . import __version__
from .config import STATIC_DIR, Settings, load_env
from .sessions import list_sessions, sessions_dir


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
    soft = {"ATTENDEE_API_KEY", "cloudflared"}
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
    if not s.public_url and shutil.which("cloudflared"):
        from .tunnel import start_tunnel, stop_tunnel

        log.info("starting a Cloudflare quick tunnel ...")
        url, proc = start_tunnel(s.port)
        atexit.register(stop_tunnel, proc)
        s.public_url = url
        log.info("tunnel: %s", url)
    elif not s.public_url:
        log.warning("no public URL: the bot cannot join meetings from this run, but the monitor page and `roger ask` work")

    from .server import serve

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
    rows = list_sessions(project)
    if not rows:
        fail(f"no Claude Code sessions found for {project} (looked in {sessions_dir(project)})", code=1)
    print(f"Claude Code sessions for {project} (newest first):\n")
    for r in rows:
        print(f"{r.id}  {r.size_mb:6.1f} MB  {r.age_h:7.1f} h ago  {r.lines:6d} lines  {r.first_message}")
    print("\nPut one in .env as CLAUDE_SESSION_ID=<id> and set PROJECT_DIR to the project path above.")


def cmd_doctor(s: Settings, env_files: list[Path]) -> None:
    ok, bad = "ok ", "!! "
    lines: list[tuple[str, str, str]] = []

    lines.append((ok if env_files else bad, ".env", ", ".join(map(str, env_files)) if env_files else "not found: copy .env.example to .env in this directory (or to ~/.roger/.env) and fill in your keys"))
    lines.append((ok if s.attendee_api_key else bad, "Attendee key", "set" if s.attendee_api_key else "missing (https://app.attendee.dev)"))
    lines.append((ok if s.elevenlabs_api_key else bad, "ElevenLabs key", "set" if s.elevenlabs_api_key else "missing (https://elevenlabs.io)"))
    fast_key = s.openai_api_key if s.fast_provider == "openai" else s.anthropic_api_key
    lines.append((ok if fast_key else bad, "Fast responder", f"{s.fast_provider} / {s.fast_model}" + ("" if fast_key else f"  ({s.fast_provider.upper()}_API_KEY missing)")))
    lines.append((ok, "Classifier", f"{s.fast_provider} / {s.classifier_model}"))
    lines.append((ok, "Voice", f"{s.voice_id} / {s.tts_model}; hearing with {s.stt_model}"))
    if s.public_url:
        lines.append((ok, "Public URL", s.public_url))
    else:
        cf = shutil.which("cloudflared")
        lines.append((ok if cf else bad, "Tunnel", f"cloudflared at {cf}" if cf else "cloudflared not found: brew install cloudflared, or set PUBLIC_URL"))
    orb_built = (STATIC_DIR / "orb" / "index.html").exists()
    lines.append((ok, "Orb page", ("ElevenLabs UI orb (built)" if orb_built else "fallback shader orb (run `make orb` for the ElevenLabs one)") if s.orb else "disabled (ORB=0)"))
    if s.deep_enabled:
        if s.claude_session_id:
            f = sessions_dir(s.project_dir) / f"{s.claude_session_id}.jsonl"
            lines.append((ok if f.exists() else bad, "Deep brain", f"Claude Code session {s.claude_session_id[:8]} in {s.project_dir}" + ("" if f.exists() else f"  (session file not found: {f})")))
        else:
            lines.append((ok, "Deep brain", f"fresh Claude Code session in {s.project_dir}"))
        lines.append((ok, "Deep auth", "claude.ai login (DEEP_AUTH=subscription)" if s.deep_auth == "subscription" else "ANTHROPIC_API_KEY (DEEP_AUTH=api)"))
    else:
        lines.append((ok, "Deep brain", "off (set CLAUDE_SESSION_ID or PROJECT_DIR to enable)"))
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


def apply_deep_flags(s: Settings, session: str | None, project: str | None) -> None:
    """--session / --project on `run` and `serve`: attach a Claude Code session from the command line."""
    if not session and not project:
        return
    project_dir = str(Path(project).expanduser().resolve()) if project else s.project_dir
    if session == "latest":
        rows = list_sessions(project_dir)
        if not rows:
            fail(f"no Claude Code sessions found for {project_dir} (looked in {sessions_dir(project_dir)}); pass --project <dir> or a session id")
        session = rows[0].id
        print(f"attaching the newest Claude Code session for {project_dir}: {session[:8]}  ({rows[0].age_h:.1f} h old: {rows[0].first_message[:60]})")
    elif session:
        f = sessions_dir(project_dir) / f"{session}.jsonl"
        if not f.exists():
            fail(f"session {session} not found for {project_dir} (looked for {f}); run `roger sessions <project-dir>` to list ids")
    s.attach_session(session, project)


# --------------------------------------------------------------------------- entry point


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="roger", description="Roger: a voice AI teammate that joins your Google Meet, Microsoft Teams and Zoom calls.")
    p.add_argument("--env", metavar="FILE", help="path to a .env file (default: ./.env, then the repository root)")
    p.add_argument("--version", action="version", version=f"roger {__version__}")
    sub = p.add_subparsers(dest="command", required=True, metavar="command")

    def deep_flags(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--session", metavar="ID", help="Claude Code session to attach as the deep brain; 'latest' picks the newest one for the project (overrides CLAUDE_SESSION_ID)")
        parser.add_argument("--project", metavar="DIR", help="project folder of that session, or any repo for a fresh read-only session (overrides PROJECT_DIR)")

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
        apply_deep_flags(s, args.session, args.project)
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
