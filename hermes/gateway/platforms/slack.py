"""Slack (#44). Receiving uses Socket Mode via slack-bolt (``pip install slack-bolt``);
sending uses the Web API directly.

Env: SLACK_BOT_TOKEN (xoxb-), SLACK_APP_TOKEN (xapp-, for Socket Mode),
     SLACK_ALLOWED_USERS (member ids), SLACK_HOME_CHANNEL
"""

from __future__ import annotations

import logging
import os
import re

from hermes import config
from hermes.gateway.base import MessageEvent, PlatformAdapter
from hermes.providers import http

log = logging.getLogger("hermes.slack")
API = "https://slack.com/api"


class SlackAdapter(PlatformAdapter):
    name = "slack"
    max_len = 3500
    env_required = ("SLACK_BOT_TOKEN",)

    def __init__(self, cfg=None, on_message=None):
        super().__init__(cfg, on_message)
        self.token = self.env("SLACK_BOT_TOKEN")
        self.app_token = self.env("SLACK_APP_TOKEN")
        self.bot_user = ""
        self.handler = None

    def _api(self, method: str, payload: dict) -> dict:
        data = http.post_json(f"{API}/{method}", payload, {"Authorization": f"Bearer {self.token}"})
        if not data.get("ok"):
            raise RuntimeError(f"slack {method}: {data.get('error')}")
        return data

    def _download(self, f: dict) -> str:
        url = f.get("url_private_download") or f.get("url_private")
        _, _, body = http.request("GET", url, headers={"Authorization": f"Bearer {self.token}"}, timeout=120)
        kind = "audio" if f.get("mimetype", "").startswith("audio") else "images" if f.get("mimetype", "").startswith("image") else "documents"
        dest = config.path("cache", kind, f.get("name") or f.get("id"))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        return str(dest)

    def start(self) -> None:
        try:
            from slack_bolt import App  # type: ignore
            from slack_bolt.adapter.socket_mode import SocketModeHandler  # type: ignore
        except ImportError:
            log.error("slack-bolt not installed: pip install slack-bolt")
            self.stop_event.wait()
            return
        if not self.app_token:
            log.error("SLACK_APP_TOKEN (xapp-...) is required for Socket Mode")
            self.stop_event.wait()
            return
        app = App(token=self.token)
        self.bot_user = app.client.auth_test()["user_id"]
        adapter = self

        def handle(event, is_mention):
            if event.get("bot_id") or event.get("subtype") in ("bot_message", "message_changed"):
                return
            text = re.sub(rf"<@{adapter.bot_user}>", "", event.get("text") or "").strip()
            is_dm = event.get("channel_type") == "im"
            files = [adapter._download(f) for f in (event.get("files") or [])[:4]]
            adapter.emit(MessageEvent(
                platform="slack", chat_id=event["channel"], user_id=event.get("user", ""), text=text,
                thread_id=event.get("thread_ts") or ("" if is_dm else event.get("ts", "")), message_id=event.get("ts", ""),
                is_group=not is_dm, mentioned=is_dm or is_mention, attachments=files,
                media_type="voice" if any(f.get("mimetype", "").startswith("audio") for f in event.get("files") or []) else "",
            ))

        @app.event("app_mention")
        def on_mention(event, ack=None):
            handle(event, True)

        @app.event("message")
        def on_message(event, ack=None):
            if f"<@{adapter.bot_user}>" in (event.get("text") or "") and event.get("channel_type") != "im":
                return  # handled by app_mention
            handle(event, False)

        self.handler = SocketModeHandler(app, self.app_token)
        self.handler.connect()
        self.stop_event.wait()
        self.handler.close()

    def send(self, chat_id, text, thread_id="", reply_to=""):
        payload = {"channel": chat_id, "text": text, "mrkdwn": True}
        if thread_id:
            payload["thread_ts"] = thread_id
        self._api("chat.postMessage", payload)

    def send_file(self, chat_id, path, caption="", thread_id=""):
        data = open(path, "rb").read()
        name = os.path.basename(path)
        _, _, body = http.request("GET", f"{API}/files.getUploadURLExternal?filename={name}&length={len(data)}",
                                  headers={"Authorization": f"Bearer {self.token}"})
        import json

        info = json.loads(body)
        if not info.get("ok"):
            raise RuntimeError(f"slack upload: {info.get('error')}")
        http.request("POST", info["upload_url"], data=data, headers={"Content-Type": "application/octet-stream"}, timeout=120)
        payload = {"files": [{"id": info["file_id"], "title": name}], "channel_id": chat_id}
        if caption:
            payload["initial_comment"] = caption
        if thread_id:
            payload["thread_ts"] = thread_id
        self._api("files.completeUploadExternal", payload)
