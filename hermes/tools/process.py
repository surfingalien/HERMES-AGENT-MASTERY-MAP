"""process tool (#14 process_manage): background processes with log polling."""

from __future__ import annotations

import collections
import itertools
import os
import signal
import subprocess
import threading
import time

from hermes.security.approval import detect_dangerous
from hermes.security.redact import redact
from hermes.tools.environments import sanitized_env
from hermes.tools.registry import ToolContext, ToolError, tool

_ids = itertools.count(1)


class BackgroundProcess:
    def __init__(self, command: str, cwd: str, env: dict):
        self.id = f"proc_{next(_ids)}"
        self.command = command
        self.started = time.time()
        self.buffer: collections.deque[str] = collections.deque(maxlen=5000)
        self.read_pos = 0
        self.total_lines = 0
        self.proc = subprocess.Popen(
            ["bash", "-c", command], cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE, text=True, bufsize=1, start_new_session=True,
        )
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()

    def _pump(self) -> None:
        assert self.proc.stdout
        for line in self.proc.stdout:
            self.buffer.append(line.rstrip("\n"))
            self.total_lines += 1

    def status(self) -> dict:
        rc = self.proc.poll()
        return {"id": self.id, "command": self.command, "running": rc is None, "exit_code": rc,
                "uptime_s": round(time.time() - self.started, 1), "lines": self.total_lines}

    def new_output(self) -> str:
        lines = list(self.buffer)
        dropped = self.total_lines - len(lines)
        start = max(0, self.read_pos - dropped)
        out = lines[start:]
        self.read_pos = self.total_lines
        return "\n".join(out)

    def kill(self) -> None:
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


PROCESSES: dict[str, BackgroundProcess] = {}


@tool(
    "process",
    "terminal",
    "Manage background processes (dev servers, watchers, long builds). "
    "actions: start, list, poll (new output), wait, write (stdin), kill.",
    {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["start", "list", "poll", "wait", "write", "kill"]},
            "command": {"type": "string", "description": "for start"},
            "id": {"type": "string", "description": "process id for poll/wait/write/kill"},
            "input": {"type": "string", "description": "text for write"},
            "timeout": {"type": "integer", "default": 60},
        },
        "required": ["action"],
    },
    parallel_safe=False,
)
def process(args: dict, ctx: ToolContext):
    action = args["action"]
    if action == "start":
        cmd = args.get("command") or ""
        if not cmd:
            raise ToolError("command is required for start")
        reason = detect_dangerous(cmd)
        if reason and not ctx.request_approval(cmd, reason):
            return {"error": f"blocked: {reason}"}
        backend = (ctx.config.get("terminal", {}) or {}).get("backend", "local")
        if backend != "local":
            raise ToolError("background processes are only supported on the local backend; use terminal with nohup")
        p = BackgroundProcess(cmd, ctx.cwd, sanitized_env((ctx.config.get("terminal", {}) or {}).get("env_passthrough")))
        PROCESSES[p.id] = p
        time.sleep(0.5)
        return {**p.status(), "output": redact(p.new_output())[-4000:]}
    if action == "list":
        return {"processes": [p.status() for p in PROCESSES.values()]}
    p = PROCESSES.get(args.get("id") or "")
    if not p:
        raise ToolError(f"unknown process id {args.get('id')!r}")
    if action == "poll":
        return {**p.status(), "output": redact(p.new_output())[-20000:]}
    if action == "wait":
        try:
            p.proc.wait(timeout=int(args.get("timeout") or 60))
        except subprocess.TimeoutExpired:
            pass
        return {**p.status(), "output": redact(p.new_output())[-20000:]}
    if action == "write":
        if p.proc.stdin and p.proc.poll() is None:
            p.proc.stdin.write((args.get("input") or "") + "\n")
            p.proc.stdin.flush()
        return p.status()
    if action == "kill":
        p.kill()
        return p.status()
    raise ToolError(f"unknown action {action}")


def kill_all() -> None:
    for p in PROCESSES.values():
        p.kill()
