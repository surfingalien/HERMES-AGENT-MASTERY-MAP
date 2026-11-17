"""Google Chat (#49) as an HTTP-endpoint Chat app.

Configure the Chat app's HTTP endpoint as
``https://<host>/google_chat/<GOOGLE_CHAT_SECRET>``. Replies that finish within
~25 s are returned synchronously; longer ones are posted through the space's
incoming webhook (``GOOGLE_CHAT_WEBHOOK_URL``) when configured.

Security note: Google signs requests with a JWT (RS256); the stdlib can't verify
it, so this adapter requires a secret path segment plus the user allowlist.

Env: GOOGLE_CHAT_SECRET, GOOGLE_CHAT_WEBHOOK_URL, GOOGLE_CHAT_ALLOWED_USERS (emails),
     GOOGLE_CHAT_HOME_CHANNEL
"""

from __future__ import annotations

import hmac
import json
import logging
import threading

from hermes.gateway.base import MessageEvent, PlatformAdapter
from hermes.providers import http

log = logging.getLogger("hermes.google_chat")
SYNC_WAIT = 25


class GoogleChatAdapter(PlatformAdapter):
    name = "google_chat"
    max_len = 4000
    uses_webhook = True
    env_required = ("GOOGLE_CHAT_SECRET",)

    def __init__(self, cfg=None, on_message=None):
        super().__init__(cfg, on_message)
        self.secret = self.env("GOOGLE_CHAT_SECRET")
        self.webhook_url = self.env("GOOGLE_CHAT_WEBHOOK_URL")
        self.waiting: dict[str, dict] = {}
        self.lock = threading.Lock()

    def start(self) -> None:
        self.stop_event.wait()

    def handle_http(self, path, headers, body, method="POST"):
        parts = path.strip("/").split("/")
        if not self.secret or not parts or not hmac.compare_digest(parts[0], self.secret):
            return 403, "forbidden"
        ev = json.loads(body or b"{}")
        etype = ev.get("type")
        if etype == "ADDED_TO_SPACE":
            return 200, {"text": "👋 Hi! Mention me with a task. Type /help for commands."}
        if etype != "MESSAGE":
            return 200, {}
        msg = ev.get("message") or {}
        space = (ev.get("space") or {})
        sender = msg.get("sender") or ev.get("user") or {}
        chat_id = space.get("name", "")
        thread = (msg.get("thread") or {}).get("name", "")
        slot = {"event": threading.Event(), "texts": []}
        with self.lock:
            self.waiting[chat_id] = slot
        self.emit(MessageEvent(platform="google_chat", chat_id=chat_id, user_id=(sender.get("email") or sender.get("name", "")).lower(),
                               user_name=sender.get("displayName", ""), text=(msg.get("argumentText") or msg.get("text") or "").strip(),
                               thread_id=thread, is_group=space.get("type") == "ROOM" or space.get("spaceType") == "SPACE",
                               mentioned=True, raw=ev))
        slot["event"].wait(SYNC_WAIT)
        with self.lock:
            self.waiting.pop(chat_id, None)
        if slot["texts"]:
            return 200, {"text": "\n\n".join(slot["texts"])}
        return 200, {"text": "⏳ Working on it - I'll post the result here." if self.webhook_url else "⏳ Working on it…"}

    def send(self, chat_id, text, thread_id="", reply_to=""):
        with self.lock:
            slot = self.waiting.get(chat_id)
        if slot is not None and not slot["event"].is_set():
            slot["texts"].append(text)
            slot["event"].set()
            return
        if not self.webhook_url:
            raise RuntimeError("GOOGLE_CHAT_WEBHOOK_URL is required for asynchronous delivery")
        payload = {"text": text}
        url = self.webhook_url
        if thread_id:
            payload["thread"] = {"name": thread_id}
            url += ("&" if "?" in url else "?") + "messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
        http.post_json(url, payload)
