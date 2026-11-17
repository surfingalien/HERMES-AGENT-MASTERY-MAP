"""`hermes doctor` (#10 troubleshooting): diagnose config, keys, tools and services."""

from __future__ import annotations

import os
import shutil
import stat
import sys

from hermes import config

OK, WARN, FAIL = "✓", "⚠", "✗"


def run_doctor(fix: bool = False, ping: bool = False) -> int:
    results: list[tuple[str, str, str]] = []

    def add(status: str, what: str, hint: str = "") -> None:
        results.append((status, what, hint))

    # Python & home
    add(OK if sys.version_info >= (3, 10) else FAIL, f"Python {sys.version.split()[0]}", "Python 3.10+ required")
    home = config.hermes_home()
    if not home.exists():
        if fix:
            config.ensure_home()
            add(OK, f"created {home}")
        else:
            add(FAIL, f"{home} missing", "run `hermes setup` or `hermes doctor --fix`")
    else:
        add(OK, f"home: {home}")
        if fix:
            config.ensure_home()
    env_path = config.path(".env")
    if env_path.exists():
        mode = stat.S_IMODE(env_path.stat().st_mode)
        if mode & 0o077:
            if fix:
                env_path.chmod(0o600)
                add(OK, ".env permissions fixed to 600")
            else:
                add(WARN, f".env is mode {oct(mode)}", "chmod 600 ~/.hermes/.env (or --fix)")
        else:
            add(OK, ".env permissions 600")

    # Config
    try:
        cfg = config.load_config()
        add(OK, "config.yaml parses")
    except Exception as exc:
        add(FAIL, f"config.yaml invalid: {exc}", "fix the YAML or delete it and run `hermes setup`")
        return _print(results)

    # Provider
    provider = cfg.get_path("model.provider")
    model = cfg.get_path("model.default")
    from hermes.providers.openai_compat import PRESETS

    key_env = cfg.get_path("model.api_key_env") or (PRESETS.get(provider, {}) or {}).get("key_env") or ("ANTHROPIC_API_KEY" if provider == "anthropic" else "")
    if provider == "mock":
        add(WARN, "provider is 'mock' (offline testing)", "run `hermes model` to pick a real provider")
    elif key_env and not os.environ.get(key_env):
        add(FAIL, f"{provider}: {key_env} not set", "run `hermes setup` (or `hermes config env set ...`)")
    else:
        add(OK, f"model: {provider}:{model}")
    if provider == "anthropic":
        try:
            import anthropic  # noqa: F401
            add(OK, "anthropic SDK installed")
        except ImportError:
            add(FAIL, "anthropic SDK missing", "pip install 'hermes-agent-replica[anthropic]'")
    if ping and provider != "mock":
        try:
            from hermes.providers import create_provider

            resp = create_provider(cfg).chat([{"role": "user", "content": "Reply with: pong"}], max_tokens=50)
            add(OK, f"provider replied: {resp.content.strip()[:40]!r}")
        except Exception as exc:
            add(FAIL, f"provider call failed: {exc}", "check key, model name and network")
    for i, fb in enumerate(cfg.get("fallback_providers") or []):
        add(OK, f"fallback {i + 1}: {fb.get('provider')}:{fb.get('model')}")

    # Binaries
    for binary, why in (("git", "checkpoints & rollback"), ("rg", "fast file search (optional)"), ("docker", "docker terminal backend (optional)"),
                        ("node", "many MCP servers (optional)"), ("edge-tts", "free text-to-speech (optional)"), ("ffmpeg", "voice mode (optional)")):
        add(OK if shutil.which(binary) else (FAIL if binary == "git" else WARN), f"{binary}: {'found' if shutil.which(binary) else 'not found'}", why)
    backend = cfg.get_path("terminal.backend")
    if backend == "docker" and not shutil.which("docker"):
        add(FAIL, "terminal backend is docker but docker is not installed")
    if backend == "ssh" and not cfg.get_path("terminal.ssh_host"):
        add(FAIL, "terminal backend is ssh but terminal.ssh_host is empty")
    try:
        import playwright  # noqa: F401
        add(OK, "playwright installed (full browser)")
    except ImportError:
        add(WARN, "playwright not installed - browser uses lite HTTP engine", "pip install playwright && playwright install chromium")

    # Tools
    from hermes.tools import registry

    registry.load_builtin_tools()
    unavailable = [(t.name, t.availability()[1]) for t in registry.REGISTRY.values() if not t.availability()[0]]
    add(OK if not unavailable else WARN, f"tools: {len(registry.REGISTRY) - len(unavailable)}/{len(registry.REGISTRY)} available",
        "; ".join(f"{n} ({why})" for n, why in unavailable))
    if cfg.get_path("approvals.mode") == "off":
        add(WARN, "approvals are OFF - dangerous commands run without asking", "hermes config set approvals.mode manual")

    # Skills
    from hermes.skills.manager import SkillManager

    mgr = SkillManager(cfg)
    report = mgr.check()
    bad = [r for r in report if r["issues"]]
    add(OK if not bad else WARN, f"skills: {len(report)} found, {len(bad)} with issues", "run `hermes skills check`")

    # Sessions db
    try:
        from hermes.sessions.db import SessionDB

        SessionDB().list_sessions(1)
        add(OK, "session database OK (FTS5)")
    except Exception as exc:
        add(FAIL, f"session database: {exc}")

    # Messaging
    from hermes.gateway.platforms import ADAPTERS

    pcfg = cfg.get_path("gateway.platforms", {}) or {}
    configured = [n for n, cls in ADAPTERS.items() if cls.configured(pcfg.get(n, {}) or {})]
    add(OK, f"messaging platforms configured: {', '.join(configured) or 'none'}", "" if configured else "optional: `hermes gateway setup`")
    for n in configured:
        cls = ADAPTERS[n]
        if not cls(pcfg.get(n, {}) or {}).allowed_users() and not cfg.get_path("gateway.allow_all_users"):
            add(WARN, f"{n}: no allowed users - strangers get pairing codes", f"set {n.upper()}_ALLOWED_USERS")

    # Cron
    from hermes.cron import jobs

    js = jobs.load_jobs()
    failing = [j.id for j in js if j.last_status == "error"]
    add(OK if not failing else WARN, f"cron: {len(js)} jobs, {len(failing)} failing", "hermes cron list" if failing else "")

    # MCP
    for name, spec in (cfg.get("mcp_servers") or {}).items():
        if spec.get("command") and not shutil.which(spec["command"]):
            add(FAIL, f"mcp {name}: command '{spec['command']}' not found")
        else:
            add(OK, f"mcp {name}: configured ({'http' if spec.get('url') else 'stdio'})", "test with `hermes mcp test " + name + "`")
    return _print(results)


def _print(results) -> int:
    width = max((len(w) for _, w, _ in results), default=20)
    for status, what, hint in results:
        line = f" {status} {what:<{width}}"
        if hint and status != OK:
            line += f"  → {hint}"
        print(line)
    fails = sum(1 for s, _, _ in results if s == FAIL)
    warns = sum(1 for s, _, _ in results if s == WARN)
    print(f"\n{fails} problem(s), {warns} warning(s).")
    return 1 if fails else 0
