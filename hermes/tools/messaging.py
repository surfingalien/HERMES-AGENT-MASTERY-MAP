"""send_message (#50 delivery routing): proactive messages to any connected platform."""

from __future__ import annotations

from hermes.gateway.delivery import deliver, list_targets
from hermes.tools.registry import ToolContext, ToolError, tool


@tool(
    "send_message",
    "messaging",
    "Send a message to a messaging platform. target: 'telegram' (home channel), 'telegram:<chat_id>', "
    "'discord:<channel_id>', 'slack:<channel>', 'email:<address>', 'origin' (current chat) or 'list' to see targets.",
    {"type": "object", "properties": {"target": {"type": "string"}, "message": {"type": "string"}}, "required": ["target"]},
    parallel_safe=False,
)
def send_message(args: dict, ctx: ToolContext):
    if args["target"] == "list":
        return {"targets": list_targets(ctx.config)}
    if not args.get("message"):
        raise ToolError("message is required")
    origin = {"platform": ctx.platform, "chat_id": ctx.chat_id}
    return deliver(args["target"], args["message"], origin, ctx.config)
