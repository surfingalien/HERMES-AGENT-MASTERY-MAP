"""Configuration, paths and environment handling.

Everything lives under ``HERMES_HOME`` (default ``~/.hermes``):

    config.yaml      settings (model, toolsets, terminal backend, gateway ...)
    .env             secrets (API keys, bot tokens) - chmod 600
    SOUL.md          the agent's personality / identity
    memories/        MEMORY.md + USER.md (persistent curated memory)
    skills/          SKILL.md procedures (agentskills.io compatible)
    state.db         SQLite session store with FTS5 search
    cron/            scheduled jobs + their outputs
    logs/            agent.log / errors.log (secrets redacted)
    checkpoints/     shadow git repos for file-change rollback
    plugins/ hooks/  extension points
    profiles/<name>/ isolated specialist-bot homes (each is a full HERMES_HOME)
"""

from __future__ import annotations

import copy
import os
import re
import stat
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG: dict[str, Any] = {
    "model": {
        "provider": "openrouter",
        "default": "anthropic/claude-opus-5-5",
        "base_url": "",
        "api_key_env": "",
        "max_tokens": 16000,
        "reasoning_effort": "medium",
        "context_length": 200000,
    },
    # Ordered list of {provider, model[, base_url]} tried when the primary fails.
    "fallback_providers": [],
    # provider -> list of env var names holding API keys (rotated on 429/401).
    "credential_pools": {},
    "credential_pool_strategy": "round_robin",  # round_robin | fill_first
    # OpenRouter provider routing preferences (passed through as `provider`).
    "provider_routing": {},
    # Send short/simple turns to a cheaper model.
    "smart_model_routing": {"enabled": False, "cheap_model": "", "max_simple_chars": 160},
    "agent": {
        "max_turns": 60,
        "personality": "",
        "verify_outputs": False,
        "tool_output_limit": 20000,
        "parallel_tools": True,
    },
    # Toolsets enabled per platform. "hermes-cli" etc. are composite toolsets.
    "platform_toolsets": {
        "cli": ["hermes-cli"],
        "telegram": ["hermes-gateway"],
        "discord": ["hermes-gateway"],
        "slack": ["hermes-gateway"],
        "whatsapp": ["hermes-gateway"],
        "signal": ["hermes-gateway"],
        "email": ["hermes-gateway"],
        "teams": ["hermes-gateway"],
        "google_chat": ["hermes-gateway"],
        "api": ["hermes-api"],
        "web": ["hermes-cli"],
        "cron": ["hermes-cron"],
        "acp": ["hermes-acp"],
    },
    "disabled_tools": [],
    "terminal": {
        "backend": "local",  # local | docker | ssh | daytona | modal
        "cwd": ".",
        "timeout": 180,
        "persistent_shell": True,
        "env_passthrough": [],
        "docker_image": "python:3.12-slim",
        "docker_network": "bridge",
        "docker_memory": "2g",
        "docker_cpus": "2",
        "docker_extra_args": [],
        "ssh_host": "",
        "ssh_user": "",
        "ssh_port": 22,
        "ssh_key": "",
        "remote_cwd": "~",
        "daytona_image": "",
        "modal_image": "python:3.12-slim",
    },
    "memory": {
        "memory_enabled": True,
        "user_profile_enabled": True,
        "memory_char_limit": 2200,
        "user_char_limit": 1375,
        "nudge_interval": 10,
        # Platforms where the agent may read but never write memory (privacy).
        "read_only_platforms": [],
        "provider": "builtin",
    },
    "skills": {
        "creation_nudge_interval": 15,
        "external_dirs": [],
        "disabled": [],
    },
    "approvals": {
        "mode": "manual",  # manual | smart | off
        "timeout": 300,
        "allowlist": [],
    },
    "security": {
        "redact_secrets": True,
        "scan_context_files": True,
        "protected_paths": ["~/.ssh", "~/.aws", "~/.gnupg", "~/.hermes/.env"],
    },
    "checkpoints": {"enabled": True, "max_snapshots": 50},
    "compression": {
        "enabled": True,
        "threshold": 0.50,
        "protect_last_n": 20,
        "summary_model": "",
    },
    "delegation": {
        "max_iterations": 50,
        "max_concurrent": 3,
        "max_depth": 2,
        "default_toolsets": ["terminal", "file", "web"],
    },
    "browser": {"headless": True, "engine": "auto", "timeout": 30},
    "web": {"backend": "auto", "max_extract_chars": 15000},
    "tts": {"provider": "auto", "voice": "alloy", "edge_voice": "en-US-AriaNeural"},
    "stt": {"provider": "auto", "model": "whisper-1"},
    "image_gen": {"provider": "auto", "model": ""},
    "vision": {"model": ""},
    "gateway": {
        "platforms": {},
        "home_channels": {},
        "allow_all_users": False,
        "dm_pairing": True,
        "group_require_mention": True,
        "cron_tick_seconds": 60,
    },
    "mcp_servers": {},
    "plugins": {"enabled": []},
    "api_server": {"host": "127.0.0.1", "port": 8642, "key_env": "API_SERVER_KEY", "model_name": "hermes-agent"},
    "dashboard": {"token_env": "HERMES_WEB_TOKEN", "allowed_hosts": []},
    "kanban": {"workers": 2, "poll_seconds": 5},
    "pricing": {},
    "display": {"show_tool_calls": True, "show_reasoning": False, "streaming": True},
}

