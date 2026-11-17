"""session_search tool (#24 session_search, #25 cross-session recall)."""

from __future__ import annotations

from hermes.sessions.db import SessionDB
from hermes.tools.registry import ToolContext, tool


@tool(
    "session_search",
    "session_search",
    "Search past conversations (all platforms) with full-text search. Use it when the user refers to something "
    "discussed before ('like last time', 'that bug from Tuesday'). Set summarize=true to get a focused recap of the "
    "best matching session instead of raw snippets.",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Keywords; FTS5 syntax (OR, \"phrases\", prefix*) allowed"},
            "limit": {"type": "integer", "default": 5},
            "summarize": {"type": "boolean", "default": False},
            "session_id": {"type": "string", "description": "Read a specific session's transcript instead of searching"},
        },
        "required": ["query"],
    },
)
def session_search(args: dict, ctx: ToolContext):
    db = ctx.agent.db if ctx.agent is not None and getattr(ctx.agent, "db", None) else SessionDB()
    if args.get("session_id"):
        msgs = db.get_messages(args["session_id"])
        lines = [f"{m['role']}: {str(m.get('content') or '')[:800]}" for m in msgs if m["role"] in ("user", "assistant") and m.get("content")]
        return {"session_id": args["session_id"], "transcript": "\n".join(lines)[-15000:]}
    hits = db.search(args["query"], limit=min(int(args.get("limit") or 5), 20), exclude_session=ctx.session_id)
    if not hits:
        return {"results": [], "note": "no matching past sessions"}
    if args.get("summarize") and ctx.agent is not None:
        best = hits[0]["session_id"]
        msgs = db.get_messages(best)
        transcript = "\n".join(f"{m['role']}: {str(m.get('content') or '')[:1500]}" for m in msgs
                               if m["role"] in ("user", "assistant") and m.get("content"))[-30000:]
        prompt = (f"Below is a past conversation. Summarize only what is relevant to: {args['query']!r}. "
                  f"Include decisions, commands, file paths and outcomes.\n\n{transcript}")
        resp = ctx.agent.provider.chat([{"role": "user", "content": prompt}], tools=None, max_tokens=2000)
        ctx.agent.record_usage(resp)
        return {"results": hits, "summary_of": best, "summary": resp.content}
    return {"results": hits}
