"""Provider factory: primary model + fallback chain + credential pools + routing.

Map coverage: #5 choose model provider, #93 provider routing, #94 fallback
providers, #95 credential pools.
"""

from __future__ import annotations

import logging

from hermes.providers.base import ChatResponse, Provider, ProviderError, ToolCall, Usage  # noqa: F401

log = logging.getLogger("hermes.providers")

PROVIDER_CHOICES = [
    ("openrouter", "OpenRouter - 300+ models behind one key", "anthropic/claude-opus-5-5"),
    ("nous", "Nous Portal - Hermes models", "Hermes-4-405B"),
    ("anthropic", "Anthropic Claude (official SDK)", "claude-opus-5-5"),
    ("openai", "OpenAI", "gpt-5"),
    ("gemini", "Google Gemini (OpenAI-compatible endpoint)", "gemini-2.5-pro"),
    ("deepseek", "DeepSeek", "deepseek-chat"),
    ("groq", "Groq", "llama-3.3-70b-versatile"),
    ("xai", "xAI", "grok-4"),
    ("ollama", "Ollama (local)", "hermes3"),
    ("lmstudio", "LM Studio (local)", "local-model"),
    ("custom", "Any OpenAI-compatible endpoint", ""),
    ("mock", "Offline mock provider (testing)", "mock-1"),
]


def split_model(spec: str, default_provider: str) -> tuple[str, str]:
    """``"anthropic:claude-opus-5-5"`` -> (anthropic, claude-opus-5-5)."""
    if ":" in spec and not spec.startswith("http"):
        prov, _, model = spec.partition(":")
        if prov in {p for p, _, _ in PROVIDER_CHOICES}:
            return prov, model
    return default_provider, spec


def build_single(provider: str, model: str, cfg: dict, base_url: str = "", api_key_env: str = "") -> Provider:
    model_cfg = cfg.get("model", {}) if cfg else {}
    if provider == "mock":
        from hermes.providers.mock import MockProvider

        return MockProvider(model or "mock-1")
    if provider == "anthropic":
        from hermes.providers.anthropic_provider import AnthropicProvider

        return AnthropicProvider(model, api_key_env=api_key_env or "ANTHROPIC_API_KEY", effort=model_cfg.get("reasoning_effort", ""))
    from hermes.providers.openai_compat import CredentialPool, OpenAICompatProvider, PRESETS

    pools = (cfg or {}).get("credential_pools", {}) or {}
    pool = None
    if pools.get(provider):
        pool = CredentialPool(list(pools[provider]), (cfg or {}).get("credential_pool_strategy", "round_robin"))
    return OpenAICompatProvider(
        model,
        provider=provider if provider in PRESETS else "custom",
        base_url=base_url,
        api_key_env=api_key_env,
        pool=pool,
        routing=(cfg or {}).get("provider_routing") or {},
        reasoning_effort=model_cfg.get("reasoning_effort", ""),
    )


class FallbackProvider(Provider):
    """Tries the primary, then each fallback in order on retryable/auth failures."""

    name = "fallback"

    def __init__(self, chain: list[Provider]):
        super().__init__(chain[0].model if chain else "")
        self.chain = chain
        self.last_used: Provider | None = None

    def chat(self, messages, tools=None, model=None, max_tokens=None, **kwargs) -> ChatResponse:
        errors = []
        for i, prov in enumerate(self.chain):
            try:
                resp = prov.chat(messages, tools=tools, model=model if i == 0 else None, max_tokens=max_tokens, **kwargs)
                if i > 0:
                    log.warning("primary failed; served by fallback %s", prov.describe())
                self.last_used = prov
                return resp
            except ProviderError as exc:
                errors.append(f"{prov.describe()}: {exc}")
                continue
        raise ProviderError("All providers failed:\n  " + "\n  ".join(errors), retryable=False)

    def describe(self) -> str:
        return " -> ".join(p.describe() for p in self.chain)


def create_provider(cfg: dict, model_override: str | None = None, provider_override: str | None = None) -> Provider:
    model_cfg = cfg.get("model", {})
    provider = provider_override or model_cfg.get("provider", "openrouter")
    model = model_override or model_cfg.get("default", "")
    if model_override:
        provider, model = split_model(model_override, provider)
    primary = build_single(provider, model, cfg, model_cfg.get("base_url", ""), model_cfg.get("api_key_env", ""))
    chain = [primary]
    for fb in cfg.get("fallback_providers") or []:
        try:
            chain.append(build_single(fb.get("provider", provider), fb.get("model", ""), cfg, fb.get("base_url", ""), fb.get("api_key_env", "")))
        except ProviderError as exc:
            log.warning("skipping fallback %s: %s", fb, exc)
    return primary if len(chain) == 1 else FallbackProvider(chain)


def choose_model_for_turn(cfg: dict, user_text: str, default_model: str) -> str:
    """Smart model routing: short, tool-free chit-chat goes to a cheap model."""
    smr = cfg.get("smart_model_routing") or {}
    if not smr.get("enabled") or not smr.get("cheap_model"):
        return default_model
    simple = len(user_text) <= int(smr.get("max_simple_chars", 160)) and not any(
        w in user_text.lower() for w in ("code", "file", "search", "http", "run", "debug", "build", "analy", "```")
    )
    return smr["cheap_model"] if simple else default_model
