"""OpenAI-compatible Chat Completions provider.

Covers OpenRouter, Nous Portal, OpenAI, DeepSeek, Groq, xAI, Gemini's OpenAI
endpoint, Ollama, LM Studio, vLLM and any custom endpoint.
"""

from __future__ import annotations

import os
import time
from typing import Any

from hermes.providers import http
from hermes.providers.base import ChatResponse, Provider, ProviderError, ToolCall, Usage, parse_arguments

PRESETS: dict[str, dict[str, str]] = {
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "key_env": "OPENROUTER_API_KEY"},
    "nous": {"base_url": "https://inference-api.nousresearch.com/v1", "key_env": "NOUS_API_KEY"},
    "openai": {"base_url": "https://api.openai.com/v1", "key_env": "OPENAI_API_KEY"},
    "deepseek": {"base_url": "https://api.deepseek.com/v1", "key_env": "DEEPSEEK_API_KEY"},
    "groq": {"base_url": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY"},
    "xai": {"base_url": "https://api.x.ai/v1", "key_env": "XAI_API_KEY"},
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "key_env": "GEMINI_API_KEY"},
    "together": {"base_url": "https://api.together.xyz/v1", "key_env": "TOGETHER_API_KEY"},
    "ollama": {"base_url": "http://localhost:11434/v1", "key_env": ""},
    "lmstudio": {"base_url": "http://localhost:1234/v1", "key_env": ""},
    "custom": {"base_url": "", "key_env": "CUSTOM_API_KEY"},
}


class CredentialPool:
    """Several API keys for one provider, rotated on rate-limit/auth errors (#95)."""

    def __init__(self, env_names: list[str], strategy: str = "round_robin"):
        self.env_names = [n for n in env_names if n]
        self.strategy = strategy
        self.index = 0
        self.exhausted: dict[str, float] = {}

    def keys(self) -> list[str]:
        return [os.environ[n] for n in self.env_names if os.environ.get(n)]

    def current(self) -> str:
        keys = self.keys()
        if not keys:
            return ""
        now = time.time()
        for offset in range(len(keys)):
            key = keys[(self.index + offset) % len(keys)]
            if self.exhausted.get(key, 0) <= now:
                return key
        return keys[self.index % len(keys)]

    def mark_bad(self, key: str, cooldown: float = 60.0) -> bool:
        """Mark a key as cooling down. Returns True if another key is available."""
        self.exhausted[key] = time.time() + cooldown
        self.index += 1
        return any(self.exhausted.get(k, 0) <= time.time() for k in self.keys())

    def advance(self) -> None:
        if self.strategy == "round_robin":
            self.index += 1


class OpenAICompatProvider(Provider):
    def __init__(
        self,
        model: str,
        provider: str = "openrouter",
        base_url: str = "",
        api_key_env: str = "",
        pool: CredentialPool | None = None,
        routing: dict | None = None,
        reasoning_effort: str = "",
        timeout: float = 300,
        max_retries: int = 2,
    ):
        super().__init__(model)
        preset = PRESETS.get(provider, PRESETS["custom"])
        self.name = provider
        self.base_url = (base_url or preset["base_url"] or os.environ.get("OPENAI_BASE_URL", "")).rstrip("/")
        key_env = api_key_env or preset["key_env"]
        self.pool = pool or CredentialPool([key_env] + [f"{key_env}_{i}" for i in range(2, 6)] if key_env else [])
        self.routing = routing or {}
        self.reasoning_effort = reasoning_effort
        self.timeout = timeout
        self.max_retries = max_retries
        if not self.base_url:
            raise ProviderError(f"provider '{provider}' needs model.base_url in config.yaml")

    def _headers(self, key: str) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if key:
            h["Authorization"] = f"Bearer {key}"
        if self.name == "openrouter":
            h["HTTP-Referer"] = "https://github.com/hermes-agent-replica"
            h["X-Title"] = "Hermes Agent Replica"
        return h

    @staticmethod
    def _clean(messages: list[dict]) -> list[dict]:
        out = []
        for m in messages:
            clean = {k: v for k, v in m.items() if not k.startswith("_")}
            if clean.get("role") == "assistant" and clean.get("content") is None:
                clean["content"] = ""
            out.append(clean)
        return out

    def chat(self, messages, tools=None, model=None, max_tokens=None, **kwargs) -> ChatResponse:
        model = model or self.model
        payload: dict[str, Any] = {"model": model, "messages": self._clean(messages)}
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = [{"type": "function", "function": t} for t in tools]
            payload["tool_choice"] = "auto"
        if self.name == "openrouter":
            if self.routing:
                payload["provider"] = self.routing
            payload["usage"] = {"include": True}
            if self.reasoning_effort:
                payload["reasoning"] = {"effort": self.reasoning_effort}
        for key in ("temperature", "response_format", "stop"):
            if key in kwargs and kwargs[key] is not None:
                payload[key] = kwargs[key]

        attempt = 0
        while True:
            key = self.pool.current()
            if not key and self.pool.env_names and self.name not in ("ollama", "lmstudio", "custom"):
                raise ProviderError(
                    f"No API key for provider '{self.name}'. Set {self.pool.env_names[0]} (run `hermes setup`).",
                    auth=True,
                )
            try:
                data = http.post_json(f"{self.base_url}/chat/completions", payload, self._headers(key), timeout=self.timeout)
                self.pool.advance()
                break
            except http.HTTPError as exc:
                retryable = exc.status in (408, 409, 429) or exc.status >= 500
                if exc.status in (401, 402, 403, 429) and key and self.pool.mark_bad(key):
                    continue  # rotate to the next pooled credential
                if retryable and attempt < self.max_retries:
                    attempt += 1
                    time.sleep(min(2 ** attempt, 10))
                    continue
                raise ProviderError(f"{self.name} error: {exc}", exc.status, retryable, exc.status in (401, 403)) from None
            except OSError as exc:  # network
                if attempt < self.max_retries:
                    attempt += 1
                    time.sleep(min(2 ** attempt, 10))
                    continue
                raise ProviderError(f"{self.name} connection error: {exc}", None, True) from None

        if "error" in data and not data.get("choices"):
            raise ProviderError(f"{self.name} error: {data['error']}", retryable=True)
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        calls = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            args, err = parse_arguments(fn.get("arguments"))
            calls.append(ToolCall(tc.get("id") or f"call_{i}", fn.get("name", ""), args, err))
        u = data.get("usage") or {}
        details = u.get("prompt_tokens_details") or {}
        usage = Usage(
            input_tokens=int(u.get("prompt_tokens") or 0),
            output_tokens=int(u.get("completion_tokens") or 0),
            cache_read_tokens=int(details.get("cached_tokens") or 0),
        )
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        return ChatResponse(
            content=content,
            tool_calls=calls,
            usage=usage,
            model=data.get("model") or model,
            provider=self.name,
            finish_reason=choice.get("finish_reason") or "stop",
            reasoning=msg.get("reasoning") or msg.get("reasoning_content") or "",
        )

    def list_models(self) -> list[str]:
        key = self.pool.current()
        data = http.get_json(f"{self.base_url}/models", self._headers(key))
        return sorted(m.get("id", "") for m in (data.get("data") or []))
