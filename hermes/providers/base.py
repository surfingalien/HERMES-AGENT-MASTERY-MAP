"""Provider-neutral chat types.

The agent speaks an OpenAI-style message format internally:

    {"role": "system"|"user"|"assistant"|"tool", "content": ..., "tool_calls": [...], "tool_call_id": ...}

Each provider adapter converts to and from its own wire format. Assistant
messages may carry ``_raw`` (the provider's native content blocks, e.g.
Anthropic thinking blocks) plus ``_provider`` so they can be replayed
unchanged to the same provider on the next request.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    parse_error: str = ""

    def to_message_dict(self) -> dict:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": json.dumps(self.arguments, ensure_ascii=False)},
        }


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens


@dataclass
class ChatResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    provider: str = ""
    finish_reason: str = "stop"
    reasoning: str = ""
    raw_content: Any = None  # provider-native assistant content for faithful replay

    def to_message(self) -> dict:
        msg: dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            msg["tool_calls"] = [tc.to_message_dict() for tc in self.tool_calls]
        if self.raw_content is not None:
            msg["_raw"] = self.raw_content
            msg["_provider"] = self.provider
            msg["_model"] = self.model
        if self.reasoning:
            msg["_reasoning"] = self.reasoning
        return msg


class ProviderError(Exception):
    def __init__(self, message: str, status: int | None = None, retryable: bool = False, auth: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.auth = auth


class Provider:
    """Base class. Subclasses implement :meth:`chat`."""

    name = "base"

    def __init__(self, model: str = "", **options: Any):
        self.model = model
        self.options = options

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> ChatResponse:
        raise NotImplementedError

    def describe(self) -> str:
        return f"{self.name}:{self.model}"


def parse_arguments(raw: Any) -> tuple[dict, str]:
    """Parse tool-call arguments; never raw-string-match serialized JSON."""
    if isinstance(raw, dict):
        return raw, ""
    if raw in (None, ""):
        return {}, ""
    try:
        val = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        return {}, f"invalid JSON arguments: {exc}"
    if not isinstance(val, dict):
        return {}, "tool arguments must be a JSON object"
    return val, ""


def message_text(msg: dict) -> str:
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""
