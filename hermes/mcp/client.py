"""MCP client (#66 MCP over stdio, #67 MCP over HTTP, #68 server tool filtering, #69 external APIs).

config.yaml::

    mcp_servers:
      github:
        command: npx
        args: ["-y", "@modelcontextprotocol/server-github"]
        env: {GITHUB_PERSONAL_ACCESS_TOKEN: "${GITHUB_TOKEN}"}
        tools: {include: [create_issue, search_repositories]}
      docs:
        url: https://mcp.example.com/mcp
        headers: {Authorization: "Bearer ${DOCS_TOKEN}"}
        tools: {exclude: [delete_page]}

Each remote tool is registered as ``mcp_<server>_<tool>`` in toolset ``mcp-<server>``.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import re
import subprocess
import threading
from typing import Any

from hermes.providers import http
from hermes.tools.registry import Tool, ToolContext, ToolError, register, unregister

log = logging.getLogger("hermes.mcp")
PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "hermes-agent-replica", "version": "0.1.0"}


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


class MCPError(Exception):
    pass


class StdioTransport:
    def __init__(self, command: str, args: list[str], env: dict[str, str] | None = None, cwd: str | None = None):
        full_env = dict(os.environ)
        full_env.update(env or {})
        self.proc = subprocess.Popen([command, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, bufsize=1, env=full_env, cwd=cwd)
        self.lock = threading.Lock()
        self.pending: dict[int, dict] = {}
        self.events: dict[int, threading.Event] = {}
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stderr(self) -> None:
        for line in self.proc.stderr:  # type: ignore[union-attr]
            log.debug("mcp stderr: %s", line.rstrip())

    def _read(self) -> None:
        for line in self.proc.stdout:  # type: ignore[union-attr]
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            mid = msg.get("id")
            if mid is not None and mid in self.events and ("result" in msg or "error" in msg):
                self.pending[mid] = msg
                self.events[mid].set()
        for ev in self.events.values():
            ev.set()

    def request(self, payload: dict, timeout: float = 60) -> dict:
        mid = payload["id"]
        ev = threading.Event()
        self.events[mid] = ev
        with self.lock:
            self.proc.stdin.write(json.dumps(payload) + "\n")  # type: ignore[union-attr]
            self.proc.stdin.flush()  # type: ignore[union-attr]
        if not ev.wait(timeout):
            raise MCPError(f"timeout waiting for {payload.get('method')}")
        self.events.pop(mid, None)
        msg = self.pending.pop(mid, None)
        if msg is None:
            raise MCPError("server exited")
        return msg

    def notify(self, payload: dict) -> None:
        with self.lock:
            self.proc.stdin.write(json.dumps(payload) + "\n")  # type: ignore[union-attr]
            self.proc.stdin.flush()  # type: ignore[union-attr]

    def close(self) -> None:
        try:
            self.proc.stdin.close()  # type: ignore[union-attr]
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
        for stream in (self.proc.stdout, self.proc.stderr):
            try:
                stream.close()  # type: ignore[union-attr]
            except Exception:
                pass


class HTTPTransport:
    """Streamable HTTP transport: JSON or SSE responses, Mcp-Session-Id tracking."""

    def __init__(self, url: str, headers: dict[str, str] | None = None):
        self.url = url
        self.headers = headers or {}
        self.session_id = ""

    def _post(self, payload: dict, timeout: float = 60) -> tuple[dict, bytes]:
        hdrs = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
                "MCP-Protocol-Version": PROTOCOL_VERSION, **self.headers}
        if self.session_id:
            hdrs["Mcp-Session-Id"] = self.session_id
        _, resp_headers, body = http.request("POST", self.url, headers=hdrs, json_body=payload, timeout=timeout)
        lower = {k.lower(): v for k, v in resp_headers.items()}
        if lower.get("mcp-session-id"):
            self.session_id = lower["mcp-session-id"]
        return lower, body

    def request(self, payload: dict, timeout: float = 60) -> dict:
        headers, body = self._post(payload, timeout)
        text = body.decode("utf-8", "replace")
        if "text/event-stream" in headers.get("content-type", ""):
            for block in text.split("\n\n"):
                data = "\n".join(line[5:].lstrip() for line in block.splitlines() if line.startswith("data:"))
                if not data:
                    continue
                msg = json.loads(data)
                if msg.get("id") == payload["id"]:
                    return msg
            raise MCPError("no response in event stream")
        return json.loads(text)

    def notify(self, payload: dict) -> None:
        try:
            self._post(payload)
        except http.HTTPError as exc:
            if exc.status not in (202, 204):
                raise

    def close(self) -> None:
        if self.session_id:
            try:
                http.request("DELETE", self.url, headers={**self.headers, "Mcp-Session-Id": self.session_id}, timeout=10)
            except Exception:
                pass


class MCPClient:
    def __init__(self, name: str, spec: dict):
        self.name = name
        self.spec = _expand(spec)
        self.ids = itertools.count(1)
        self.transport: StdioTransport | HTTPTransport | None = None
        self.server_info: dict = {}

    def connect(self) -> None:
        if self.spec.get("url"):
            self.transport = HTTPTransport(self.spec["url"], self.spec.get("headers"))
        elif self.spec.get("command"):
            self.transport = StdioTransport(self.spec["command"], list(self.spec.get("args", [])), self.spec.get("env"), self.spec.get("cwd"))
        else:
            raise MCPError(f"server '{self.name}' needs either command or url")
        res = self.call("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO},
                        timeout=float(self.spec.get("connect_timeout", 60)))
        self.server_info = res.get("serverInfo", {})
        self.transport.notify({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, method: str, params: dict | None = None, timeout: float = 120) -> dict:
        if self.transport is None:
            raise MCPError("not connected")
        payload = {"jsonrpc": "2.0", "id": next(self.ids), "method": method, "params": params or {}}
        msg = self.transport.request(payload, timeout)
        if "error" in msg:
            raise MCPError(f"{method}: {msg['error'].get('message')}")
        return msg.get("result") or {}

    def list_tools(self) -> list[dict]:
        tools, cursor = [], None
        while True:
            res = self.call("tools/list", {"cursor": cursor} if cursor else {})
            tools += res.get("tools", [])
            cursor = res.get("nextCursor")
            if not cursor:
                return tools

    def call_tool(self, name: str, arguments: dict) -> str:
        res = self.call("tools/call", {"name": name, "arguments": arguments}, timeout=float(self.spec.get("timeout", 300)))
        parts = []
        for c in res.get("content", []):
            if c.get("type") == "text":
                parts.append(c.get("text", ""))
            elif c.get("type") == "resource":
                parts.append(json.dumps(c.get("resource"))[:5000])
            else:
                parts.append(f"[{c.get('type')} content]")
        if res.get("structuredContent") and not parts:
            parts.append(json.dumps(res["structuredContent"]))
        text = "\n".join(parts)
        if res.get("isError"):
            raise ToolError(text or "MCP tool error")
        return text

    def close(self) -> None:
        if self.transport:
            self.transport.close()


CLIENTS: dict[str, MCPClient] = {}


def _allowed(tool_name: str, filt: dict) -> bool:
    inc, exc = filt.get("include"), filt.get("exclude") or []
    if inc and tool_name not in inc:
        return False
    return tool_name not in exc


def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", s)[:48]


def register_server(name: str, spec: dict) -> list[str]:
    client = MCPClient(name, spec)
    client.connect()
    CLIENTS[name] = client
    filt = spec.get("tools") or {}
    registered = []
    for t in client.list_tools():
        if not _allowed(t["name"], filt):
            continue
        tool_name = f"mcp_{safe_name(name)}_{safe_name(t['name'])}"

        def handler(args: dict, ctx: ToolContext, _remote=t["name"], _client=client) -> str:
            return _client.call_tool(_remote, args)

        register(Tool(tool_name, f"[MCP:{name}] {t.get('description', '')}"[:1024],
                      t.get("inputSchema") or {"type": "object", "properties": {}}, handler, f"mcp-{name}",
                      source=f"mcp:{name}"), replace=True)
        registered.append(tool_name)
    log.info("MCP server %s: %d tools", name, len(registered))
    return registered


def load_mcp_servers(cfg: dict) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for name, spec in (cfg.get("mcp_servers") or {}).items():
        if not spec or spec.get("enabled") is False:
            continue
        try:
            results[name] = register_server(name, spec)
        except Exception as exc:
            results[name] = f"error: {exc}"
            log.warning("MCP server %s failed: %s", name, exc)
    return results


def shutdown() -> None:
    for name, c in list(CLIENTS.items()):
        c.close()
        CLIENTS.pop(name, None)
    from hermes.tools.registry import REGISTRY

    for tname in [n for n, t in REGISTRY.items() if t.source.startswith("mcp:")]:
        unregister(tname)
