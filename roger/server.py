"""The local server: the control API, the monitor page, and the meeting's audio socket.

Whichever participant is in use connects here over a WebSocket -- meeting audio in, the bot's voice out --
on a route the provider itself names. For the default browser participant that is a page on this machine
talking to ``127.0.0.1``, so nothing has to be reachable from the internet.

A hosted participant is the exception: it dials in from outside, so ``PUBLIC_URL`` is needed, or a
Cloudflare quick tunnel is started in front of the server (``cloudflared`` on PATH, no account).

A small monitor page at ``/monitor`` plays the bot's voice locally, which is how you hear it without
joining a call.
"""
from __future__ import annotations

import asyncio
import atexit
import json
import logging
import re
import shutil
import signal
import subprocess
import time

from aiohttp import WSMsgType, web

from .audio import tone
from .bridge import Bridge
from .config import STATIC_DIR, Settings
from .meeting import UnsupportedMeeting

log = logging.getLogger("roger.server")

TUNNEL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def start_tunnel(port: int, timeout: float = 25.0) -> tuple[str, subprocess.Popen]:
    """A free public HTTPS URL in front of ``localhost:port``. Returns ``(url, process)``."""
    proc = subprocess.Popen(
        ["cloudflared", "tunnel", "--url", f"http://localhost:{port}", "--no-autoupdate"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stderr.readline()
        if not line:
            if proc.poll() is not None:
                break
            continue
        found = TUNNEL_RE.search(line)
        if found:
            return found.group(0), proc
    proc.terminate()
    raise RuntimeError("cloudflared did not report a URL; run `cloudflared tunnel --url http://localhost:%d` by hand, or set PUBLIC_URL" % port)


def public_url(s: Settings) -> str | None:
    """A URL that reaches this machine from outside. Only a hosted participant needs one."""
    if s.public_url:
        return s.public_url
    if not s.hosted_participant or not shutil.which("cloudflared"):
        return None
    log.info("starting a Cloudflare quick tunnel ...")
    url, proc = start_tunnel(s.port)
    atexit.register(lambda: proc.terminate())
    log.info("tunnel: %s", url)
    return url


def build_app(bridge: Bridge, s: Settings) -> web.Application:
    monitor_html = (STATIC_DIR / "monitor.html").read_text().replace("__BOT_NAME__", s.bot_name)

    async def h_monitor(_: web.Request) -> web.Response:
        return web.Response(text=monitor_html, content_type="text/html")

    async def h_ws_monitor(request: web.Request) -> web.WebSocketResponse:
        """The monitor page: it receives the bot's audio and its listening/speaking state."""
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        bridge.speaker.monitors.add(ws)
        await ws.send_str(json.dumps({"type": "state", "state": "listening"}))
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                if msg.data == "ping":
                    await ws.send_str("pong")
                    continue
                try:
                    m = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if m.get("type") == "hello":
                    log.info("monitor page connected")
        finally:
            bridge.speaker.monitors.discard(ws)
        return ws

    async def h_ws_meeting(request: web.Request) -> web.WebSocketResponse:
        """The active participant's audio socket, on whichever route that provider asked for."""
        return await bridge.meeting.handle_ws(request)

    async def h_join(request: web.Request) -> web.Response:
        body = await request.json()
        url = str(body.get("meeting_url", "")).strip()
        if not url:
            return web.json_response({"error": "meeting_url required"}, status=400)
        try:
            bot_id = await bridge.join(url)
        except UnsupportedMeeting as e:
            return web.json_response({"error": str(e)}, status=400)
        except Exception as e:
            log.warning("join failed: %s", e)
            return web.json_response({"error": str(e)}, status=500)
        return web.json_response({"bot_id": bot_id})

    async def h_leave(_: web.Request) -> web.Response:
        await bridge.leave()
        return web.json_response({"ok": True})

    async def h_say(request: web.Request) -> web.Response:
        body = await request.json()
        text = str(body.get("text", "")).strip()
        if not text:
            return web.json_response({"error": "text required"}, status=400)
        await bridge.speak_exactly(text)
        return web.json_response({"ok": True})

    async def h_ask(request: web.Request) -> web.Response:
        """Simulate a heard utterance without a meeting: {"speaker": "Alice", "text": "Roger, ..."}"""
        body = await request.json()
        text = str(body.get("text", "")).strip()
        if not text:
            return web.json_response({"error": "text required"}, status=400)
        await bridge.inject_utterance(str(body.get("speaker", "Tester")), text)
        return web.json_response({"ok": True})

    async def h_tone(_: web.Request) -> web.Response:
        """One second of a 440 Hz tone: verifies the audio path without any API keys."""
        bridge.speaker.play_pcm("tone", tone(1.0, rate=s.sample_rate))
        return web.json_response({"ok": True})


    async def h_health(_: web.Request) -> web.Response:
        return web.json_response(bridge.health())

    async def h_index(_: web.Request) -> web.Response:
        raise web.HTTPFound("/monitor")

    app = web.Application()
    routes = [
        web.get("/", h_index),
        web.get("/monitor", h_monitor),
        web.get("/ws/monitor", h_ws_monitor),
        web.post("/join", h_join),
        web.post("/leave", h_leave),
        web.post("/say", h_say),
        web.post("/ask", h_ask),
        web.get("/test/tone", h_tone),
        web.get("/health", h_health),
    ]
    if bridge.meeting.ws_path:
        routes.append(web.get(bridge.meeting.ws_path, h_ws_meeting))
        log.info("meeting audio socket at %s (%s participant)", bridge.meeting.ws_path, bridge.meeting.name)
    app.add_routes(routes)
    return app


async def serve(s: Settings, meeting_url: str | None = None, leave_on_exit: bool = False) -> None:
    """Run the server until SIGINT/SIGTERM. Optionally send the bot into a meeting as soon as it is up."""
    bridge = Bridge(s)
    app = build_app(bridge, s)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", s.port)
    await site.start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass

    try:
        await bridge.start()
        log.info("ready: local http://localhost:%d/monitor  public %s", s.port, s.public_url or "(none)")
        log.info("config: %s", json.dumps(s.summary()))
        if meeting_url:
            await bridge.join(meeting_url)
            log.info("joining %s; if it lands in a waiting room, admit \"%s\". Press Ctrl-C to leave.", meeting_url, s.bot_name)
        await stop.wait()
    finally:
        log.info("shutting down")
        if bridge.meeting.state.live:
            if leave_on_exit or bridge.meeting.name == "browser":
                # A browser participant is this process. Nothing survives the exit, so it always leaves.
                try:
                    await bridge.leave()
                except Exception as e:
                    log.warning("leave failed: %s", e)
            else:
                log.info("the bot stays in the meeting and reconnects if you restart; run `roger leave` to remove it")
        await bridge.close()
        await runner.cleanup()
