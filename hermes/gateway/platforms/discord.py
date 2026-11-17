"""Discord (#43). Receiving uses discord.py (``pip install discord.py``; enable the
Message Content intent). Sending uses the REST API, so deliveries work without it.

Env: DISCORD_BOT_TOKEN, DISCORD_ALLOWED_USERS, DISCORD_HOME_CHANNEL
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from hermes import config
from hermes.gateway.base import MessageEvent, PlatformAdapter
from hermes.providers import http

log = logging.getLogger("hermes.discord")
API = "https://discord.com/api/v10"


class DiscordAdapter(PlatformAdapter):
    name = "discord"
    max_len = 1900
    env_required = ("DISCORD_BOT_TOKEN",)

    def __init__(self, cfg=None, on_message=None):
        super().__init__(cfg, on_message)
        self.token = self.env("DISCORD_BOT_TOKEN")
        self.client = None
        self.loop = None

    def _headers(self) -> dict:
        return {"Authorization": f"Bot {self.token}"}

    def start(self) -> None:
        try:
            import discord  # type: ignore
        except ImportError:
            log.error("discord.py not installed: pip install discord.py")
            self.stop_event.wait()
            return
        intents = discord.Intents.default()
        intents.message_content = True
        client = discord.Client(intents=intents)
        self.client = client
        adapter = self

        @client.event
        async def on_message(message):
            if message.author == client.user or message.author.bot:
                return
            attachments = []
            for att in message.attachments[:4]:
                kind = "audio" if (att.content_type or "").startswith("audio") else "images" if (att.content_type or "").startswith("image") else "documents"
                dest = config.path("cache", kind, att.filename)
                dest.parent.mkdir(parents=True, exist_ok=True)
                await att.save(dest)
                attachments.append(str(dest))
            is_dm = isinstance(message.channel, discord.DMChannel)
            mentioned = is_dm or client.user in message.mentions
            text = message.content.replace(f"<@{client.user.id}>", "").replace(f"<@!{client.user.id}>", "").strip()
            adapter.emit(MessageEvent(
                platform="discord", chat_id=str(message.channel.id), user_id=str(message.author.id),
                user_name=message.author.name, text=text,
                message_id=str(message.id), is_group=not is_dm, mentioned=mentioned, attachments=attachments,
                media_type="voice" if any("audio" in (a.content_type or "") for a in message.attachments) else "",
            ))

        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(client.start(self.token))
        finally:
            self.loop.close()

    def stop(self) -> None:
        super().stop()
        if self.client and self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self.client.close(), self.loop)

    def send(self, chat_id, text, thread_id="", reply_to=""):
        payload = {"content": text}
        if reply_to:
            payload["message_reference"] = {"message_id": reply_to}
        http.post_json(f"{API}/channels/{thread_id or chat_id}/messages", payload, self._headers())

    def send_file(self, chat_id, path, caption="", thread_id=""):
        with open(path, "rb") as fh:
            body, ctype = http.multipart({"payload_json": json.dumps({"content": caption})},
                                         {"files[0]": (os.path.basename(path), fh.read(), http.guess_type(path))})
        http.request("POST", f"{API}/channels/{thread_id or chat_id}/messages",
                     headers={**self._headers(), "Content-Type": ctype}, data=body, timeout=120)

    def send_typing(self, chat_id, thread_id=""):
        http.request("POST", f"{API}/channels/{thread_id or chat_id}/typing", headers=self._headers(), data=b"", timeout=10)
