"""cronjob tool (#51-#54): the agent schedules, pauses and resumes its own jobs."""

from __future__ import annotations

from hermes.cron import jobs as cron
from hermes.tools.registry import ToolContext, ToolError, tool


@tool(
    "cronjob",
    "cronjob",
    "Manage scheduled tasks. Schedules accept '30m' (once), 'every 2h', 'daily at 9am', 'every monday at 8:30', "
    "'weekdays at 6pm' or cron '0 9 * * *'. The prompt must be self-contained (the job runs in a fresh session). "
    "deliver: 'origin' (this chat), 'local', a platform name for its home channel, or 'platform:chat_id'.",
    {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["create", "list", "update", "pause", "resume", "remove", "run", "status"]},
            "prompt": {"type": "string"},
            "schedule": {"type": "string"},
            "name": {"type": "string"},
            "deliver": {"type": "string", "default": "origin"},
            "skills": {"type": "array", "items": {"type": "string"}, "description": "Skills to load before running"},
            "repeat": {"type": "integer", "description": "Number of runs before the job completes"},
            "job_id": {"type": "string"},
        },
        "required": ["action"],
    },
    parallel_safe=False,
)
def cronjob(args: dict, ctx: ToolContext):
    action = args["action"]
    if action == "create":
        if not args.get("prompt") or not args.get("schedule"):
            raise ToolError("create needs prompt and schedule")
        try:
            job = cron.create_job(
                args["prompt"], args["schedule"], name=args.get("name") or "", deliver=args.get("deliver") or "origin",
                origin={"platform": ctx.platform, "chat_id": ctx.chat_id, "user_id": ctx.user_id},
                skills=args.get("skills"), repeat=args.get("repeat"),
            )
        except ValueError as exc:
            raise ToolError(str(exc))
        return {"created": job.summary()}
    if action == "list":
        return {"jobs": [j.summary() for j in cron.load_jobs()]}
    if action == "status":
        return cron.status_report()
    jid = args.get("job_id") or args.get("name")
    if not jid:
        raise ToolError("job_id is required")
    if action == "pause":
        j = cron.pause(jid)
    elif action == "resume":
        j = cron.resume(jid)
    elif action == "remove":
        return {"removed": cron.remove(jid)}
    elif action == "update":
        try:
            j = cron.update_job(jid, prompt=args.get("prompt"), schedule=args.get("schedule"), deliver=args.get("deliver"),
                                name=args.get("name"), skills=args.get("skills"))
        except ValueError as exc:
            raise ToolError(str(exc))
    elif action == "run":
        j = cron.find(jid)
        if not j:
            raise ToolError("job not found")
        from hermes.gateway.delivery import deliver

        return cron.run_job(j, ctx.config, lambda target, text, origin: deliver(target, text, origin, ctx.config))
    else:
        raise ToolError(f"unknown action {action}")
    if j is None:
        raise ToolError(f"job '{jid}' not found")
    return {"job": j.summary()}
