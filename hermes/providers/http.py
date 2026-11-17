"""Tiny stdlib HTTP helper (honours HTTPS_PROXY / CA env through urllib)."""

from __future__ import annotations

import json
import ssl
import os
import urllib.error
import urllib.request
from typing import Any

USER_AGENT = "hermes-agent-replica/0.1"


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        bundle = os.environ.get(var)
        if bundle and os.path.exists(bundle):
            try:
                ctx.load_verify_locations(bundle)
            except (OSError, ssl.SSLError):
                pass
            break
    return ctx


class HTTPError(Exception):
    def __init__(self, status: int, body: str, headers: dict | None = None):
        super().__init__(f"HTTP {status}: {body[:500]}")
        self.status = status
        self.body = body
        self.headers = headers or {}


def request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: Any = None,
    data: bytes | None = None,
    timeout: float = 60,
) -> tuple[int, dict, bytes]:
    hdrs = {"User-Agent": USER_AGENT}
    hdrs.update(headers or {})
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context() if url.startswith("https") else None) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace") if exc.fp else ""
        raise HTTPError(exc.code, body, dict(exc.headers or {})) from None


def get_json(url: str, headers: dict | None = None, timeout: float = 30) -> Any:
    _, _, body = request("GET", url, headers=headers, timeout=timeout)
    return json.loads(body.decode("utf-8") or "null")


def post_json(url: str, payload: Any, headers: dict | None = None, timeout: float = 120) -> Any:
    _, _, body = request("POST", url, headers=headers, json_body=payload, timeout=timeout)
    text = body.decode("utf-8")
    return json.loads(text) if text.strip() else {}


def get_text(url: str, headers: dict | None = None, timeout: float = 30) -> tuple[str, str]:
    """Return (content_type, text)."""
    _, hdrs, body = request("GET", url, headers=headers, timeout=timeout)
    ctype = next((v for k, v in hdrs.items() if k.lower() == "content-type"), "")
    charset = "utf-8"
    if "charset=" in ctype:
        charset = ctype.split("charset=")[-1].split(";")[0].strip() or "utf-8"
    return ctype, body.decode(charset, "replace")


def multipart(fields: dict[str, str], files: dict[str, tuple[str, bytes, str]]) -> tuple[bytes, str]:
    """Encode multipart/form-data. files: field -> (filename, data, content_type)."""
    import uuid

    boundary = uuid.uuid4().hex
    out = []
    for k, v in fields.items():
        out.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    for k, (fname, data, ctype) in files.items():
        out.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fname}\"\r\n"
                   f"Content-Type: {ctype}\r\n\r\n".encode() + data + b"\r\n")
    out.append(f"--{boundary}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


def guess_type(path: str) -> str:
    import mimetypes

    return mimetypes.guess_type(path)[0] or "application/octet-stream"
