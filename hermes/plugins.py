"""Plugin system (`hermes plugins`).

A plugin is a directory in ``HERMES_HOME/plugins/<name>/`` (or ``./.hermes/plugins``
when ``HERMES_ENABLE_PROJECT_PLUGINS=1``) with::

    plugin.yaml      name, version, description, requires_env
    __init__.py      def register(ctx): ...

``ctx`` offers ``register_tool``, ``register_hook``, ``register_command``
(CLI/gateway slash commands) and ``register_memory_provider``. Plugins are
opt-in: they load only when listed in ``plugins.enabled``.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from hermes import config
from hermes.hooks import BUS
from hermes.tools.registry import Tool, register

log = logging.getLogger("hermes.plugins")

COMMANDS: dict[str, Callable[..., str]] = {}

TEMPLATE_INIT = '''"""{name} - a Hermes plugin."""


def hello(args, ctx):
    return {{"message": f"Hello, {{args.get('name', 'world')}}!"}}


def register(ctx):
    ctx.register_tool(
        name="{tool}_hello",
        toolset="{name}",
        description="Example tool from the {name} plugin.",
        parameters={{"type": "object", "properties": {{"name": {{"type": "string"}}}}}},
        handler=hello,
    )
    ctx.register_hook("agent:end", lambda event, data: None)
'''


@dataclass
class PluginInfo:
    name: str
    path: Path
    meta: dict = field(default_factory=dict)
    enabled: bool = False
    loaded: bool = False
    error: str = ""
    tools: list[str] = field(default_factory=list)


class PluginContext:
    def __init__(self, info: PluginInfo):
        self.info = info

    def register_tool(self, name: str, toolset: str, description: str, parameters: dict, handler: Callable, **kw: Any) -> None:
        register(Tool(name, description, parameters, handler, toolset, source=f"plugin:{self.info.name}", **kw), replace=True)
        self.info.tools.append(name)

    def register_hook(self, event: str, fn: Callable[[str, dict], Any]) -> None:
        BUS.register(event, fn)

    def register_command(self, name: str, fn: Callable[..., str]) -> None:
        COMMANDS[name] = fn

    def register_memory_provider(self, provider: Any) -> None:
        from hermes.memory.store import register_memory_provider

        register_memory_provider(provider)


def plugin_dirs() -> list[Path]:
    dirs = [config.path("plugins")]
    if os.environ.get("HERMES_ENABLE_PROJECT_PLUGINS"):
        dirs.append(Path.cwd() / ".hermes" / "plugins")
    return dirs


def discover(cfg: dict) -> list[PluginInfo]:
    enabled = set((cfg.get("plugins", {}) or {}).get("enabled", []))
    out = []
    for root in plugin_dirs():
        if not root.exists():
            continue
        for d in sorted(p for p in root.iterdir() if p.is_dir()):
            meta_path = d / "plugin.yaml"
            meta = yaml.safe_load(meta_path.read_text()) if meta_path.exists() else {}
            name = (meta or {}).get("name", d.name)
            out.append(PluginInfo(name, d, meta or {}, name in enabled))
    return out


_loaded: dict[str, PluginInfo] = {}


def load_plugins(cfg: dict) -> list[PluginInfo]:
    infos = discover(cfg)
    for info in infos:
        if not info.enabled or info.name in _loaded:
            continue
        missing = [e for e in info.meta.get("requires_env", []) if not os.environ.get(e)]
        if missing:
            info.error = "missing env: " + ", ".join(missing)
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"hermes_plugin_{info.name}", info.path / "__init__.py",
                                                          submodule_search_locations=[str(info.path)])
            if not spec or not spec.loader:
                raise ImportError("no __init__.py")
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)
            mod.register(PluginContext(info))
            info.loaded = True
            _loaded[info.name] = info
        except Exception as exc:
            info.error = f"{type(exc).__name__}: {exc}"
            log.exception("plugin %s failed to load", info.name)
    return infos


def scaffold(name: str) -> Path:
    d = config.path("plugins", name)
    d.mkdir(parents=True, exist_ok=False)
    (d / "plugin.yaml").write_text(yaml.safe_dump({"name": name, "version": "0.1.0", "description": f"{name} plugin", "requires_env": []}))
    (d / "__init__.py").write_text(TEMPLATE_INIT.format(name=name, tool=name.replace("-", "_")))
    return d


def install_git(url: str) -> Path:
    name = url.rstrip("/").split("/")[-1].removesuffix(".git")
    dest = config.path("plugins", name)
    subprocess.run(["git", "clone", "--depth", "1", url, str(dest)], check=True)
    return dest
