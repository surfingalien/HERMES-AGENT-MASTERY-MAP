"""Insights & cost tracking (#100). Prices are USD per million tokens and are
*estimates* - override or extend them with ``pricing:`` in config.yaml::

    pricing:
      my-model: {input: 1.0, output: 3.0}
"""

from __future__ import annotations

PRICES: dict[str, tuple[float, float, float]] = {
    # substring: (input, output, cache_read)
    "claude-fable-5-2": (8.0, 40.0, 0.20),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-opus-4": (5.0, 25.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.10),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-sonnet-4": (3.0, 15.0, 0.30),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-haiku-4-5": (1.0, 5.0, 0.10),
    "gpt-5-mini": (0.25, 2.0, 0.025),
    "gpt-5": (1.25, 10.0, 0.125),
    "gpt-4o-mini": (0.15, 0.60, 0.075),
    "gpt-4o": (2.5, 10.0, 1.25),
    "gemini-2.5-pro": (1.25, 10.0, 0.31),
    "gemini-2.5-flash": (0.30, 2.5, 0.075),
    "deepseek": (0.27, 1.10, 0.07),
    "hermes-4-405b": (1.0, 3.0, 0.0),
    "hermes-4-70b": (0.13, 0.40, 0.0),
    "llama": (0.20, 0.60, 0.0),
    "mock": (0.0, 0.0, 0.0),
}


def price_for(model: str, overrides: dict | None = None) -> tuple[float, float, float]:
    m = (model or "").lower()
    for key, val in (overrides or {}).items():
        if key.lower() in m:
            return float(val.get("input", 0)), float(val.get("output", 0)), float(val.get("cache_read", 0))
    for key in sorted(PRICES, key=len, reverse=True):
        if key in m:
            return PRICES[key]
    return 0.0, 0.0, 0.0


def estimate_cost(model: str, input_tokens: int, output_tokens: int, cache_read: int = 0, overrides: dict | None = None) -> float:
    pin, pout, pcache = price_for(model, overrides)
    uncached = max(0, input_tokens - cache_read)
    return (uncached * pin + output_tokens * pout + cache_read * pcache) / 1_000_000


def format_report(data: dict) -> str:
    t = data["totals"]
    lines = [
        f"Insights - last {data['days']} days",
        f"  sessions: {t['sessions']}   messages: {t['messages']}   tool calls: {t['tool_calls']}",
        f"  tokens: {t['input_tokens']:,} in / {t['output_tokens']:,} out ({t['cache_read_tokens']:,} cached)",
        f"  estimated cost: ${t['cost_usd']:.4f}",
        "",
        "  By model:",
    ]
    for r in data["by_model"]:
        lines.append(f"    {r['model'] or '?':40} {r['sessions']:>4} sessions  ${(r['cost_usd'] or 0):.4f}")
    lines.append("  By platform:")
    for r in data["by_source"]:
        lines.append(f"    {r['source'] or '?':15} {r['sessions']:>4} sessions  ${(r['cost_usd'] or 0):.4f}")
    if data["tools"]:
        lines.append("  Top tools:")
        for r in data["tools"]:
            lines.append(f"    {r['tool_name']:24} {r['calls']:>5} calls  {r['ok']}/{r['calls']} ok  ~{r['avg_ms']}ms")
    if data["top_sessions"]:
        lines.append("  Most expensive sessions:")
        for r in data["top_sessions"]:
            lines.append(f"    {r['id']}  {(r['title'] or '')[:40]:40} ${(r['cost_usd'] or 0):.4f}")
    return "\n".join(lines)
