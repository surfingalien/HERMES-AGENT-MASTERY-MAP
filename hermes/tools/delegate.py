"""delegate_task (#56 delegate_task, #57 isolated subagents, #58 parallel workstreams).

Each subagent gets a fresh conversation (no parent history), a restricted
toolset, its own terminal session and no memory writes. Several tasks run in
parallel (``delegation.max_concurrent``). Only the final summaries return to
the parent, keeping its context small.
"""

from __future__ import annotations

import concurrent.futures
import uuid

from hermes.tools.registry import ToolContext, ToolError, tool

BLOCKED_FOR_CHILDREN = ["delegation", "clarify", "messaging", "cronjob", "memory"]


def _run_child(ctx: ToolContext, goal: str, context: str, toolsets: list[str], max_iter: int) -> dict:
    from hermes.agent import AIAgent

    parent = ctx.agent
    child_toolsets = [t for t in toolsets if t not in BLOCKED_FOR_CHILDREN]
    child = AIAgent(
        config=ctx.config,
        provider=parent.provider if parent else None,
        platform="subagent",
        toolsets=child_toolsets,
        max_turns=max_iter,
        parent=parent,
        depth=ctx.depth + 1,
        skip_memory=True,
        quiet=True,
        cwd=ctx.cwd,
        task_id=f"sub-{uuid.uuid4().hex[:8]}",
    )
    prompt = (
        "You are a focused subagent working for another agent. Complete the goal below autonomously; you cannot ask "
        "questions. Finish with a concise report: what you did, key findings/results (with file paths, URLs, numbers), "
        f"and anything left unresolved.\n\nGOAL:\n{goal}"
        + (f"\n\nCONTEXT FROM PARENT:\n{context}" if context else "")
    )
    try:
        res = child.run_conversation(prompt)
        return {"goal": goal, "status": "completed" if not res.get("error") else "error", "summary": res.get("final_response", ""),
                "turns": res.get("turns", 0), "tool_calls": res.get("tool_calls", 0), "session_id": child.session_id}
    except Exception as exc:
        return {"goal": goal, "status": "error", "summary": f"{type(exc).__name__}: {exc}"}
    finally:
        child.close()


@tool(
    "delegate_task",
    "delegation",
    "Spawn isolated subagents for self-contained work (research, coding, analysis). Use `tasks` to run several in "
    "parallel. Subagents start with a blank context - put every needed detail in goal/context. Returns their summaries.",
    {
        "type": "object",
        "properties": {
            "goal": {"type": "string", "description": "Single task goal"},
            "context": {"type": "string", "description": "Background, constraints, file paths"},
            "toolsets": {"type": "array", "items": {"type": "string"}, "description": "e.g. ['web','file','terminal']"},
            "tasks": {"type": "array", "items": {"type": "object", "properties": {
                "goal": {"type": "string"}, "context": {"type": "string"},
                "toolsets": {"type": "array", "items": {"type": "string"}}}, "required": ["goal"]},
                "description": "Batch mode: up to 5 tasks run in parallel"},
            "max_iterations": {"type": "integer"},
        },
    },
    parallel_safe=False,
)
def delegate_task(args: dict, ctx: ToolContext):
    dcfg = ctx.config.get("delegation", {}) or {}
    if ctx.depth >= int(dcfg.get("max_depth", 2)):
        raise ToolError("maximum delegation depth reached; do the work directly")
    default_ts = args.get("toolsets") or dcfg.get("default_toolsets", ["terminal", "file", "web"])
    max_iter = int(args.get("max_iterations") or dcfg.get("max_iterations", 50))
    tasks = args.get("tasks") or ([{"goal": args["goal"], "context": args.get("context", "")}] if args.get("goal") else [])
    if not tasks:
        raise ToolError("provide goal or tasks")
    tasks = tasks[:5]
    workers = max(1, min(int(dcfg.get("max_concurrent", 3)), len(tasks)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_run_child, ctx, t["goal"], t.get("context", ""), t.get("toolsets") or default_ts, max_iter) for t in tasks]
        results = [f.result() for f in futures]
    return {"results": results} if len(results) > 1 else results[0]
