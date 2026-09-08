"""Cloudflare quick tunnel: a public https URL for the local server, no account needed."""
from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time

URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def start_tunnel(port: int, timeout_s: float = 40.0) -> tuple[str, subprocess.Popen]:
    """Start ``cloudflared tunnel --url http://localhost:<port>`` and return ``(public_url, process)``."""
    if not shutil.which("cloudflared"):
        raise RuntimeError("cloudflared is not installed. Install it (macOS: brew install cloudflared; other: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/) or set PUBLIC_URL.")
    proc = subprocess.Popen(
        ["cloudflared", "tunnel", "--url", f"http://localhost:{port}", "--no-autoupdate"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    found: dict[str, str] = {}

    def reader() -> None:
        assert proc.stdout
        for line in proc.stdout:
            m = URL_RE.search(line)
            if m and "url" not in found:
                found["url"] = m.group(0)

    threading.Thread(target=reader, daemon=True).start()
    deadline = time.time() + timeout_s
    while time.time() < deadline and "url" not in found:
        if proc.poll() is not None:
            raise RuntimeError("cloudflared exited before reporting a URL")
        time.sleep(0.2)
    if "url" not in found:
        proc.terminate()
        raise RuntimeError(f"cloudflared did not report a URL within {timeout_s:.0f} s")
    return found["url"], proc


def stop_tunnel(proc: subprocess.Popen | None) -> None:
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
