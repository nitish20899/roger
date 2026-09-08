"""The fast responder: a small, streaming model that answers on the voice path within about a second.

Two interchangeable implementations (OpenAI chat completions, Anthropic messages). Both stream text,
hand completed sentences to ``on_sentence`` as they form, and report tool calls through ``on_tool``.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Awaitable, Callable, Protocol

from .config import Settings
from .prompts import FAST_TOOLS, fast_user
from .text import take_sentences

log = logging.getLogger("roger.fast")

SentenceCallback = Callable[[str], Awaitable[None]]
ToolCallback = Callable[[str, dict], Awaitable[None]]


class FastResponder(Protocol):
    async def respond(self, system: str, transcript_window: str, speaker: str, question: str, on_sentence: SentenceCallback, on_tool: ToolCallback) -> None: ...


class AnthropicFastResponder:
    def __init__(self, api_key: str | None, model: str) -> None:
        import anthropic

        self.client = anthropic.AsyncAnthropic(api_key=api_key) if api_key else anthropic.AsyncAnthropic()
        self.model = model

    async def respond(self, system, transcript_window, speaker, question, on_sentence, on_tool) -> None:
        t0 = time.time()
        first = None
        buf = ""
        async with self.client.messages.stream(
            model=self.model,
            max_tokens=300,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": fast_user(transcript_window, speaker, question)}],
            tools=FAST_TOOLS,
        ) as stream:
            async for text in stream.text_stream:
                if first is None:
                    first = time.time()
                    log.info("first token in %.0f ms", (first - t0) * 1000)
                buf += text
                sentences, buf = take_sentences(buf)
                for s in sentences:
                    await on_sentence(s)
            final = await stream.get_final_message()
        if buf.strip():
            await on_sentence(buf.strip())
        for block in final.content:
            if block.type == "tool_use":
                await on_tool(block.name, dict(block.input))
        log.info("done in %.0f ms (stop=%s)", (time.time() - t0) * 1000, final.stop_reason)


class OpenAIFastResponder:
    def __init__(self, api_key: str | None, model: str) -> None:
        from openai import AsyncOpenAI

        self.client = AsyncOpenAI(api_key=api_key)
        self.model = model
        self.tools = [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}} for t in FAST_TOOLS]

    async def respond(self, system, transcript_window, speaker, question, on_sentence, on_tool) -> None:
        t0 = time.time()
        first = None
        buf = ""
        calls: dict[int, dict[str, str]] = {}
        extra: dict[str, Any] = {"reasoning_effort": "minimal"} if self.model.startswith(("gpt-5", "o")) else {}
        stream = await self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": fast_user(transcript_window, speaker, question)}],
            tools=self.tools,
            stream=True,
            max_completion_tokens=300,
            **extra,
        )
        finish = None
        async for chunk in stream:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            d = choice.delta
            if d and d.content:
                if first is None:
                    first = time.time()
                    log.info("first token in %.0f ms", (first - t0) * 1000)
                buf += d.content
                sentences, buf = take_sentences(buf)
                for s in sentences:
                    await on_sentence(s)
            if d and d.tool_calls:
                for tc in d.tool_calls:
                    c = calls.setdefault(tc.index or 0, {"name": "", "args": ""})
                    if tc.function:
                        if tc.function.name:
                            c["name"] = tc.function.name
                        if tc.function.arguments:
                            c["args"] += tc.function.arguments
            if choice.finish_reason:
                finish = choice.finish_reason
        if buf.strip():
            await on_sentence(buf.strip())
        for c in calls.values():
            try:
                args = json.loads(c["args"]) if c["args"].strip() else {}
            except json.JSONDecodeError:
                args = {}
            await on_tool(c["name"], args)
        log.info("done in %.0f ms (finish=%s)", (time.time() - t0) * 1000, finish)


def make_fast_responder(s: Settings) -> FastResponder:
    if s.fast_provider == "openai":
        return OpenAIFastResponder(s.openai_api_key, s.fast_model)
    return AnthropicFastResponder(s.anthropic_api_key, s.fast_model)
