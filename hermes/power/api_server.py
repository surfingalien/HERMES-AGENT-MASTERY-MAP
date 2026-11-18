"""OpenAI-compatible API server (#96).

    hermes serve --port 8642
    curl localhost:8642/v1/chat/completions -H "Authorization: Bearer $API_SERVER_KEY" \\
         -d '{"model":"hermes-agent","messages":[{"role":"user","content":"hi"}]}'

Any OpenAI-compatible frontend (Open WebUI, LibreChat, ...) can use the full
agent - tools, memory and skills included. Pass ``"session_id"`` (or header
``X-Hermes-Session``) to continue a stored session; otherwise the request's
message history is replayed into a fresh session.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("hermes.api")


def make_server(cfg: dict, host: str | None = None, port: int | None = None, agent_factory=None,
                dashboard=None, gateway=None) -> ThreadingHTTPServer:
    """OpenAI-compatible API; with ``dashboard`` also serves the web UI on every non-/v1 path."""
    acfg = cfg.get("api_server", {}) or {}
    key = os.environ.get(acfg.get("key_env", "API_SERVER_KEY"), "")
    model_name = acfg.get("model_name", "hermes-agent")
    host = host or acfg.get("host", "127.0.0.1")
    port = int(port if port is not None else acfg.get("port", 8642))
    local = host in ("127.0.0.1", "localhost", "::1")
    if not local and not key:
        raise RuntimeError("refusing to listen on a public interface without API_SERVER_KEY")
    if dashboard is not None and not local and not os.environ.get((cfg.get("dashboard", {}) or {}).get("token_env", "HERMES_WEB_TOKEN")):
        raise RuntimeError("refusing to expose the dashboard on a public interface without HERMES_WEB_TOKEN")
    if agent_factory is None:
        from hermes.agent import AIAgent

        agent_factory = AIAgent
    session_locks: dict[str, threading.Lock] = {}

    def is_api(path: str) -> bool:
        p = path.split("?", 1)[0]
        return p.startswith("/v1/") or p == "/health"

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _hook(self) -> bool:
            """Messaging webhooks (/whatsapp, /teams/..., /google_chat/..., /webhook/...) on the shared port."""
            if gateway is None or self.command not in ("GET", "POST"):
                return False
            first = self.path.lstrip("/").split("/", 1)[0].split("?", 1)[0].replace("-", "_")
            if first not in gateway.webhook_adapters():
                return False
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0)) if self.command == "POST" else b""
            status, payload = gateway.dispatch_webhook(self.command, self.path, dict(self.headers), body) or (404, "not found")
            data = json.dumps(payload).encode() if isinstance(payload, (dict, list)) else str(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json" if isinstance(payload, (dict, list)) else "text/plain")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return True

        def _ui(self) -> bool:
            if self._hook():
                return True
            if dashboard is not None and not is_api(self.path):
                dashboard.handle(self)
                return True
            return False

        def do_PUT(self):
            if not self._ui():
                self._json(405, {"error": {"message": "method not allowed"}})

        def do_DELETE(self):
            if not self._ui():
                self._json(405, {"error": {"message": "method not allowed"}})

        def _json(self, status: int, payload: dict) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            if not key:
                return True
            auth = self.headers.get("Authorization", "")
            return hmac.compare_digest(auth, f"Bearer {key}")

        def do_OPTIONS(self):
            if self._ui():
                return
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Hermes-Session")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            if self._ui():
                return
            if self.path == "/health":
                return self._json(200, {"status": "ok"})
            if not self._authorized():
                return self._json(401, {"error": {"message": "invalid API key"}})
            if self.path == "/v1/models":
                return self._json(200, {"object": "list", "data": [{"id": model_name, "object": "model", "owned_by": "hermes"}]})
            self._json(404, {"error": {"message": "not found"}})

        def do_POST(self):
            if self._ui():
                return
            if not self._authorized():
                return self._json(401, {"error": {"message": "invalid API key"}})
            if self.path != "/v1/chat/completions":
                return self._json(404, {"error": {"message": "not found"}})
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except json.JSONDecodeError:
                return self._json(400, {"error": {"message": "invalid JSON"}})
            msgs = req.get("messages") or []
            if not msgs or msgs[-1].get("role") != "user":
                return self._json(400, {"error": {"message": "last message must be from the user"}})
            sid = req.get("session_id") or self.headers.get("X-Hermes-Session") or ""
            lock = session_locks.setdefault(sid or uuid.uuid4().hex, threading.Lock())
            with lock:
                agent = agent_factory(config=cfg, platform="api", session_id=sid or None, resume=bool(sid),
                                      approval_mode="deny-dangerous", ephemeral_system="\n".join(
                                          m["content"] for m in msgs if m.get("role") == "system" and isinstance(m.get("content"), str)))
                if not sid:
                    for m in msgs[:-1]:
                        if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str):
                            agent.messages.append({"role": m["role"], "content": m["content"]})
                last = msgs[-1]["content"]
                if isinstance(last, list):
                    last = "\n".join(p.get("text", "") for p in last if isinstance(p, dict))
                result = agent.run_conversation(last)
                agent.close()
            text = result.get("final_response") or ""
            cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
            usage = {"prompt_tokens": result["usage"].input_tokens, "completion_tokens": result["usage"].output_tokens,
                     "total_tokens": result["usage"].input_tokens + result["usage"].output_tokens}
            if req.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                for i in range(0, max(len(text), 1), 400):
                    chunk = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()), "model": model_name,
                             "choices": [{"index": 0, "delta": {"role": "assistant", "content": text[i:i + 400]} if i == 0 else {"content": text[i:i + 400]}, "finish_reason": None}]}
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                end = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()), "model": model_name,
                       "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                self.wfile.write(f"data: {json.dumps(end)}\n\ndata: [DONE]\n\n".encode())
                self.close_connection = True
                return
            self._json(200, {"id": cid, "object": "chat.completion", "created": int(time.time()), "model": model_name,
                             "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                             "usage": usage, "session_id": result.get("session_id")})

        def log_message(self, fmt, *args):
            log.info("api %s", fmt % args)

    return ThreadingHTTPServer((host, port), Handler)


def serve(cfg: dict, host: str | None = None, port: int | None = None, with_dashboard: bool = True,
          open_browser: bool = False, with_gateway: bool = False) -> None:
    """Run the API (+ dashboard). With ``with_gateway`` the same process also runs the cron
    ticker and any configured messaging platforms - one port, as container hosts expect."""
    if port is None and os.environ.get("PORT"):
        port = int(os.environ["PORT"])  # Render / Fly / Railway / Heroku-style platforms
    workdir = (cfg.get("terminal", {}) or {}).get("cwd") or "."
    try:
        os.makedirs(os.path.expanduser(workdir), exist_ok=True)  # e.g. /data/workspace on a fresh volume
    except OSError:
        log.warning("could not create working directory %s", workdir)
    try:  # first boot on a fresh volume: install the bundled skills (user edits are preserved)
        from hermes.skills.manager import SkillManager

        SkillManager(cfg).sync_bundled()
    except Exception:
        log.exception("could not sync bundled skills")
    dashboard = None
    if with_dashboard:
        from hermes.web.dashboard import Dashboard

        wcfg = cfg.get("dashboard", {}) or {}
        bind = host or (cfg.get("api_server", {}) or {}).get("host", "127.0.0.1")
        dashboard = Dashboard(cfg, allowed_hosts=["127.0.0.1", "localhost", "[::1]", *([bind] if bind not in ("0.0.0.0", "::") else []),
                                                  *wcfg.get("allowed_hosts", [])])
    runner, ticker = None, None
    if with_gateway:
        from hermes.gateway.runner import GatewayRunner

        runner = GatewayRunner(cfg)
        runner.build_adapters()
        if runner.adapters:
            runner.start(block=False, cron=True, own_http=False)
        else:
            runner = None
            from hermes.cron.jobs import CronTicker
            from hermes.gateway.delivery import deliver

            ticker = CronTicker(cfg, lambda t, text, o: deliver(t, text, o, cfg), int((cfg.get("gateway", {}) or {}).get("cron_tick_seconds", 60)))
            ticker.start()
    server = make_server(cfg, host, port, dashboard=dashboard, gateway=runner)
    shown_host = server.server_address[0] if server.server_address[0] not in ("0.0.0.0", "::") else "127.0.0.1"
    port = server.server_address[1]
    print(f"Hermes API:       http://{shown_host}:{port}/v1", flush=True)
    if dashboard is not None:
        url = dashboard.login_url(shown_host, port)
        print(f"Hermes dashboard: {url}\n  (this link signs you in - keep it private)", flush=True)
        if open_browser:
            import webbrowser

            threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    if with_gateway:
        print("Scheduled jobs:   running" + (f"; messaging: {', '.join(runner.adapters)}" if runner else " (no messaging platforms configured)"), flush=True)
    print("Ctrl+C to stop", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if runner is not None:
            runner.stop()
        if ticker is not None:
            ticker.stop()
        if dashboard is not None:
            dashboard.shutdown()
        server.server_close()
