"""Microsoft Teams (#48) via the Bot Framework REST API.

Register an Azure Bot, set its messaging endpoint to
``https://<host>/teams/<TEAMS_WEBHOOK_SECRET>/api/messages`` and install it in Teams.

Security note: full Bot Framework JWT validation needs RS256 verification, which
the stdlib lacks; this adapter instead requires a secret path segment plus the
user allowlist / tenant allowlist. Put the gateway behind HTTPS.

Env: TEAMS_APP_ID, TEAMS_APP_PASSWORD, TEAMS_WEBHOOK_SECRET, TEAMS_TENANT_ID (optional,
     single-tenant bots), TEAMS_ALLOWED_TENANTS, TEAMS_ALLOWED_USERS (AAD object ids),
     TEAMS_INCOMING_WEBHOOK_URL (optional, for deliveries to a channel), TEAMS_HOME_CHANNEL
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import threading
import time
import urllib.parse

from hermes import config
from hermes.gateway.base import MessageEvent, PlatformAdapter
from hermes.providers import http

log = logging.getLogger("hermes.teams")


class TeamsAdapter(PlatformAdapter):
    name = "teams"
    max_len = 20000
    uses_webhook = True
    env_required = ("TEAMS_APP_ID", "TEAMS_APP_PASSWORD")

    def __init__(self, cfg=None, on_message=None):
        super().__init__(cfg, on_message)
        self.app_id = self.env("TEAMS_APP_ID")
        self.app_password = self.env("TEAMS_APP_PASSWORD")
        self.secret = self.env("TEAMS_WEBHOOK_SECRET")
        self.tenant = self.env("TEAMS_TENANT_ID") or "botframework.com"
        self.allowed_tenants = {t.strip() for t in self.env("TEAMS_ALLOWED_TENANTS").split(",") if t.strip()}
        self.webhook_url = self.env("TEAMS_INCOMING_WEBHOOK_URL")
        self._token = ("", 0.0)
        self._lock = threading.Lock()
        self.refs_path = config.path("gateway", "teams_conversations.json")
        self.refs = json.loads(self.refs_path.read_text()) if self.refs_path.exists() else {}

    def start(self) -> None:
        self.stop_event.wait()

    def _access_token(self) -> str:
        with self._lock:
            tok, exp = self._token
            if tok and exp > time.time() + 60:
                return tok
            body = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": self.app_id,
                                           "client_secret": self.app_password, "scope": "https://api.botframework.com/.default"}).encode()
            _, _, resp = http.request("POST", f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/token",
                                      headers={"Content-Type": "application/x-www-form-urlencoded"}, data=body)
            data = json.loads(resp)
            self._token = (data["access_token"], time.time() + int(data.get("expires_in", 3600)))
            return self._token[0]

    def handle_http(self, path, headers, body, method="POST"):
        parts = path.strip("/").split("/")
        if not self.secret or not parts or not hmac.compare_digest(parts[0], self.secret):
            return 403, "forbidden"
        if method != "POST":
            return 405, "method not allowed"
        act = json.loads(body or b"{}")
        if act.get("type") != "message":
            return 200, {}
        tenant = ((act.get("channelData") or {}).get("tenant") or {}).get("id", "")
        if self.allowed_tenants and tenant not in self.allowed_tenants:
            return 403, "tenant not allowed"
        conv = act.get("conversation") or {}
        conv_id = conv.get("id", "")
        self.refs[conv_id] = {"serviceUrl": act.get("serviceUrl"), "bot": act.get("recipient"), "tenant": tenant}
        self.refs_path.parent.mkdir(parents=True, exist_ok=True)
        self.refs_path.write_text(json.dumps(self.refs))
        text = re.sub(r"<at>.*?</at>", "", act.get("text") or "").strip()
        user = act.get("from") or {}
        is_group = conv.get("conversationType") in ("groupChat", "channel")
        mentioned = not is_group or any(e.get("type") == "mention" and (e.get("mentioned") or {}).get("id") == (act.get("recipient") or {}).get("id")
                                        for e in act.get("entities") or [])
        self.emit(MessageEvent(platform="teams", chat_id=conv_id, user_id=user.get("aadObjectId") or user.get("id", ""),
                               user_name=user.get("name", ""), text=text, message_id=act.get("id", ""), is_group=is_group,
                               mentioned=mentioned, raw=act))
        return 200, {}

    def _post_activity(self, chat_id: str, activity: dict) -> None:
        ref = self.refs.get(chat_id)
        if not ref:
            if self.webhook_url:
                http.post_json(self.webhook_url, {"text": activity.get("text", "")})
                return
            raise RuntimeError("unknown Teams conversation; the user must message the bot first")
        url = f"{ref['serviceUrl'].rstrip('/')}/v3/conversations/{urllib.parse.quote(chat_id, safe='')}/activities"
        http.post_json(url, {"type": "message", "from": ref.get("bot"), **activity}, {"Authorization": f"Bearer {self._access_token()}"})

    def send(self, chat_id, text, thread_id="", reply_to=""):
        self._post_activity(chat_id, {"text": text, "textFormat": "markdown"})

    def send_typing(self, chat_id, thread_id=""):
        if chat_id in self.refs:
            self._post_activity(chat_id, {"type": "typing"})
