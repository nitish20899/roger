#!/usr/bin/env python
"""End-to-end test without a meeting: pretend to be the Attendee bot.

1. Render a spoken question with ElevenLabs (this is the "meeting audio").
2. Stream it into the running server's /ws/attendee in 100 ms frames, like the bot does, then silence.
3. Collect the audio frames the server sends back (the bot's reply) and report the timings.

Start the server first with AUDIO_OUT=ws so the reply comes back over the WebSocket:
    AUDIO_OUT=ws roger serve
    python scripts/loop_test.py "Hey Roger, in one sentence, what can you do?"
Add --echo to also feed the bot's own voice back in, which exercises the echo filter.
"""
import asyncio
import base64
import json
import sys
import time
from pathlib import Path

import httpx
import websockets

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from roger.config import Settings, load_env  # noqa: E402

load_env()
S = Settings.from_env()
ASKER_VOICE = "EXAVITQu4vr4xnSDxMaL"  # "Sarah", a different stock voice for the person asking
ECHO = "--echo" in sys.argv
args = [a for a in sys.argv[1:] if not a.startswith("--")]
QUESTION = args[0] if args else f"Hey {S.bot_first_name}, can you tell me in one sentence what you can do?"
SR = 16000
FRAME = 3200  # 100 ms


async def render(text: str) -> bytes:
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{ASKER_VOICE}/stream",
            params={"output_format": f"pcm_{SR}"},
            headers={"xi-api-key": S.elevenlabs_api_key},
            json={"text": text, "model_id": "eleven_flash_v2_5"},
        )
        r.raise_for_status()
        return r.content


def frame(chunk_b64: str) -> str:
    return json.dumps({"trigger": "realtime_audio.mixed", "data": {"chunk": chunk_b64, "sample_rate": SR, "timestamp_ms": int(time.time() * 1000)}})


async def main() -> None:
    pcm = await render(QUESTION)
    print(f"question audio: {len(pcm) / (SR * 2):.1f}s  '{QUESTION}'")
    async with websockets.connect(f"ws://localhost:{S.port}/ws/attendee", max_size=16 * 1024 * 1024) as ws:
        got = {"frames": 0, "bytes": 0, "first": None}

        async def receiver() -> None:
            async for raw in ws:
                m = json.loads(raw)
                if m.get("trigger") == "realtime_audio.bot_output":
                    if got["first"] is None:
                        got["first"] = time.time()
                    got["frames"] += 1
                    got["bytes"] += len(base64.b64decode(m["data"]["chunk"]))
                    if ECHO:  # the meeting feeds the bot's own voice back into its microphone
                        asyncio.get_event_loop().call_later(0.4, lambda c=m["data"]["chunk"]: asyncio.ensure_future(ws.send(frame(c))))

        recv = asyncio.create_task(receiver())
        await asyncio.sleep(12 if ECHO else 9)  # the bot greets when the connection opens; let that pass
        got.update(frames=0, bytes=0, first=None)

        for i in range(0, len(pcm), FRAME):  # speech, paced in real time
            await ws.send(frame(base64.b64encode(pcm[i : i + FRAME]).decode()))
            await asyncio.sleep(0.1)
        t_end_of_speech = time.time()
        silence = base64.b64encode(b"\x00" * FRAME).decode()
        for _ in range(120):  # silence so the transcriber commits and the bot has time to answer
            await ws.send(frame(silence))
            await asyncio.sleep(0.1)
            if got["first"] and time.time() - got["first"] > 6:
                break
        recv.cancel()
        if got["first"]:
            print(f"reply: first audio {((got['first'] - t_end_of_speech) * 1000):.0f} ms after end of speech; {got['bytes'] / (SR * 2):.1f}s of audio in {got['frames']} frames")
        else:
            print("no reply audio received (is the server running with AUDIO_OUT=ws? check ~/.roger/roger.log)")


if __name__ == "__main__":
    asyncio.run(main())
