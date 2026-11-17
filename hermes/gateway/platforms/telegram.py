"""Telegram (#42) - Bot API long polling, stdlib only.

Env: TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USERS (ids or @usernames), TELEGRAM_HOME_CHANNEL
"""

from __future__ import annotations

import json
import logging
import os

from hermes import config
from hermes.gateway.base import MessageEvent, PlatformAdapter
from hermes.providers import http

log = logging.getLogger("hermes.telegram")


class TelegramAdapter(PlatformAdapter):
    name = "telegram"
    max_len = 4000
    env_required = ("TELEGRAM_BOT_TOKEN",)

    def __init__(self, cfg=None, on_message=None):
        super().__init__(cfg, on_message)
        self.token = self.env("TELEGRAM_BOT_TOKEN")
        self.api = f"https://api.telegram.org/bot{self.token}"
        self.username = ""

    def _call(self, method: str, payload: dict | None = None, timeout: float = 60):
        data = http.post_json(f"{self.api}/{method}", payload or {}, timeout=timeout)
        if not data.get("ok"):
            raise RuntimeError(f"telegram {method}: {data.get('description')}")
        return data["result"]

    def _download(self, file_id: str, kind: str) -> str:
        info = self._call("getFile", {"file_id": file_id})
        fp = info["file_path"]
        _, _, body = http.request("GET", f"https://api.telegram.org/file/bot{self.token}/{fp}", timeout=120)
        dest = config.path("cache", kind, os.path.basename(fp))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        return str(dest)

    def start(self) -> None:
        me = self._call("getMe")
        self.username = (me.get("username") or "").lower()
        log.info("telegram connected as @%s", self.username)
        offset = 0
        while not self.stop_event.is_set():
            try:
                updates = self._call("getUpdates", {"offset": offset, "timeout": 30, "allowed_updates": ["message"]}, timeout=45)
            except Exception as exc:
                log.warning("getUpdates failed: %s", exc)
                self.stop_event.wait(5)
                continue
            for upd in updates:
                offset = upd["update_id"] + 1
                msg = upd.get("message")
                if msg:
                    try:
                        self._handle(msg)
                    except Exception:
                        log.exception("failed to handle telegram message")

    def _handle(self, msg: dict) -> None:
        chat = msg["chat"]
        user = msg.get("from") or {}
        text = msg.get("text") or msg.get("caption") or ""
        attachments, media_type = [], ""
        if msg.get("voice") or msg.get("audio"):
            v = msg.get("voice") or msg.get("audio")
            attachments.append(self._download(v["file_id"], "audio"))
            media_type = "voice"
        elif msg.get("photo"):
            attachments.append(self._download(msg["photo"][-1]["file_id"], "images"))
            media_type = "image"
        elif msg.get("document"):
            attachments.append(self._download(msg["document"]["file_id"], "documents"))
            media_type = "document"
        is_group = chat.get("type") in ("group", "supergroup")
        mentioned = not is_group or (self.username and f"@{self.username}" in text.lower()) or (
            (msg.get("reply_to_message") or {}).get("from", {}).get("username", "").lower() == self.username)
        if self.username:
            text = text.replace(f"@{self.username}", "").replace(f"@{self.username.capitalize()}", "").strip()
        self.emit(MessageEvent(
            platform="telegram", chat_id=str(chat["id"]), user_id=str(user.get("id", "")),
            user_name=user.get("username") or user.get("first_name", ""), text=text,
            thread_id=str(msg.get("message_thread_id") or ""), message_id=str(msg.get("message_id")),
            is_group=is_group, mentioned=bool(mentioned), attachments=attachments, media_type=media_type, raw=msg,
        ))

    def send(self, chat_id, text, thread_id="", reply_to=""):
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": True}
        if thread_id:
            payload["message_thread_id"] = int(thread_id)
        try:
            self._call("sendMessage", payload)
        except (RuntimeError, http.HTTPError):
            payload.pop("parse_mode")  # unbalanced markdown - retry as plain text
            self._call("sendMessage", payload)

    def send_file(self, chat_id, path, caption="", thread_id=""):
        ext = os.path.splitext(path)[1].lower()
        method, field = ("sendPhoto", "photo") if ext in (".png", ".jpg", ".jpeg", ".webp") else \
            ("sendVoice", "voice") if ext in (".ogg", ".oga", ".opus") else \
            ("sendAudio", "audio") if ext in (".mp3", ".m4a", ".wav") else ("sendDocument", "document")
        fields = {"chat_id": chat_id}
        if caption:
            fields["caption"] = caption[:1000]
        if thread_id:
            fields["message_thread_id"] = thread_id
        with open(path, "rb") as fh:
            body, ctype = http.multipart(fields, {field: (os.path.basename(path), fh.read(), http.guess_type(path))})
        _, _, resp = http.request("POST", f"{self.api}/{method}", headers={"Content-Type": ctype}, data=body, timeout=120)
        if not json.loads(resp).get("ok"):
            raise RuntimeError(f"telegram {method} failed")

    def send_typing(self, chat_id, thread_id=""):
        payload = {"chat_id": chat_id, "action": "typing"}
        if thread_id:
            payload["message_thread_id"] = int(thread_id)
        self._call("sendChatAction", payload, timeout=10)

