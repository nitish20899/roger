"""The deep brain: Claude Code, driven through the Claude Agent SDK.

Either resumes (and forks) one of your existing Claude Code sessions, so the bot knows everything that
session knows, or starts a fresh session in ``PROJECT_DIR`` with read-only access to the repository.
It answers through two in-process MCP tools, ``say`` and ``post_chat``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Optional

from .attendee import Attendee
from .config import Settings
from .prompts import BRIEFING_FROM_REPO, BRIEFING_FROM_SESSION, DEEP_FAILURE_LINE, deep_rules, delegated_prompt
from .speaker import Speaker
from .text import speech_clean, take_sentences

log = logging.getLogger("roger.deep")


class DeepAgent:
    def __init__(self, s: Settings, speaker: Speaker, attendee: Attendee) -> None:
        self.s = s
        self.speaker = speaker
        self.attendee = attendee
        self.client = None
        self.lock = asyncio.Lock()
        self.available = False

    async def start(self) -> None:
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, create_sdk_mcp_server, tool

        if self.s.deep_auth == "subscription":
            # The bundled Claude Code CLI inherits our environment. Without the key it uses your claude.ai login.
            os.environ.pop("ANTHROPIC_API_KEY", None)

        speaker, attendee = self.speaker, self.attendee

        @tool("say", "Speak one to three short sentences into the meeting (plain speech, no markdown).", {"text": str})
        async def say(args: dict[str, Any]) -> dict[str, Any]:
            speaker.say(args["text"])
            return {"content": [{"type": "text", "text": "spoken"}]}

        @tool("post_chat", "Post plain text to the meeting chat (under 450 characters).", {"text": str})
        async def post_chat(args: dict[str, Any]) -> dict[str, Any]:
            await attendee.chat(args["text"])
            return {"content": [{"type": "text", "text": "posted"}]}

        server = create_sdk_mcp_server(name="meeting", version="1.0.0", tools=[say, post_chat])
        resume: dict[str, Any] = {"resume": self.s.claude_session_id, "fork_session": True} if self.s.claude_session_id else {}
        options = ClaudeAgentOptions(
            model=self.s.deep_model,
            cwd=self.s.project_dir,
            mcp_servers={"meeting": server},
            allowed_tools=["mcp__meeting__say", "mcp__meeting__post_chat", "Read", "Grep", "Glob"],
            permission_mode="default",
            system_prompt={"type": "preset", "preset": "claude_code", "append": deep_rules(self.s)},
            **resume,
        )
        self.client = ClaudeSDKClient(options)
        await self.client.connect()
        self.available = True
        if self.s.claude_session_id:
            log.info("attached to Claude Code session %s (forked, model=%s)", self.s.claude_session_id, self.s.deep_model)
        else:
            log.info("fresh Claude Code session in %s (model=%s)", self.s.project_dir, self.s.deep_model)

    async def _run(self, prompt: str) -> tuple[str, bool]:
        """Send one prompt and collect the response. Returns ``(text, is_error)``."""
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

        texts: list[str] = []
        result = ""
        is_error = False
        async with self.lock:
            await self.client.query(prompt)
            async for msg in self.client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for b in msg.content:
                        if isinstance(b, TextBlock):
                            texts.append(b.text)
                elif isinstance(msg, ResultMessage):
                    result = msg.result or ""
                    is_error = bool(msg.is_error)
                    if is_error or msg.subtype != "success":
                        log.warning("result subtype=%s is_error=%s: %s", msg.subtype, msg.is_error, result[:300])
        return (result or "\n".join(texts)), is_error

    async def briefing(self) -> str:
        t0 = time.time()
        prompt = BRIEFING_FROM_SESSION if self.s.claude_session_id else BRIEFING_FROM_REPO
        text, is_error = await self._run(prompt)
        if is_error or len(text) < 500:
            raise RuntimeError(f"briefing failed: {text[:200]}")
        log.info("briefing ready (%d chars, %.1fs)", len(text), time.time() - t0)
        return text

    async def ask(self, asker: str, question: str, context: str) -> None:
        t0 = time.time()
        before = len(self.speaker.spoken_log)
        try:
            result, is_error = await self._run(delegated_prompt(asker, question, context))
        except Exception as e:
            log.error("failed: %s", e)
            result, is_error = "", True
        log.info("answered in %.1fs (error=%s)", time.time() - t0, is_error)
        if is_error or (not result and len(self.speaker.spoken_log) == before):
            self.speaker.say(DEEP_FAILURE_LINE)
            return
        if result and len(self.speaker.spoken_log) == before:
            # It wrote text instead of calling say(): speak a trimmed version so the room is not left hanging.
            sentences, rest = take_sentences(speech_clean(result))
            fallback = " ".join((sentences or [rest])[:2])
            if fallback:
                self.speaker.say(fallback[:400])

    async def close(self) -> None:
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:
                pass
