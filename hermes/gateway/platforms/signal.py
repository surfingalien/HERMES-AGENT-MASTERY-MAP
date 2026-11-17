"""Signal (#46) through signal-cli-rest-api (https://github.com/bbernhard/signal-cli-rest-api).

Run it with ``docker run -p 8080:8080 -e MODE=json-rpc bbernhard/signal-cli-rest-api``
(or MODE=normal for polling) and link your number.

Env: SIGNAL_API_URL (http://localhost:8080), SIGNAL_NUMBER (+15551234567),
     SIGNAL_ALLOWED_USERS (numbers or UUIDs), SIGNAL_HOME_CHANNEL
"""

from __future__ import annotations

import base64
import logging
import os

from hermes import config
from hermes.gateway.base import MessageEvent, PlatformAdapter
from hermes.providers import http

log = logging.getLogger("hermes.signal")


class SignalAdapter(PlatformAdapter):
    name = "signal"
    max_len = 4000
    env_required = ("SIGNAL_API_URL", "SIGNAL_NUMBER")

    def __init__(self, cfg=None, on_message=None):
        super().__init__(cfg, on_message)
        self.url = self.env("SIGNAL_API_URL").rstrip("/")
        self.number = self.env("SIGNAL_NUMBER")

    def start(self) -> None:
        while not self.stop_event.is_set():
            try:
                envelopes = http.get_json(f"{self.url}/v1/receive/{self.number}?timeout=10", timeout=30) or []
            except Exception as exc:
                log.warning("signal receive failed: %s", exc)
                self.stop_event.wait(5)
                continue
            for item in envelopes:
                try:
                    self._handle(item.get("envelope", item))
                except Exception:
                    log.exception("failed to handle signal message")
            self.stop_event.wait(1)

    def _handle(self, env: dict) -> None:
        dm = env.get("dataMessage") or {}
        if not dm:
            return
        sender = env.get("sourceNumber") or env.get("source") or env.get("sourceUuid", "")
        group = (dm.get("groupInfo") or {}).get("groupId")
        attachments, media_type = [], ""
        for att in dm.get("attachments") or []:
            _, _, data = http.request("GET", f"{self.url}/v1/attachments/{att['id']}", timeout=120)
            kind = "audio" if att.get("contentType", "").startswith("audio") else "images" if att.get("contentType", "").startswith("image") else "documents"
            dest = config.path("cache", kind, att.get("filename") or att["id"])
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            attachments.append(str(dest))
            media_type = "voice" if kind == "audio" else media_type
        text = dm.get("message") or ""
        mentioned = not group or any(m.get("number") == self.number for m in dm.get("mentions") or [])
        self.emit(MessageEvent(platform="signal", chat_id=f"group.{group}" if group else sender, user_id=sender,
                               user_name=env.get("sourceName", ""), text=text, is_group=bool(group), mentioned=mentioned,
                               attachments=attachments, media_type=media_type, raw=env))

    def _recipient(self, chat_id: str) -> str:
        return chat_id

    def send(self, chat_id, text, thread_id="", reply_to=""):
        http.post_json(f"{self.url}/v2/send", {"message": text, "number": self.number, "recipients": [self._recipient(chat_id)]})

    def send_file(self, chat_id, path, caption="", thread_id=""):
        with open(path, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode()
        mime = http.guess_type(path)
        http.post_json(f"{self.url}/v2/send", {"message": caption, "number": self.number, "recipients": [chat_id],
                                               "base64_attachments": [f"data:{mime};filename={os.path.basename(path)};base64,{b64}"]})