SECRET_ENV_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASS", "CREDENTIAL")


def base_home() -> Path:
    """The root home, ignoring profile selection."""
    return Path(os.environ.get("HERMES_BASE_HOME") or Path.home() / ".hermes").expanduser()


def hermes_home() -> Path:
    """Active HERMES_HOME (a profile directory when a profile is active)."""
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env).expanduser()
    return base_home()


def use_profile(name: str | None) -> Path:
    """Switch the process to a named profile (isolated specialist bot)."""
    if not name or name == "default":
        os.environ.pop("HERMES_HOME", None)
        return hermes_home()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name):
        raise ValueError(f"invalid profile name: {name!r}")
    home = base_home() / "profiles" / name
    os.environ["HERMES_HOME"] = str(home)
    return home


def path(*parts: str) -> Path:
    return hermes_home().joinpath(*parts)


def ensure_home() -> Path:
    home = hermes_home()
    for sub in ("memories", "skills", "cron/output", "logs", "checkpoints", "plugins", "hooks", "cache/images", "cache/audio", "cache/documents", "sessions"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    try:
        home.chmod(0o700)
    except OSError:
        pass
    return home


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load_env_file(env_path: Path | None = None, override: bool = False) -> dict[str, str]:
    """Load KEY=VALUE pairs from HERMES_HOME/.env into os.environ."""
    env_path = env_path or path(".env")
    loaded: dict[str, str] = {}
    if not env_path.exists():
        return loaded
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        loaded[key] = value
        if override or key not in os.environ:
            os.environ[key] = value
    return loaded


def set_env_value(key: str, value: str) -> None:
    """Persist a secret to HERMES_HOME/.env (mode 600) and the live env."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        raise ValueError(f"invalid env var name: {key}")
    ensure_home()
    env_path = path(".env")
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    out, found = [], False
    for line in lines:
        if re.match(rf"^\s*(export\s+)?{re.escape(key)}\s*=", line):
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"{key}={value}")
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    env_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    os.environ[key] = value


class Config(dict):
    """Dict with dotted-path helpers. ``cfg.get_path("model.default")``."""

    def get_path(self, dotted: str, default: Any = None) -> Any:
        node: Any = self
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set_path(self, dotted: str, value: Any) -> None:
        node = self
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value


def load_config(config_path: Path | None = None, load_env: bool = True) -> Config:
    if load_env:
        load_env_file()
    config_path = config_path or path("config.yaml")
    user: dict = {}
    if config_path.exists():
        user = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(user, dict):
            raise ValueError(f"{config_path} must contain a YAML mapping")
    return Config(deep_merge(DEFAULT_CONFIG, user))


def save_config(cfg: dict, config_path: Path | None = None) -> Path:
    ensure_home()
    config_path = config_path or path("config.yaml")
    # Only persist values that differ from defaults to keep the file readable.
    diff = _diff(DEFAULT_CONFIG, dict(cfg))
    config_path.write_text(yaml.safe_dump(diff, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return config_path


def _diff(default: dict, current: dict) -> dict:
    out = {}
    for key, value in current.items():
        if key not in default:
            out[key] = value
        elif isinstance(value, dict) and isinstance(default[key], dict):
            sub = _diff(default[key], value)
            if sub:
                out[key] = sub
        elif value != default[key]:
            out[key] = value
    return out


def parse_value(text: str) -> Any:
    """Parse a CLI-supplied value (``hermes config set a.b 3``) as YAML."""
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return text


def is_secret_name(name: str) -> bool:
    upper = name.upper()
    return any(h in upper for h in SECRET_ENV_HINTS)
