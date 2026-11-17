"""Generic webhook platform - trigger the agent from any service (GitHub, IFTTT, Zapier,
n8n, monitoring alerts). POST JSON to ``/webhook/<WEBHOOK_SECRET>``::

    {"text": "...", "chat_id": "alerts", "user_id": "github", "callback_url": "https://..."}

The final answer is POSTed to ``callback_url`` (or ``WEBHOOK_CALLBACK_URL``) as
``{"chat_id": ..., "text": ...}``.
"""

from __future__ import annotations

import hmac
import json

from hermes.gateway.base import MessageEvent, PlatformAdapter
from hermes.providers import http


class WebhookAdapter(PlatformAdapter):
    name = "webhook"
    max_len = 100_000
    uses_webhook = True
    env_required = ("WEBHOOK_SECRET",)

    def __init__(self, cfg=None, on_message=None):
        super().__init__(cfg, on_message)
        self.secret = self.env("WEBHOOK_SECRET")
        self.default_callback = self.env("WEBHOOK_CALLBACK_URL")
        self.callbacks: dict[str, str] = {}

    def allowed_users(self) -> set[str]:
        return {"*"} | super().allowed_users()

    def start(self) -> None:
        self.stop_event.wait()

    def handle_http(self, path, headers, body, method="POST"):
        parts = path.strip("/").split("/")
        if not self.secret or not parts or not hmac.compare_digest(parts[0], self.secret):
            return 403, "forbidden"
        data = json.loads(body or b"{}")
        chat = str(data.get("chat_id") or "webhook")
        if data.get("callback_url"):
            self.callbacks[chat] = data["callback_url"]
        self.emit(MessageEvent(platform="webhook", chat_id=chat, user_id="*", text=str(data.get("text") or json.dumps(data)[:20000])))
        return 202, {"accepted": True, "chat_id": chat}

    def send(self, chat_id, text, thread_id="", reply_to=""):
        url = self.callbacks.get(chat_id) or self.default_callback
        if url:
            http.post_json(url, {"chat_id": chat_id, "text": text})
