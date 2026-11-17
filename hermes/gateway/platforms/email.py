"""Email (#47) - IMAP polling in, SMTP out (stdlib). Each sender is a conversation.

Env: EMAIL_ADDRESS, EMAIL_PASSWORD (app password), EMAIL_IMAP_HOST, EMAIL_SMTP_HOST,
     EMAIL_SMTP_PORT (587), EMAIL_ALLOWED_USERS (sender addresses), EMAIL_POLL_SECONDS (60)
"""

from __future__ import annotations

import email
import email.utils
import imaplib
import logging
import mimetypes
import os
import re
import smtplib
import ssl
from email.message import EmailMessage

from hermes import config
from hermes.gateway.base import MessageEvent, PlatformAdapter

log = logging.getLogger("hermes.email")


def _plain_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition", "")):
                return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                from hermes.tools.web import html_to_text

                return html_to_text(part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace"))[1]
        return ""
    payload = msg.get_payload(decode=True) or b""
    return payload.decode(msg.get_content_charset() or "utf-8", "replace")


def _strip_quoted(text: str) -> str:
    lines = []
    for line in text.splitlines():
        if line.startswith(">") or re.match(r"^On .+wrote:$", line.strip()):
            break
        lines.append(line)
    return "\n".join(lines).strip()


class EmailAdapter(PlatformAdapter):
    name = "email"
    max_len = 100_000
    env_required = ("EMAIL_ADDRESS", "EMAIL_PASSWORD", "EMAIL_IMAP_HOST", "EMAIL_SMTP_HOST")

    def __init__(self, cfg=None, on_message=None):
        super().__init__(cfg, on_message)
        self.address = self.env("EMAIL_ADDRESS")
        self.password = self.env("EMAIL_PASSWORD")
        self.imap_host = self.env("EMAIL_IMAP_HOST")
        self.smtp_host = self.env("EMAIL_SMTP_HOST")
        self.smtp_port = int(self.env("EMAIL_SMTP_PORT", "587"))
        self.poll = int(self.env("EMAIL_POLL_SECONDS", "60"))
        self.threads: dict[str, dict] = {}  # sender -> {subject, message_id}

    def start(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._poll_once()
            except Exception as exc:
                log.warning("imap poll failed: %s", exc)
            self.stop_event.wait(self.poll)

    def _poll_once(self) -> None:
        with imaplib.IMAP4_SSL(self.imap_host, ssl_context=ssl.create_default_context()) as imap:
            imap.login(self.address, self.password)
            imap.select("INBOX")
            _, data = imap.search(None, "UNSEEN")
            for num in data[0].split():
                _, msg_data = imap.fetch(num, "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1])
                sender = email.utils.parseaddr(msg.get("From", ""))[1].lower()
                if not sender or sender == self.address.lower():
                    continue
                attachments = []
                for part in msg.walk():
                    fname = part.get_filename()
                    if fname and part.get_payload(decode=True):
                        dest = config.path("cache", "documents", os.path.basename(fname))
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(part.get_payload(decode=True))
                        attachments.append(str(dest))
                subject = msg.get("Subject", "")
                self.threads[sender] = {"subject": subject, "message_id": msg.get("Message-ID", "")}
                body = _strip_quoted(_plain_body(msg))
                self.emit(MessageEvent(platform="email", chat_id=sender, user_id=sender,
                                       user_name=email.utils.parseaddr(msg.get("From", ""))[0],
                                       text=f"Subject: {subject}\n\n{body}".strip(), message_id=msg.get("Message-ID", ""),
                                       attachments=attachments, raw=None))

    def _send(self, to: str, body: str, attachment: str | None = None) -> None:
        thread = self.threads.get(to.lower(), {})
        msg = EmailMessage()
        msg["From"] = self.address
        msg["To"] = to
        subj = thread.get("subject") or "Message from Hermes"
        msg["Subject"] = subj if subj.lower().startswith("re:") or not thread else f"Re: {subj}"
        if thread.get("message_id"):
            msg["In-Reply-To"] = thread["message_id"]
            msg["References"] = thread["message_id"]
        msg.set_content(body)
        if attachment:
            ctype = mimetypes.guess_type(attachment)[0] or "application/octet-stream"
            maintype, subtype = ctype.split("/", 1)
            with open(attachment, "rb") as fh:
                msg.add_attachment(fh.read(), maintype=maintype, subtype=subtype, filename=os.path.basename(attachment))
        with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=60) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(self.address, self.password)
            smtp.send_message(msg)

    def send(self, chat_id, text, thread_id="", reply_to=""):
        self._send(chat_id, text)

    def send_file(self, chat_id, path, caption="", thread_id=""):
        self._send(chat_id, caption or "Attached.", path)
