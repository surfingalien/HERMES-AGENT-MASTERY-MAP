"""`hermes setup` (#4 hermes setup --portal, #5 choose model provider, #8 configuration)."""

from __future__ import annotations

import getpass
import os
import webbrowser

from hermes import config
from hermes.providers import PROVIDER_CHOICES

PROVIDER_KEYS = {
    "openrouter": ("OPENROUTER_API_KEY", "https://openrouter.ai/keys"),
    "nous": ("NOUS_API_KEY", "https://portal.nousresearch.com"),
    "anthropic": ("ANTHROPIC_API_KEY", "https://platform.claude.com/settings/keys"),
    "openai": ("OPENAI_API_KEY", "https://platform.openai.com/api-keys"),
    "gemini": ("GEMINI_API_KEY", "https://aistudio.google.com/apikey"),
    "deepseek": ("DEEPSEEK_API_KEY", "https://platform.deepseek.com/api_keys"),
    "groq": ("GROQ_API_KEY", "https://console.groq.com/keys"),
    "xai": ("XAI_API_KEY", "https://console.x.ai"),
    "custom": ("CUSTOM_API_KEY", ""),
}

OPTIONAL_KEYS = [
    ("TAVILY_API_KEY", "Tavily web search (better than the keyless DuckDuckGo fallback)"),
    ("FIRECRAWL_API_KEY", "Firecrawl page extraction"),
    ("FAL_KEY", "FAL image generation"),
    ("ELEVENLABS_API_KEY", "ElevenLabs text-to-speech"),
    ("GROQ_API_KEY", "Groq Whisper speech-to-text (voice messages)"),
]


def _ask(prompt: str, default: str = "") -> str:
    try:
        val = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    except EOFError:
        return default
    return val or default


def _yes(prompt: str, default: bool = False) -> bool:
    ans = _ask(prompt + (" (Y/n)" if default else " (y/N)"), "").lower()
    return default if not ans else ans.startswith("y")


def _secret(name: str, url: str = "") -> None:
    if os.environ.get(name):
        if not _yes(f"{name} is already set. Replace it?"):
            return
    if url:
        print(f"  Get a key at: {url}")
    try:
        val = getpass.getpass(f"  {name} (input hidden, Enter to skip): ").strip()
    except EOFError:
        val = ""
    if val:
        config.set_env_value(name, val)
        print(f"  ✓ saved {name} to {config.path('.env')} (mode 600)")


def choose_provider(cfg: config.Config) -> None:
    print("\nChoose a model provider:")
    for i, (key, desc, default_model) in enumerate(PROVIDER_CHOICES, 1):
        print(f"  {i:2}. {key:11} {desc}" + (f"  (default model: {default_model})" if default_model else ""))
    current = cfg.get_path("model.provider")
    choice = _ask("Provider number or name", current)
    keys = [p[0] for p in PROVIDER_CHOICES]
    provider = keys[int(choice) - 1] if choice.isdigit() and 1 <= int(choice) <= len(keys) else choice
    if provider not in keys:
        print(f"  unknown provider {provider!r}; keeping {current}")
        return
    default_model = next(m for k, _, m in PROVIDER_CHOICES if k == provider)
    cfg.set_path("model.provider", provider)
    model = _ask("Model", default_model if provider != current else cfg.get_path("model.default") or default_model)
    cfg.set_path("model.default", model)
    if provider in ("custom", "ollama", "lmstudio"):
        from hermes.providers.openai_compat import PRESETS

        cfg.set_path("model.base_url", _ask("Base URL (OpenAI-compatible)", cfg.get_path("model.base_url") or PRESETS[provider]["base_url"]))
    if provider in PROVIDER_KEYS:
        _secret(*PROVIDER_KEYS[provider])
    if provider == "anthropic":
        try:
            import anthropic  # noqa: F401
        except ImportError:
            print("  ! install the official SDK: pip install 'hermes-agent-replica[anthropic]'")


def portal_flow(cfg: config.Config) -> None:
    """Nous Portal quick path: open the portal, paste the key, use Hermes models."""
    url = PROVIDER_KEYS["nous"][1]
    print(f"\nNous Portal setup\n  1. Sign in at {url} and create an API key.\n  2. Paste it below.")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    cfg.set_path("model.provider", "nous")
    cfg.set_path("model.default", _ask("Model", cfg.get_path("model.default") if cfg.get_path("model.provider") == "nous" else "Hermes-4-405B"))
    _secret("NOUS_API_KEY")


def run_setup(portal: bool = False, non_interactive: dict | None = None) -> config.Config:
    config.ensure_home()
    cfg = config.load_config()
    if non_interactive is not None:
        for k, v in non_interactive.items():
            if v is not None:
                cfg.set_path(k, v)
        config.save_config(cfg)
        _finish()
        return cfg
    print("╭─ Hermes setup ─────────────────────────────────────╮")
    print(f"│ home: {str(config.hermes_home()):45}│")
    print("╰────────────────────────────────────────────────────╯")
    if portal:
        portal_flow(cfg)
    else:
        choose_provider(cfg)
    print("\nTerminal backend - where the agent runs commands:")
    print("  local (this machine) · docker (isolated container) · ssh (remote host) · daytona / modal (cloud sandboxes)")
    backend = _ask("Backend", cfg.get_path("terminal.backend"))
    cfg.set_path("terminal.backend", backend)
    if backend == "ssh":
        cfg.set_path("terminal.ssh_host", _ask("SSH host", cfg.get_path("terminal.ssh_host")))
        cfg.set_path("terminal.ssh_user", _ask("SSH user", cfg.get_path("terminal.ssh_user")))
    elif backend == "docker":
        cfg.set_path("terminal.docker_image", _ask("Docker image", cfg.get_path("terminal.docker_image")))
    print("\nApprovals for risky commands: manual (ask me) · smart (allowlist, then ask) · off (never ask - dangerous)")
    cfg.set_path("approvals.mode", _ask("Approval mode", cfg.get_path("approvals.mode")))
    if _yes("\nConfigure optional tool keys (search, images, voice)?"):
        for name, desc in OPTIONAL_KEYS:
            print(f"- {desc}")
            _secret(name)
    soul = config.path("SOUL.md")
    if not soul.exists():
        from hermes.context.prompt_builder import DEFAULT_SOUL

        soul.write_text(DEFAULT_SOUL + "\n")
        print(f"\nCreated {soul} - edit it to change the agent's personality.")
    config.save_config(cfg)
    if _yes("\nSet up a messaging platform now (Telegram, Discord, Slack...)?"):
        from hermes.cli_gateway import gateway_setup

        gateway_setup(cfg)
    _finish()
    return cfg


def _finish() -> None:
    from hermes.skills.manager import SkillManager

    copied = SkillManager().sync_bundled()
    if copied:
        print(f"Installed {len(copied)} bundled skills.")
    print(f"\n✓ Config saved to {config.path('config.yaml')}")
    print("Next: `hermes doctor` to verify, then `hermes` to start chatting.")
