"""kanban tool - lets an orchestrator agent decompose work onto the shared board (#92)."""

from __future__ import annotations

from hermes.power.kanban import Board
from hermes.tools.registry import ToolContext, ToolError, tool


@tool(
    "kanban",
    "kanban",
    "Multi-agent kanban board. Orchestrators add cards (optionally assigned to a specialist profile and depending on "
    "other cards); workers pick them up. actions: add, list, move, comment, get.",
    {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["add", "list", "move", "comment", "get"]},
            "title": {"type": "string"},
            "body": {"type": "string"},
            "assignee": {"type": "string", "description": "profile name of the specialist worker ('' = anyone)"},
            "depends_on": {"type": "array", "items": {"type": "integer"}},
            "card_id": {"type": "integer"},
            "status": {"type": "string", "enum": ["todo", "doing", "blocked", "review", "done"]},
            "text": {"type": "string"},
            "board": {"type": "string", "default": "default"},
        },
        "required": ["action"],
    },
    parallel_safe=False,
)
def kanban(args: dict, ctx: ToolContext):
    board = Board(board=args.get("board") or "default")
    action = args["action"]
    if action == "add":
        if not args.get("title"):
            raise ToolError("title required")
        return board.add(args["title"], args.get("body") or "", args.get("assignee") or "", depends_on=args.get("depends_on"),
                         created_by=ctx.session_id)
    if action == "list":
        return {"cards": [{k: c[k] for k in ("id", "title", "status", "assignee", "depends_on")} | {"result": c["result"][:300]}
                          for c in board.list(args.get("status"))]}
    if not args.get("card_id"):
        raise ToolError("card_id required")
    if action == "get":
        return board.get(int(args["card_id"]))
    if action == "move":
        return board.move(int(args["card_id"]), args.get("status") or "todo", args.get("text"))
    if action == "comment":
        return board.comment(int(args["card_id"]), args.get("text") or "")
    raise ToolError(f"unknown action {action}")
