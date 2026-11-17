"""Delivery routing (#50): send text to origin, a home channel or an explicit chat.

Targets::

    origin                  the chat a job/tool call came from
    local                   don't send anywhere (output is still saved)
    telegram                that platform's home channel
    telegram:123456         explicit chat id (discord:<channel>, slack:<channel>, email:<addr>, ...)
    telegram:123456:789     with a thread/topic id
"""

from __future__ import annotations

import logging
import os

from hermes.gateway.base import PlatformAdapter, chunk_text

log = logging.getLogger("hermes.delivery")

RUNNING: dict[str, PlatformAdapter] = {}  # filled by a live GatewayRunner


def adapter_classes() -> dict[str, type[PlatformAdapter]]:
    from hermes.gateway.platforms import ADAPTERS

    return ADAPTERS


def get_adapter(platform: str, cfg: dict) -> PlatformAdapter:
    if platform in RUNNING:
        return RUNNING[platform]
    classes = adapter_classes()
    if platform not in classes:
        raise ValueError(f"unknown platform '{platform}' (choose from {', '.join(classes)})")
    pcfg = ((cfg.get("gateway", {}) or {}).get("platforms", {}) or {}).get(platform, {}) or {}
    return classes[platform](pcfg, None)


def home_channel(platform: str, cfg: dict) -> str:
    homes = (cfg.get("gateway", {}) or {}).get("home_channels", {}) or {}
    return str(homes.get(platform) or os.environ.get(f"{platform.upper()}_HOME_CHANNEL", ""))


def resolve(target: str, origin: dict | None, cfg: dict) -> tuple[str, str, str]:
    """Return (platform, chat_id, thread_id); platform '' means local."""
    origin = origin or {}
    target = (target or "origin").strip()
    if target == "local":
        return "", "", ""
    if target == "origin":
        plat = origin.get("platform") or ""
        if plat in ("", "cli", "cron", "api", "acp", "subagent", "kanban", "local"):
            return "", "", ""
        return plat, str(origin.get("chat_id") or home_channel(plat, cfg)), str(origin.get("thread_id") or "")
    parts = target.split(":", 2)
    plat = parts[0]
    if plat == "email" and len(parts) >= 2:
        return "email", target.split(":", 1)[1], ""
    chat = parts[1] if len(parts) > 1 and parts[1] else home_channel(plat, cfg)
    if not chat:
        raise ValueError(f"no home channel for {plat}; set one with /sethome in that chat or {plat.upper()}_HOME_CHANNEL")
    return plat, chat, parts[2] if len(parts) > 2 else ""


def deliver(target: str, text: str, origin: dict | None, cfg: dict, files: list[str] | None = None) -> dict:
    plat, chat, thread = resolve(target, origin, cfg)
    if not plat:
        return {"delivered": False, "target": "local", "note": "saved locally only"}
    adapter = get_adapter(plat, cfg)
    for chunk in chunk_text(text, adapter.max_len):
        adapter.send(chat, chunk, thread)
    for f in files or []:
        adapter.send_file(chat, f, "", thread)
    return {"delivered": True, "platform": plat, "chat_id": chat}


def list_targets(cfg: dict) -> list[dict]:
    out = []
    for name, cls in adapter_classes().items():
        pcfg = ((cfg.get("gateway", {}) or {}).get("platforms", {}) or {}).get(name, {}) or {}
        if cls.configured(pcfg):
            out.append({"platform": name, "home_channel": home_channel(name, cfg) or None, "running": name in RUNNING})
    return out
