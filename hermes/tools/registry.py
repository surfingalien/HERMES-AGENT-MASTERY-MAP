"""Tool & toolset registry.

A *tool* is a callable capability with a JSON-schema; a *toolset* groups tools
so they can be enabled per platform (``hermes tools``). Map: #11-#20, Core
building blocks "Tool" and "Toolset".
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


class ToolError(Exception):
    """Raise inside a handler to return a clean error to the model."""


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable[[dict, "ToolContext"], Any]
    toolset: str
    requires_env: tuple[str, ...] = ()
    requires_bin: tuple[str, ...] = ()
    check_fn: Callable[[], tuple[bool, str]] | None = None
    parallel_safe: bool = True
    source: str = "builtin"

    def schema(self) -> dict:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}

    def availability(self) -> tuple[bool, str]:
        missing_env = [e for e in self.requires_env if not os.environ.get(e)]
        # requires_env entries separated by "|" mean "any of".
        missing_env = [e for e in missing_env if not any(os.environ.get(x) for x in e.split("|"))]
        if missing_env:
            return False, "missing env: " + ", ".join(missing_env)
        missing_bin = [b for b in self.requires_bin if not any(shutil.which(x) for x in b.split("|"))]
        if missing_bin:
            return False, "missing binary: " + ", ".join(missing_bin)
        if self.check_fn:
            try:
                return self.check_fn()
            except Exception as exc:  # pragma: no cover - defensive
                return False, str(exc)
        return True, ""


@dataclass
class ToolContext:
    """Everything a tool handler may need about the running agent."""

    config: dict = field(default_factory=dict)
    agent: Any = None
    session_id: str = ""
    platform: str = "cli"
    cwd: str = field(default_factory=os.getcwd)
    task_id: str = ""
    depth: int = 0
    user_id: str = ""
    chat_id: str = ""
    approve: Callable[[str, str], bool] | None = None
    clarify: Callable[[str, list[str] | None], str] | None = None
    checkpoint: Callable[[str], None] | None = None
    interrupt: threading.Event = field(default_factory=threading.Event)
    extra: dict = field(default_factory=dict)

    def resolve_path(self, p: str) -> Path:
        path = Path(os.path.expanduser(p))
        if not path.is_absolute():
            path = Path(self.cwd) / path
        return path

    def request_approval(self, command: str, reason: str) -> bool:
        if self.approve is None:
            return False
        return bool(self.approve(command, reason))


REGISTRY: dict[str, Tool] = {}
_lock = threading.Lock()

# Toolset name -> list of tool names, or "@other" to include another toolset.
TOOLSETS: dict[str, list[str]] = {}

CORE_TOOLSETS = [
    "web", "terminal", "file", "browser", "vision", "image_gen", "tts", "memory",
    "session_search", "skills", "delegation", "cronjob", "code_execution",
    "messaging", "todo", "clarify", "kanban", "mcp",
]

COMPOSITES: dict[str, list[str]] = {
    "hermes-cli": CORE_TOOLSETS,
    "hermes-gateway": CORE_TOOLSETS,
    "hermes-api": [t for t in CORE_TOOLSETS if t not in ("clarify", "messaging")],
    "hermes-cron": [t for t in CORE_TOOLSETS if t not in ("clarify", "cronjob")],
    "hermes-acp": ["file", "terminal", "web", "skills", "memory", "session_search", "todo", "code_execution", "delegation", "mcp"],
    "safe": ["web", "vision", "image_gen", "skills", "todo", "clarify", "session_search"],
    "research": ["web", "browser", "vision", "file", "todo", "session_search", "skills", "memory"],
}


def register(tool: Tool, replace: bool = False) -> Tool:
    with _lock:
        if tool.name in REGISTRY and not replace and REGISTRY[tool.name].source != tool.source:
            raise ValueError(f"tool name collision: {tool.name}")
        REGISTRY[tool.name] = tool
        members = TOOLSETS.setdefault(tool.toolset, [])
        if tool.name not in members:
            members.append(tool.name)
    return tool


def unregister(name: str) -> None:
    with _lock:
        tool = REGISTRY.pop(name, None)
        if tool and name in TOOLSETS.get(tool.toolset, []):
            TOOLSETS[tool.toolset].remove(name)


def tool(name: str, toolset: str, description: str, parameters: dict | None = None, **kwargs) -> Callable:
    """Decorator: ``@tool("read_file", "file", "Read a file", {...})``."""

    def deco(fn: Callable) -> Callable:
        register(Tool(name, description, parameters or {"type": "object", "properties": {}}, fn, toolset, **kwargs), replace=True)
        return fn

    return deco


def expand_toolsets(names: list[str]) -> list[str]:
    """Expand composite toolset names to concrete toolset names."""
    out: list[str] = []
    for n in names:
        for t in COMPOSITES.get(n, [n]):
            if t == "mcp":
                out.extend(sorted(k for k in TOOLSETS if k.startswith("mcp-")))
            elif t not in out:
                out.append(t)
    return out


def resolve_tools(toolsets: list[str], disabled: list[str] | None = None, only_available: bool = True) -> list[Tool]:
    disabled = set(disabled or [])
    chosen: list[Tool] = []
    seen = set()
    for ts in expand_toolsets(toolsets):
        for name in TOOLSETS.get(ts, []):
            if name in seen or name in disabled or name not in REGISTRY:
                continue
            t = REGISTRY[name]
            if only_available and not t.availability()[0]:
                continue
            seen.add(name)
            chosen.append(t)
    return chosen


def validate_args(t: Tool, args: dict) -> str:
    """Light JSON-schema validation of top-level args. Returns error text or ''."""
    schema = t.parameters or {}
    props = schema.get("properties", {})
    for req in schema.get("required", []):
        if req not in args or args[req] is None:
            return f"missing required argument '{req}'"
    type_map = {"string": (str,), "integer": (int,), "number": (int, float), "boolean": (bool,), "array": (list,), "object": (dict,)}
    for key, val in list(args.items()):
        spec = props.get(key)
        if not spec or "type" not in spec or val is None:
            continue
        types = spec["type"] if isinstance(spec["type"], list) else [spec["type"]]
        py = tuple(x for ty in types for x in type_map.get(ty, (object,)))
        if isinstance(val, bool) and "boolean" not in types and object not in py:
            return f"argument '{key}' must be of type {'/'.join(types)}"
        if not isinstance(val, py):
            # tolerate numeric strings for integers
            if "integer" in types and isinstance(val, str) and val.lstrip("-").isdigit():
                args[key] = int(val)
                continue
            return f"argument '{key}' must be of type {'/'.join(types)}"
        if "enum" in spec and val not in spec["enum"]:
            return f"argument '{key}' must be one of {spec['enum']}"
    return ""


def run_tool(name: str, args: dict, ctx: ToolContext) -> tuple[str, bool]:
    """Execute a tool. Returns (output_text, is_error)."""
    t = REGISTRY.get(name)
    if t is None:
        return json.dumps({"error": f"unknown tool '{name}'"}), True
    err = validate_args(t, args)
    if err:
        return json.dumps({"error": err, "tool": name}), True
    try:
        result = t.handler(args, ctx)
    except ToolError as exc:
        return json.dumps({"error": str(exc)}), True
    except Exception as exc:
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"}), True
    if isinstance(result, (dict, list)):
        return json.dumps(result, ensure_ascii=False, default=str), bool(isinstance(result, dict) and result.get("error"))
    return ("" if result is None else str(result)), False


_loaded = False


def load_builtin_tools() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    from hermes.tools import (  # noqa: F401
        browser, code_exec, cron_tool, delegate, files, kanban_tool, media, memory_tool,
        messaging, process, session_search_tool, skills_tools, terminal, todo, web,
    )
