"""terminal tool (#13) with approval gating (#82)."""

from __future__ import annotations

import threading

from hermes.security.approval import detect_dangerous
from hermes.security.redact import redact
from hermes.tools.environments import BaseEnv, create_env
from hermes.tools.registry import ToolContext, ToolError, tool

_envs: dict[str, BaseEnv] = {}
_lock = threading.Lock()
MAX_OUTPUT = 30000


def get_env(ctx: ToolContext) -> BaseEnv:
    """One environment per task/session so cwd persists across calls."""
    key = ctx.task_id or ctx.session_id or "default"
    with _lock:
        env = _envs.get(key)
        if env is None:
            term_cfg = dict(ctx.config.get("terminal", {}) or {})
            if term_cfg.get("backend", "local") == "local":
                term_cfg["cwd"] = ctx.cwd
            env = create_env(term_cfg)
            _envs[key] = env
        return env


def cleanup_env(key: str) -> None:
    with _lock:
        env = _envs.pop(key, None)
    if env:
        env.cleanup()


def truncate(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    return f"{head}\n\n... [{len(text) - limit} chars truncated] ...\n\n{tail}"


@tool(
    "terminal",
    "terminal",
    "Run a shell command in the configured backend (local, docker, ssh, daytona, modal). "
    "The working directory persists between calls. Use the `process` tool for servers or long jobs. "
    "Dangerous commands require user approval.",
    {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to execute"},
            "timeout": {"type": "integer", "description": "Seconds before the command is killed (default from config)"},
        },
        "required": ["command"],
    },
    parallel_safe=False,
)
def terminal(args: dict, ctx: ToolContext):
    command = args["command"].strip()
    if not command:
        raise ToolError("empty command")
    reason = detect_dangerous(command)
    if reason and not ctx.request_approval(command, reason):
        return {"error": f"Command blocked: {reason}. The user did not approve it.", "blocked": True}
    timeout = int(args.get("timeout") or (ctx.config.get("terminal", {}) or {}).get("timeout", 180))
    try:
        env = get_env(ctx)
        code, output = env.execute(command, timeout=min(timeout, 3600))
    except RuntimeError as exc:
        raise ToolError(str(exc))
    output = redact(output) if (ctx.config.get("security", {}) or {}).get("redact_secrets", True) else output
    return {"exit_code": code, "output": truncate(output), "cwd": env.cwd}
