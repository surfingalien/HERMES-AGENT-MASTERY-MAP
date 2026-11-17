"""memory tool - lets the agent curate MEMORY.md / USER.md itself."""

from __future__ import annotations

from hermes.memory.store import MemoryStore
from hermes.tools.registry import ToolContext, ToolError, tool


@tool(
    "memory",
    "memory",
    "Save durable knowledge across sessions. target='memory' for your own notes (environment facts, conventions, "
    "lessons learned); target='user' for the user's preferences and context. Keep entries short and factual; "
    "consolidate when full. Never store secrets. Changes appear in the system prompt from the next session.",
    {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["add", "replace", "remove", "read"]},
            "target": {"type": "string", "enum": ["memory", "user"], "default": "memory"},
            "content": {"type": "string", "description": "New entry text (add/replace)"},
            "old_text": {"type": "string", "description": "Unique substring identifying the entry (replace/remove)"},
        },
        "required": ["action"],
    },
    parallel_safe=False,
)
def memory(args: dict, ctx: ToolContext):
    mcfg = ctx.config.get("memory", {}) or {}
    target = args.get("target") or "memory"
    if target == "memory" and not mcfg.get("memory_enabled", True):
        raise ToolError("agent memory is disabled (memory.memory_enabled)")
    if target == "user" and not mcfg.get("user_profile_enabled", True):
        raise ToolError("user profile is disabled (memory.user_profile_enabled)")
    store = MemoryStore(ctx.config)
    action = args["action"]
    if action == "read":
        f = store.memory if target == "memory" else store.user
        return {"target": target, "entries": f.entries(), "usage": store.usage(target)}
    if ctx.platform in (mcfg.get("read_only_platforms") or []) or ctx.extra.get("memory_read_only"):
        raise ToolError(f"memory is read-only on platform '{ctx.platform}' (privacy boundary)")
    if action == "add":
        return store.add(target, args.get("content") or "")
    if action == "replace":
        return store.replace(target, args.get("old_text") or "", args.get("content") or "")
    if action == "remove":
        return store.remove(target, args.get("old_text") or "")
    raise ToolError(f"unknown action {action}")
