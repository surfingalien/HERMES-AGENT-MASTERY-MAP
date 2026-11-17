"""Native Claude provider built on the official ``anthropic`` Python SDK.

Install with ``pip install 'hermes-agent-replica[anthropic]'``. Defaults to
``claude-opus-5-5``. Highlights:

* streaming with ``get_final_message()`` so large ``max_tokens`` never time out
* adaptive thinking (the model default) with ``output_config.effort``
* top-level automatic prompt caching (stable system prompt is cached)
* assistant content blocks (thinking included) are replayed unchanged
* all tool results for one turn go back in a single user message
* server-side refusal fallbacks (``fallbacks: "default"``) on models that support them
"""

from __future__ import annotations

import base64
import os
import re
from typing import Any

from hermes.providers.base import ChatResponse, Provider, ProviderError, ToolCall, Usage

DEFAULT_MODEL = "claude-opus-5-5"
# Models where the server-side refusal fallback ("default" form) is recommended.
_FALLBACK_MODELS = ("claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5")
_EFFORT_MODELS = re.compile(r"claude-(fable|mythos|opus|sonnet-5|sonnet-4-6|haiku-5)")
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def _data_url_to_source(url: str) -> dict:
    if url.startswith("data:"):
        header, _, data = url.partition(",")
        media_type = header[5:].split(";")[0] or "image/png"
        return {"type": "base64", "media_type": media_type, "data": data}
    return {"type": "url", "url": url}


def _convert_user_content(content: Any) -> Any:
    if isinstance(content, str):
        return content
    blocks = []
    for part in content or []:
        if part.get("type") == "text":
            blocks.append({"type": "text", "text": part.get("text", "")})
        elif part.get("type") == "image_url":
            url = (part.get("image_url") or {}).get("url", "")
            blocks.append({"type": "image", "source": _data_url_to_source(url)})
    return blocks


def to_anthropic(messages: list[dict], model: str) -> tuple[str, list[dict]]:
    """Convert internal OpenAI-style messages to (system, anthropic_messages)."""
    system_parts: list[str] = []
    out: list[dict] = []

    def append(role: str, blocks: list[dict]) -> None:
        if out and out[-1]["role"] == role:
            # Merge consecutive same-role turns (e.g. several tool results).
            prev = out[-1]["content"]
            if isinstance(prev, str):
                prev = [{"type": "text", "text": prev}]
            out[-1]["content"] = prev + blocks
        else:
            out.append({"role": role, "content": blocks})

    for m in messages:
        role = m.get("role")
        if role == "system":
            if not out:
                system_parts.append(m.get("content") or "")
            else:
                append("user", [{"type": "text", "text": f"[system note] {m.get('content') or ''}"}])
        elif role == "user":
            c = _convert_user_content(m.get("content"))
            append("user", [{"type": "text", "text": c}] if isinstance(c, str) else c)
        elif role == "assistant":
            raw = m.get("_raw")
            if raw is not None and m.get("_provider") == "anthropic":
                # Replay native blocks unchanged (thinking blocks must not be edited).
                append("assistant", list(raw))
                continue
            blocks: list[dict] = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for tc in m.get("tool_calls") or []:
                from hermes.providers.base import parse_arguments

                args, _ = parse_arguments(tc["function"].get("arguments"))
                blocks.append({"type": "tool_use", "id": tc["id"], "name": tc["function"]["name"], "input": args})
            if blocks:
                append("assistant", blocks)
        elif role == "tool":
            block = {"type": "tool_result", "tool_use_id": m.get("tool_call_id"), "content": m.get("content") or ""}
            if m.get("_is_error"):
                block["is_error"] = True
            append("user", [block])
    # Tool results must come first inside a user message that answers tool_use.
    for msg in out:
        if msg["role"] == "user" and isinstance(msg["content"], list):
            results = [b for b in msg["content"] if b.get("type") == "tool_result"]
            if results:
                others = [b for b in msg["content"] if b.get("type") != "tool_result"]
                msg["content"] = results + others
    return "\n\n".join(p for p in system_parts if p), out


