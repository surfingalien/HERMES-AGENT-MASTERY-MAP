"""todo (planning) and clarify (ask the user) tools - the "Plan & Delegate" step."""

from __future__ import annotations

from hermes.tools.registry import ToolContext, ToolError, tool


@tool(
    "todo",
    "todo",
    "Maintain a task list for multi-step work. Write the full list each time (ids, content, status). "
    "Statuses: pending, in_progress, completed, cancelled. Keep exactly one item in_progress.",
    {
        "type": "object",
        "properties": {
            "todos": {"type": "array", "items": {"type": "object", "properties": {
                "id": {"type": "string"}, "content": {"type": "string"},
                "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "cancelled"]}},
                "required": ["id", "content", "status"]}},
            "merge": {"type": "boolean", "default": False, "description": "Update matching ids instead of replacing"},
        },
    },
)
def todo(args: dict, ctx: ToolContext):
    agent = ctx.agent
    store = agent.todos if agent is not None else ctx.extra.setdefault("todos", [])
    items = args.get("todos")
    if items is not None:
        if args.get("merge"):
            by_id = {t["id"]: t for t in store}
            for item in items:
                by_id.setdefault(item["id"], {}).update(item)
            store[:] = list(by_id.values())
        else:
            store[:] = [dict(i) for i in items]
    done = sum(t.get("status") == "completed" for t in store)
    return {"todos": store, "progress": f"{done}/{len(store)} completed"}


@tool(
    "clarify",
    "clarify",
    "Ask the user a clarifying question when the request is ambiguous and a wrong guess would be costly. "
    "Optionally offer choices.",
    {"type": "object", "properties": {"question": {"type": "string"}, "choices": {"type": "array", "items": {"type": "string"}}},
     "required": ["question"]},
    parallel_safe=False,
)
def clarify(args: dict, ctx: ToolContext):
    if ctx.clarify is None:
        raise ToolError("no interactive user available; make a reasonable assumption and state it")
    answer = ctx.clarify(args["question"], args.get("choices"))
    return {"question": args["question"], "answer": answer}
