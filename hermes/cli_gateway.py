"""`hermes gateway setup|install|run|status` (#41 gateway setup, #74 VPS deployment)."""

from __future__ import annotations

import getpass
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from hermes import config
from hermes.gateway.platforms import ADAPTERS, PLATFORM_ENV

HINTS = {
    "telegram": "Create a bot with @BotFather (/newbot). Find your user id with @userinfobot.",
    "discord": "discord.com/developers -> New Application -> Bot -> enable MESSAGE CONTENT intent; pip install discord.py",
    "slack": "api.slack.com/apps -> enable Socket Mode (xapp- token) + bot scopes chat:write, app_mentions:read, im:history; pip install slack-bolt",
    "whatsapp": "Meta for Developers -> WhatsApp Cloud API. Webhook URL: https://<host>/whatsapp (gateway.webhook_port, default 8650)",
    "signal": "Run bbernhard/signal-cli-rest-api and link your number; SIGNAL_API_URL=http://localhost:8080",
    "email": "Use an app password. Example: imap.gmail.com / smtp.gmail.com",
    "teams": "Azure Bot -> messaging endpoint https://<host>/teams/<TEAMS_WEBHOOK_SECRET>/api/messages",
    "google_chat": "Chat API -> App -> HTTP endpoint https://<host>/google_chat/<GOOGLE_CHAT_SECRET>",
    "webhook": "POST JSON to https://<host>/webhook/<WEBHOOK_SECRET>",
}


def gateway_setup(cfg=None) -> None:
    names = list(ADAPTERS)
    print("Messaging platforms:")
    for i, n in enumerate(names, 1):
        mark = "✓" if ADAPTERS[n].configured() else " "
        print(f"  {i}. [{mark}] {n}")
    choice = input("Platform number/name to configure (Enter to finish): ").strip()
    while choice:
        name = names[int(choice) - 1] if choice.isdigit() and 1 <= int(choice) <= len(names) else choice
        if name not in ADAPTERS:
            print("unknown platform")
        else:
            print(f"\n{name}: {HINTS.get(name, '')}")
            for var in PLATFORM_ENV[name]:
                secret = config.is_secret_name(var)
                current = os.environ.get(var, "")
                prompt = f"  {var}" + (" (set)" if current else "") + ": "
                val = getpass.getpass(prompt) if secret else input(prompt)
                if val.strip():
                    config.set_env_value(var, val.strip())
            print(f"  ✓ {name} configured. Start with `hermes gateway run`.")
        choice = input("\nAnother platform (Enter to finish): ").strip()


def systemd_unit() -> str:
    exe = shutil.which("hermes") or f"{sys.executable} -m hermes"
    return f"""[Unit]
Description=Hermes Agent gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={exe} gateway run
Restart=always
RestartSec=10
Environment=HERMES_HOME={config.hermes_home()}
WorkingDirectory={Path.home()}
NoNewPrivileges=true

[Install]
WantedBy=default.target
"""


def launchd_plist() -> str:
    exe = shutil.which("hermes") or sys.executable
    args = [exe, "gateway", "run"] if exe.endswith("hermes") else [exe, "-m", "hermes", "gateway", "run"]
    arg_xml = "".join(f"<string>{a}</string>" for a in args)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>ai.hermes.gateway</string>
  <key>ProgramArguments</key><array>{arg_xml}</array>
  <key>EnvironmentVariables</key><dict><key>HERMES_HOME</key><string>{config.hermes_home()}</string></dict>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{config.path('logs', 'gateway.out.log')}</string>
  <key>StandardErrorPath</key><string>{config.path('logs', 'gateway.err.log')}</string>
</dict></plist>
"""


def install_service(dry_run: bool = False) -> str:
    if platform.system() == "Darwin":
        path = Path.home() / "Library/LaunchAgents/ai.hermes.gateway.plist"
        content, cmds = launchd_plist(), [["launchctl", "load", "-w", str(path)]]
    else:
        path = Path.home() / ".config/systemd/user/hermes-gateway.service"
        content = systemd_unit()
        cmds = [["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now", "hermes-gateway"]]
    if dry_run:
        return f"# would write {path}\n{content}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    for c in cmds:
        if shutil.which(c[0]):
            subprocess.run(c, check=False)
    hint = "\nTip (VPS): run `loginctl enable-linger $USER` so the service survives logout." if platform.system() != "Darwin" else ""
    return f"Installed {path}{hint}"
