"""Gateway building block: a PlatformAdapter bridges one messaging service (#41).

Adapters normalise incoming messages to :class:`MessageEvent` and implement
``send``/``send_file``. ``send`` only needs credentials (REST), so delivery works
even when the gateway process is not running (cron via ``hermes cron tick``,
``hermes send``).
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class MessageEvent:
    platform: str
    chat_id: str
    user_id: str
    text: str
    user_name: str = ""
    thread_id: str = ""
    message_id: str = ""
    is_group: bool = False
    mentioned: bool = True
    attachments: list[str] = field(default_factory=list)  # local file paths
    media_type: str = ""  # voice | image | document
    raw: Any = None

    @property
    def session_key(self) -> str:
        return f"{self.platform}:{self.chat_id}" + (f":{self.thread_id}" if self.thread_id else "")


class PlatformAdapter:
    name = "base"
    max_len = 4000
    env_required: tuple[str, ...] = ()

    def __init__(self, cfg: dict | None = None, on_message: Callable[[MessageEvent], None] | None = None):
        self.cfg = cfg or {}
        self.on_message = on_message
        self.stop_event = threading.Event()

    # -- configuration -----------------------------------------------------
    def env(self, key: str, default: str = "") -> str:
        return str(self.cfg.get(key.lower()) or os.environ.get(key, default) or "")

    @classmethod
    def configured(cls, cfg: dict | None = None) -> bool:
        cfg = cfg or {}
        if cfg.get("enabled") is False:
            return False
        return all(cfg.get(e.lower()) or os.environ.get(e) for e in cls.env_required)

    def allowed_users(self) -> set[str]:
        raw = self.env(f"{self.name.upper()}_ALLOWED_USERS")
        users = {u.strip().lower() for u in re.split(r"[,\s]+", raw) if u.strip()}
        users |= {str(u).lower() for u in self.cfg.get("allowed_users", [])}
        return users

    def home_channel(self) -> str:
        return self.env(f"{self.name.upper()}_HOME_CHANNEL")

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        """Blocking receive loop; run in a thread by the GatewayRunner."""

    def stop(self) -> None:
        self.stop_event.set()

    uses_webhook = False

    def handle_http(self, path: str, headers: dict, body: bytes, method: str = "POST") -> tuple[int, dict | str]:
        """For webhook-based platforms; mounted at /<platform>/... (path includes the query string)."""
        return 404, "not found"

    # -- outbound -----------------------------------------------------------
    def send(self, chat_id: str, text: str, thread_id: str = "", reply_to: str = "") -> None:
        raise NotImplementedError

    def send_file(self, chat_id: str, path: str, caption: str = "", thread_id: str = "") -> None:
        self.send(chat_id, f"{caption}\n[file: {path}]".strip(), thread_id)

    def send_typing(self, chat_id: str, thread_id: str = "") -> None:
        pass

    def emit(self, event: MessageEvent) -> None:
        if self.on_message:
            self.on_message(event)


def chunk_text(text: str, limit: int) -> list[str]:
    """Split on paragraph/line boundaries, never inside a code fence when avoidable."""
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for para in re.split(r"(\n\n)", text):
        if len(current) + len(para) <= limit:
            current += para
            continue
        if current.strip():
            chunks.append(current)
        current = ""
        while len(para) > limit:
            cut = para.rfind("\n", 0, limit)
            cut = cut if cut > limit // 2 else limit
            chunks.append(para[:cut])
            para = para[cut:]
        current = para
    if current.strip():
        chunks.append(current)
    # keep code fences balanced across chunks
    fixed, carry = [], False
    for c in chunks:
        if carry:
            c = "```\n" + c
        carry = c.count("```") % 2 == 1
        if carry:
            c += "\n```"
        fixed.append(c)
    return fixed
