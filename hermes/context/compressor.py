"""Context compression (keeps long sessions inside the model's window).

When the estimated prompt size passes ``compression.threshold`` x context
length, the middle of the conversation is summarised by the model and replaced
with a single summary message. The system prompt, the first exchange and the
last ``protect_last_n`` messages are kept verbatim; tool-call/result pairs are
never split.
"""

from __future__ import annotations

import json


def estimate_tokens(messages: list[dict]) -> int:
    total = 0
    for m in messages:
        c = m.get("content")
        total += len(c) if isinstance(c, str) else len(json.dumps(c, default=str)) if c else 0
        if m.get("tool_calls"):
            total += len(json.dumps(m["tool_calls"]))
    return total // 4 + 4 * len(messages)


def _safe_cut(messages: list[dict], idx: int) -> int:
    """Move idx forward so we never start the tail with an orphaned tool result."""
    while idx < len(messages) and messages[idx].get("role") == "tool":
        idx += 1
    return idx


def should_compress(messages: list[dict], cfg: dict) -> bool:
    ccfg = cfg.get("compression", {}) or {}
    if not ccfg.get("enabled", True):
        return False
    limit = int((cfg.get("model", {}) or {}).get("context_length", 200000))
    return estimate_tokens(messages) > limit * float(ccfg.get("threshold", 0.5))


def compress(messages: list[dict], provider, cfg: dict, focus: str = "") -> tuple[list[dict], dict]:
    """Return (new_messages, info). messages[0] must be the system prompt."""
    ccfg = cfg.get("compression", {}) or {}
    keep_tail = int(ccfg.get("protect_last_n", 20))
    if len(messages) <= keep_tail + 4:
        return messages, {"compressed": False, "reason": "too short"}
    head_end = 1
    for i in range(1, min(len(messages), 4)):
        if messages[i].get("role") == "assistant" and not messages[i].get("tool_calls"):
            head_end = i + 1
            break
    tail_start = _safe_cut(messages, max(head_end, len(messages) - keep_tail))
    middle = messages[head_end:tail_start]
    if not middle:
        return messages, {"compressed": False, "reason": "nothing to compress"}
    transcript = []
    for m in middle:
        role = m.get("role")
        content = m.get("content") if isinstance(m.get("content"), str) else json.dumps(m.get("content"), default=str)
        if m.get("tool_calls"):
            calls = ", ".join(f"{tc['function']['name']}({tc['function']['arguments'][:200]})" for tc in m["tool_calls"])
            content = f"{content or ''} [called: {calls}]"
        transcript.append(f"{role}: {(content or '')[:3000]}")
    prompt = ("Summarize this portion of an agent conversation so work can continue seamlessly. Keep: the user's goals "
              "and constraints, decisions made, files touched (paths), commands run and their outcomes, errors and fixes, "
              "open questions and the current state/next steps. Be dense and factual."
              + (f" Focus especially on: {focus}." if focus else "")
              + "\n\n" + "\n".join(transcript)[-120000:])
    model = ccfg.get("summary_model") or None
    resp = provider.chat([{"role": "user", "content": prompt}], tools=None, model=model, max_tokens=4000)
    summary = {"role": "user", "content": f"[Context summary of {len(middle)} earlier messages]\n{resp.content}"}
    ack = {"role": "assistant", "content": "Understood - continuing from that summary."}
    new = messages[:head_end] + [summary, ack] + messages[tail_start:]
    # History was edited: replay plain text/tool calls only, never provider-native
    # thinking blocks, so preserved-thinking checks cannot trip on the rewrite.
    new = [{k: v for k, v in m.items() if k not in ("_raw", "_provider")} for m in new]
    return new, {"compressed": True, "removed": len(middle), "before_tokens": estimate_tokens(messages),
                 "after_tokens": estimate_tokens(new), "usage": resp.usage}
