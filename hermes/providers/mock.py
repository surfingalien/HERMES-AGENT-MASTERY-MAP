"""Offline deterministic provider for tests, demos and `hermes doctor`.

Default behaviour:
  * a user message containing ``call:<tool> {json}`` triggers that tool call
  * after tool results it answers ``Done. <last tool output>``
  * otherwise it echoes the message

Tests can pass ``script=[ChatResponse | callable(messages, tools) -> ChatResponse]``.
"""

from __future__ import annotations

import itertools
import json
import re
from typing import Any

from hermes.providers.base import ChatResponse, Provider, ToolCall, Usage, message_text

_CALL_RE = re.compile(r"call:([A-Za-z0-9_\-]+)\s*(\{.*\})?", re.S)
_ids = itertools.count(1)


class MockProvider(Provider):
    name = "mock"

    def __init__(self, model: str = "mock-1", script: list[Any] | None = None, **opts):
        super().__init__(model or "mock-1", **opts)
        self.script = list(script or [])
        self.calls: list[dict] = []

    def chat(self, messages, tools=None, model=None, max_tokens=None, **kwargs) -> ChatResponse:
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools, "model": model or self.model})
        usage = Usage(input_tokens=sum(len(str(m.get("content") or "")) for m in messages) // 4, output_tokens=10)
        if self.script:
            item: Any = self.script.pop(0)
            if callable(item):
                item = item(messages, tools)
            if isinstance(item, str):
                item = ChatResponse(content=item)
            item.usage = item.usage if (item.usage.input_tokens or item.usage.output_tokens) else usage
            item.provider = item.provider or "mock"
            item.model = item.model or (model or self.model)
            return item
        last = messages[-1] if messages else {"role": "user", "content": ""}
        if last.get("role") == "tool":
            return ChatResponse(content=f"Done. {str(last.get('content'))[:400]}", usage=usage, provider="mock", model=self.model)
        text = message_text(last)
        match = _CALL_RE.search(text)
        if match:
            args = json.loads(match.group(2)) if match.group(2) else {}
            return ChatResponse(
                content="",
                tool_calls=[ToolCall(f"mock_{next(_ids)}", match.group(1), args)],
                usage=usage,
                provider="mock",
                model=self.model,
                finish_reason="tool_calls",
            )
        if "Summarize" in text and "conversation" in text:
            return ChatResponse(content="Summary: earlier turns discussed the task.", usage=usage, provider="mock", model=self.model)
        return ChatResponse(content=f"Echo: {text}", usage=usage, provider="mock", model=self.model)


def scripted(*steps: Any) -> MockProvider:
    return MockProvider(script=list(steps))


def tool_step(name: str, args: dict | None = None, content: str = "") -> ChatResponse:
    return ChatResponse(content=content, tool_calls=[ToolCall(f"mock_{next(_ids)}", name, args or {})], finish_reason="tool_calls")

