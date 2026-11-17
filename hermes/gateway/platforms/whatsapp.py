"""WhatsApp (#45) via the official WhatsApp Business Cloud API.

Inbound: Meta calls ``https://<your-host>:<webhook_port>/whatsapp`` (GET verify, POST
messages, signature-checked with the app secret). Outbound: Graph API.

Env: WHATSAPP_ACCESS_TOKEN, WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_VERIFY_TOKEN,
     WHATSAPP_APP_SECRET, WHATSAPP_ALLOWED_USERS (phone numbers), WHATSAPP_HOME_CHANNEL
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import urllib.parse

from hermes import config
from hermes.gateway.base import MessageEvent, PlatformAdapter
from hermes.providers import http

log = logging.getLogger("hermes.whatsapp")
GRAPH = "https://graph.facebook.com/v21.0"


class WhatsAppAdapter(PlatformAdapter):
    name = "whatsapp"
    max_len = 4000
    uses_webhook = True
    env_required = ("WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID")

    def __init__(self, cfg=None, on_message=None):
        super().__init__(cfg, on_message)
        self.token = self.env("WHATSAPP_ACCESS_TOKEN")
        self.phone_id = self.env("WHATSAPP_PHONE_NUMBER_ID")
        self.verify_token = self.env("WHATSAPP_VERIFY_TOKEN")
        self.app_secret = self.env("WHATSAPP_APP_SECRET")

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def start(self) -> None:
        self.stop_event.wait()  # inbound arrives via the gateway webhook server

    def handle_http(self, path, headers, body, method="POST"):
        if method == "GET":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
            if q.get("hub.mode", [""])[0] == "subscribe" and self.verify_token and q.get("hub.verify_token", [""])[0] == self.verify_token:
                return 200, q.get("hub.challenge", [""])[0]
            return 403, "verification failed"
        if not self.app_secret:
            log.error("WHATSAPP_APP_SECRET not set - refusing unsigned webhook")
            return 403, "signature required"
        sig = {k.lower(): v for k, v in headers.items()}.get("x-hub-signature-256", "")
        expected = "sha256=" + hmac.new(self.app_secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return 403, "bad signature"
        data = json.loads(body or b"{}")
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                names = {c.get("wa_id"): (c.get("profile") or {}).get("name", "") for c in value.get("contacts", [])}
                for msg in value.get("messages", []):
                    try:
                        self._handle(msg, names)
                    except Exception:
                        log.exception("failed to handle whatsapp message")
        return 200, "ok"

    def _download(self, media_id: str, kind: str, ext: str) -> str:
        meta = http.get_json(f"{GRAPH}/{media_id}", self._auth())
        _, _, data = http.request("GET", meta["url"], headers=self._auth(), timeout=120)
        dest = config.path("cache", kind, f"wa-{media_id}{ext}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return str(dest)

    def _handle(self, msg: dict, names: dict) -> None:
        mtype = msg.get("type")
        text, attachments, media_type = "", [], ""
        if mtype == "text":
            text = msg["text"]["body"]
        elif mtype in ("audio", "voice"):
            attachments.append(self._download(msg[mtype]["id"], "audio", ".ogg"))
            media_type = "voice"
        elif mtype == "image":
            attachments.append(self._download(msg["image"]["id"], "images", ".jpg"))
            text = msg["image"].get("caption", "")
            media_type = "image"
        elif mtype == "document":
            attachments.append(self._download(msg["document"]["id"], "documents", ""))
            text = msg["document"].get("caption", "")
        else:
            return
        sender = msg.get("from", "")
        self.emit(MessageEvent(platform="whatsapp", chat_id=sender, user_id=sender, user_name=names.get(sender, ""),
                               text=text, message_id=msg.get("id", ""), attachments=attachments, media_type=media_type, raw=msg))

    def send(self, chat_id, text, thread_id="", reply_to=""):
        http.post_json(f"{GRAPH}/{self.phone_id}/messages",
                       {"messaging_product": "whatsapp", "to": chat_id, "type": "text", "text": {"body": text, "preview_url": False}},
                       self._auth())

    def send_file(self, chat_id, path, caption="", thread_id=""):
        ctype = http.guess_type(path)
        with open(path, "rb") as fh:
            body, mp_type = http.multipart({"messaging_product": "whatsapp", "type": ctype},
                                           {"file": (os.path.basename(path), fh.read(), ctype)})
        _, _, resp = http.request("POST", f"{GRAPH}/{self.phone_id}/media", headers={**self._auth(), "Content-Type": mp_type}, data=body)
        media_id = json.loads(resp)["id"]
        kind = "image" if ctype.startswith("image") else "audio" if ctype.startswith("audio") else "document"
        obj = {"id": media_id}
        if caption and kind != "audio":
            obj["caption"] = caption
        http.post_json(f"{GRAPH}/{self.phone_id}/messages", {"messaging_product": "whatsapp", "to": chat_id, "type": kind, kind: obj}, self._auth())
