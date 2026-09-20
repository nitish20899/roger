"""The local server, and the public URL that reaches it.

The Attendee bot connects here over a WebSocket: meeting audio in, the bot's voice out. A small monitor
page at ``/monitor`` plays that voice locally, which is how you hear the bot without joining a call.

Attendee has to reach this machine, so unless ``PUBLIC_URL`` is set a Cloudflare quick tunnel is started
in front of it. That needs ``cloudflared`` on PATH and no Cloudflare account.
"""
from __future__ import annotations

import asyncio
import atexit
import base64
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
    """The URL Attendee will dial back on: yours if set, otherwise a quick tunnel."""
    if s.public_url:
        return s.public_url
    if not shutil.which("cloudflared"):
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

    async def h_ws_attendee(request: web.Request) -> web.WebSocketResponse:
        """The Attendee bot connects here: meeting audio in, bot voice out."""
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=16 * 1024 * 1024)
        await ws.prepare(request)
        bridge.speaker.bot_ws = ws
        log.info("attendee: audio websocket connected")
        asyncio.create_task(bridge.greet())
        frames = 0
        seen: set[str] = set()  # one line per stream: they start at different times

        def first(trigger: str, rate) -> None:
            if trigger not in seen:
                seen.add(trigger)
                log.info("attendee: receiving %s audio (sample_rate=%s)", trigger, rate)
                if trigger == "mixed" and rate and int(rate) != s.sample_rate:
                    log.warning("attendee: mixed audio is %s Hz but the live session is %s Hz; set AUDIO_RATE=%s to match", rate, s.sample_rate, rate)

        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    evt = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                trig = evt.get("trigger", "")
                data = evt.get("data", {})
                chunk = data.get("chunk")
                if trig == "realtime_audio.mixed" and chunk:
                    frames += 1
                    first("mixed", data.get("sample_rate"))
                    await bridge.on_audio(base64.b64decode(chunk))
                elif trig == "realtime_audio.per_participant" and chunk:
                    frames += 1
                    first("per-participant", data.get("sample_rate"))
                    await bridge.on_participant_audio(str(data.get("participant_uuid")), base64.b64decode(chunk))
                else:
                    log.debug("attendee event %s", trig)
        finally:
            if bridge.speaker.bot_ws is ws:
                bridge.speaker.bot_ws = None
            log.info("attendee: audio websocket closed after %d frames", frames)
        return ws

    async def h_join(request: web.Request) -> web.Response:
        body = await request.json()
        url = str(body.get("meeting_url", "")).strip()
        if not url:
            return web.json_response({"error": "meeting_url required"}, status=400)
        try:
            bot_id = await bridge.join(url)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)
        return web.json_response({"bot_id": bot_id})

    async def h_leave(_: web.Request) -> web.Response:
        await bridge.attendee.leave()
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
        web.get("/ws/attendee", h_ws_attendee),
        web.post("/join", h_join),
        web.post("/leave", h_leave),
        web.post("/say", h_say),
        web.post("/ask", h_ask),
        web.get("/test/tone", h_tone),
        web.get("/health", h_health),
    ]
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
            bot_id = await bridge.join(meeting_url)
            log.info("bot %s is joining %s; if it lands in the waiting room, admit \"%s\". Press Ctrl-C to leave.", bot_id, meeting_url, s.bot_name)
        await stop.wait()
    finally:
        log.info("shutting down")
        if bridge.attendee.bot_id:
            if leave_on_exit:
                try:
                    await bridge.attendee.leave()
                except Exception as e:
                    log.warning("leave failed: %s", e)
            else:
                log.info("the bot stays in the meeting and reconnects if you restart; run `roger leave` to remove it")
        await bridge.close()
        await runner.cleanup()
