"""Event hooks (#60 event hooks).

Two kinds:
* **Directory hooks** - ``HERMES_HOME/hooks/<name>/HOOK.yaml`` + ``handler.py``::

      name: notify-on-long-task
      events: ["agent:end", "tool:post", "command:*"]

  ``handler.py`` defines ``handle(event_type: str, context: dict)``.
* **Plugin hooks** - registered in code via ``ctx.register_hook(event, fn)``.

Events: gateway:startup, session:start, session:end, session:reset,
agent:start, agent:step, agent:end, tool:pre, tool:post, llm:pre, llm:post,
command:<name>, cron:run. Errors in hooks are logged, never raised. A
``tool:pre`` hook may return ``{"block": "reason"}`` to veto a tool call.
"""

from __future__ import annotations

import fnmatch
import importlib.util
import logging
from collections import defaultdict
from typing import Any, Callable

import yaml

from hermes import config

log = logging.getLogger("hermes.hooks")


class HookBus:
    def __init__(self):
        self.handlers: dict[str, list[Callable[[str, dict], Any]]] = defaultdict(list)
        self.loaded_dirs = False

    def register(self, pattern: str, fn: Callable[[str, dict], Any]) -> None:
        self.handlers[pattern].append(fn)

    def load_directory_hooks(self) -> list[str]:
        if self.loaded_dirs:
            return []
        self.loaded_dirs = True
        names = []
        root = config.path("hooks")
        if not root.exists():
            return names
        for hook_yaml in sorted(root.glob("*/HOOK.yaml")):
            try:
                meta = yaml.safe_load(hook_yaml.read_text()) or {}
                handler = hook_yaml.parent / "handler.py"
                spec = importlib.util.spec_from_file_location(f"hermes_hook_{hook_yaml.parent.name}", handler)
                if not spec or not spec.loader:
                    continue
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                fn = getattr(mod, "handle", None)
                if not callable(fn):
                    continue
                for ev in meta.get("events", []):
                    self.register(ev, fn)
                names.append(meta.get("name", hook_yaml.parent.name))
            except Exception:
                log.exception("failed to load hook %s", hook_yaml)
        return names

    def emit(self, event: str, context: dict | None = None) -> list[Any]:
        results = []
        ctx = context or {}
        for pattern, fns in list(self.handlers.items()):
            if pattern == event or fnmatch.fnmatch(event, pattern):
                for fn in fns:
                    try:
                        results.append(fn(event, ctx))
                    except Exception:
                        log.exception("hook for %s failed", event)
        return results


BUS = HookBus()


def emit(event: str, **context: Any) -> list[Any]:
    return BUS.emit(event, context)