def _block_to_dict(block: Any) -> dict:
    for attr in ("to_dict", "model_dump"):
        fn = getattr(block, attr, None)
        if fn:
            try:
                return fn(exclude_none=True) if attr == "model_dump" else fn()
            except TypeError:
                return fn()
    return dict(block)


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, model: str = DEFAULT_MODEL, api_key_env: str = "ANTHROPIC_API_KEY", effort: str = "", client: Any = None, **opts):
        super().__init__(model or DEFAULT_MODEL, **opts)
        self.effort = effort
        if client is not None:
            self.client = client
            self._sdk = None
            return
        try:
            import anthropic  # official SDK
        except ImportError as exc:
            raise ProviderError(
                "The anthropic provider needs the official SDK: pip install 'hermes-agent-replica[anthropic]'"
            ) from exc
        self._sdk = anthropic
        key = os.environ.get(api_key_env or "ANTHROPIC_API_KEY")
        # Zero-arg client resolves ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / `ant auth login` profiles.
        self.client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()

    def chat(self, messages, tools=None, model=None, max_tokens=None, **kwargs) -> ChatResponse:
        model = model or self.model
        system, conv = to_anthropic(messages, model)
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens or 64000,
            "messages": conv,
            "cache_control": {"type": "ephemeral"},  # automatic prompt caching
        }
        if system:
            params["system"] = [{"type": "text", "text": system}]
        if tools:
            params["tools"] = [
                {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "input_schema": t.get("parameters") or {"type": "object", "properties": {}},
                    "eager_input_streaming": True,
                }
                for t in tools
            ]
        effort = kwargs.get("effort") or self.effort
        if effort and _EFFORT_MODELS.search(model):
            params["output_config"] = {"effort": effort}
        use_fallback = model in _FALLBACK_MODELS
        try:
            if use_fallback:
                params["betas"] = [FALLBACK_BETA]
                params["fallbacks"] = "default"
                with self.client.beta.messages.stream(**params) as stream:
                    msg = stream.get_final_message()
            else:
                with self.client.messages.stream(**params) as stream:
                    msg = stream.get_final_message()
        except Exception as exc:  # map SDK errors to ProviderError
            raise self._map_error(exc) from None
        return self._parse(msg, model)

    def _map_error(self, exc: Exception) -> ProviderError:
        sdk = self._sdk
        status = getattr(exc, "status_code", None)
        if sdk is not None:
            if isinstance(exc, (sdk.AuthenticationError, sdk.PermissionDeniedError)):
                return ProviderError(f"anthropic auth error: {exc}", status, False, True)
            if isinstance(exc, (sdk.BadRequestError, sdk.NotFoundError)):
                return ProviderError(f"anthropic request error: {exc}", status, False)
            if isinstance(exc, sdk.RateLimitError):
                return ProviderError(f"anthropic rate limited: {exc}", status, True)
            if isinstance(exc, sdk.APIStatusError):
                return ProviderError(f"anthropic API error: {exc}", status, (status or 500) >= 500)
            if isinstance(exc, sdk.APIConnectionError):
                return ProviderError(f"anthropic connection error: {exc}", None, True)
        return ProviderError(f"anthropic error: {exc}", status, True)

    def _parse(self, msg: Any, model: str) -> ChatResponse:
        text_parts, calls, reasoning = [], [], []
        raw = []
        for block in msg.content:
            raw.append(_block_to_dict(block))
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(block.text)
            elif btype == "tool_use":
                inp = block.input if isinstance(block.input, dict) else {}
                err = "" if isinstance(block.input, dict) else "tool input was not a JSON object"
                calls.append(ToolCall(block.id, block.name, inp, err))
            elif btype == "thinking":
                if getattr(block, "thinking", ""):
                    reasoning.append(block.thinking)
        stop = getattr(msg, "stop_reason", "end_turn")
        content = "".join(text_parts)
        if stop == "refusal":
            details = getattr(msg, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            content = content or f"[The model declined this request{f' ({category})' if category else ''}.]"
            calls = []
        elif stop == "max_tokens" and calls:
            # A truncated tool call may hold partial input - don't run it.
            for c in calls:
                c.parse_error = "response hit max_tokens; tool input may be truncated"
        u = msg.usage
        usage = Usage(
            input_tokens=int(getattr(u, "input_tokens", 0) or 0),
            output_tokens=int(getattr(u, "output_tokens", 0) or 0),
            cache_read_tokens=int(getattr(u, "cache_read_input_tokens", 0) or 0),
            cache_write_tokens=int(getattr(u, "cache_creation_input_tokens", 0) or 0),
        )
        return ChatResponse(
            content=content,
            tool_calls=calls,
            usage=usage,
            model=getattr(msg, "model", model) or model,
            provider="anthropic",
            finish_reason={"end_turn": "stop", "tool_use": "tool_calls"}.get(stop, stop),
            reasoning="\n".join(reasoning),
            raw_content=raw,
        )


def image_to_data_url(path: str) -> str:
    ext = os.path.splitext(path)[1].lower().lstrip(".") or "png"
    media = {"jpg": "jpeg"}.get(ext, ext)
    with open(path, "rb") as fh:
        data = base64.standard_b64encode(fh.read()).decode("ascii")
    return f"data:image/{media};base64,{data}"
