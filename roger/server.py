"""HTTP and WebSocket server: the Attendee bot connects here, the orb and monitor pages are served from here."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import signal
from pathlib import Path

from aiohttp import WSMsgType, web

from .audio import tone
from .bridge import Bridge
from .config import STATIC_DIR, Settings

log = logging.getLogger("roger.server")

ORB_DIR = STATIC_DIR / "orb"  # built ElevenLabs UI orb (see orb/ at the repo root); falls back to the shader page


def orb_page(s: Settings) -> tuple[str, str]:
    """``(html, version)``. The version changes whenever the page or its settings change, and connected pages reload."""
    built = ORB_DIR / "index.html"
    if built.exists():
        html = built.read_text()
        assets = "".join(sorted(p.name for p in (ORB_DIR / "assets").glob("*")))
        digest = hashlib.md5((html + assets + s.orb_colors + s.orb_bg + str(s.orb_size)).encode()).hexdigest()[:10]
        inject = f'<script>window.ORB_VERSION="{digest}";window.ORB_COLORS="{s.orb_colors}";window.ORB_BG="{s.orb_bg}";window.ORB_SIZE="{s.orb_size}";</script>'
        return html.replace("</head>", inject + "</head>", 1), digest
    html = (STATIC_DIR / "orb-fallback.html").read_text()
    digest = hashlib.md5(html.encode()).hexdigest()[:10]
    return html.replace("__BOT_FIRST_NAME__", s.bot_first_name).replace("__BOT_NAME__", s.bot_name).replace("__ORB_VERSION__", digest), digest


def orb_kind() -> str:
    return "elevenlabs-ui" if (ORB_DIR / "index.html").exists() else "shader"


def build_app(bridge: Bridge, s: Settings) -> web.Application:
    orb_html, orb_version = orb_page(s)
    monitor_html = (STATIC_DIR / "monitor.html").read_text().replace("__BOT_NAME__", s.bot_name)

    async def h_orb(_: web.Request) -> web.Response:
        return web.Response(text=orb_html, content_type="text/html", headers={"Cache-Control": "no-store"})

    async def h_monitor(_: web.Request) -> web.Response:
        return web.Response(text=monitor_html, content_type="text/html")

    async def h_ws_monitor(request: web.Request) -> web.WebSocketResponse:
        """Orb and monitor pages: they receive audio and state, and may send the meeting audio they capture."""
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        bridge.speaker.monitors.add(ws)
        await ws.send_str(json.dumps({"type": "state", "state": "listening"}))
        await ws.send_str(json.dumps({"type": "orb_version", "v": orb_version}))
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
                if m.get("type") == "mic" and m.get("pcm"):
                    await bridge.on_page_mic(base64.b64decode(m["pcm"]))
                elif m.get("type") == "hello":
                    log.info("orb: page connected (%s, webgl=%s, version=%s, ua=%s)", m.get("orb", "shader"), m.get("webgl"), m.get("version"), str(m.get("ua", ""))[:60])
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
                    if frames == 1:
                        log.info("attendee: receiving mixed audio (sample_rate=%s)", data.get("sample_rate"))
                    await bridge.on_audio(base64.b64decode(chunk))
                elif trig == "realtime_audio.per_participant" and chunk:
                    frames += 1
                    if frames == 1:
                        log.info("attendee: receiving per-participant audio (sample_rate=%s)", data.get("sample_rate"))
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
        bridge.speaker.say(str(body.get("text", "")))
        return web.json_response({"ok": True})

    async def h_ask(request: web.Request) -> web.Response:
        """Simulate a heard utterance without a meeting: {"speaker": "Alice", "text": "Roger, ..."}"""
        body = await request.json()
        await bridge.on_committed(str(body.get("text", "")), str(body.get("speaker", "Tester")))
        return web.json_response({"ok": True})

    async def h_tone(_: web.Request) -> web.Response:
        """One second of a 440 Hz tone: verifies the audio path without any API keys."""
        bridge.speaker.play_pcm("tone", tone(1.0))
        return web.json_response({"ok": True})

    async def h_decisions(_: web.Request) -> web.Response:
        return web.json_response(bridge.attention.decisions[-50:])

    async def h_health(_: web.Request) -> web.Response:
        h = bridge.health()
        h["orb"] = orb_kind() if s.orb else "off"
        return web.json_response(h)

    async def h_index(_: web.Request) -> web.Response:
        raise web.HTTPFound("/monitor")

    app = web.Application()
    routes = [
        web.get("/", h_index),
        web.get("/monitor", h_monitor),
        web.get("/orb", h_orb),
        web.get("/ws/monitor", h_ws_monitor),
        web.get("/ws/attendee", h_ws_attendee),
        web.post("/join", h_join),
        web.post("/leave", h_leave),
        web.post("/say", h_say),
        web.post("/ask", h_ask),
        web.get("/test/tone", h_tone),
        web.get("/health", h_health),
        web.get("/decisions", h_decisions),
    ]
    if ORB_DIR.exists():
        routes.append(web.static("/orb/", str(ORB_DIR)))
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
        log.info("ready: local http://localhost:%d/monitor  public %s  orb=%s", s.port, s.public_url or "(none)", orb_kind() if s.orb else "off")
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
