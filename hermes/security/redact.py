"""Secret redaction for tool output, logs, memory and transcripts (#83 protect secrets)."""

from __future__ import annotations

import os
import re

_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"sk-(?:proj-|or-v1-)?[A-Za-z0-9_\-]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[abposr]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"xapp-[A-Za-z0-9\-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{30,}\b"),  # telegram bot token
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(api[_-]?key|secret|token|password|passwd)\b(\s*[=:]\s*)([\"']?)[^\s\"']{8,}\3"),
]

_SECRET_ENV_HINT = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASS)$|_KEY_|API_KEY", re.I)


def _env_secrets() -> list[str]:
    vals = []
    for name, value in os.environ.items():
        if value and len(value) >= 12 and _SECRET_ENV_HINT.search(name) and value != "proxy-injected":
            vals.append(value)
    return sorted(set(vals), key=len, reverse=True)


def redact(text: str) -> str:
    if not text or not isinstance(text, str):
        return text
    for value in _env_secrets():
        if value in text:
            text = text.replace(value, "***REDACTED***")
    for pat in _PATTERNS:
        if pat.groups >= 3:
            text = pat.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}***REDACTED***{m.group(3)}", text)
        elif pat.groups == 1:
            text = pat.sub(lambda m: f"{m.group(1)}***REDACTED***", text)
        else:
            text = pat.sub("***REDACTED***", text)
    return text


def contains_secret(text: str) -> bool:
    return bool(text) and redact(text) != text
