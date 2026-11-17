"""Built-in web dashboard: chat, sessions, memory, skills, cron, insights, settings.

Served by ``hermes dashboard`` (or alongside the API by ``hermes serve``). The UI
is a dependency-free single page in ``hermes/web/static``; this module is its
JSON + Server-Sent-Events backend.

Security model (the dashboard can make the agent run commands):
* binds to 127.0.0.1 by default; any other host requires ``HERMES_WEB_TOKEN``
* every request needs the session cookie, obtained by opening the one-time login
  URL printed at startup (``/?token=...``); the cookie is HttpOnly + SameSite=Strict
* state-changing requests also need the ``X-Hermes: 1`` header (blocks CSRF, since
  browsers can't add custom headers cross-site without a CORS preflight we never allow)
* the Host header must match the bind address (blocks DNS rebinding)
* strict Content-Security-Policy: no inline script, no third-party origins
"""

from __future__ import annotations

import contextlib
import hmac
import io
import itertools
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, Callable

from hermes import __version__
from hermes import config as hconfig
from hermes.security.redact import redact

log = logging.getLogger("hermes.web")

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_FILES = {"index.html": "text/html; charset=utf-8", "app.js": "text/javascript; charset=utf-8",
                "style.css": "text/css; charset=utf-8", "favicon.svg": "image/svg+xml"}
COOKIE = "hermes_session"
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self'; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
SECRET_KEY_RE = re.compile(r"(key|token|secret|password|passwd|authorization)", re.I)
EDITABLE_SETTINGS = {
    "model.provider": str, "model.default": str, "model.base_url": str, "model.reasoning_effort": str,
    "agent.personality": str, "agent.max_turns": int, "agent.verify_outputs": bool,
    "approvals.mode": str, "terminal.backend": str, "terminal.cwd": str,
    "memory.provider": str, "checkpoints.enabled": bool,
}
CHOICES = {
    "approvals.mode": ["manual", "smart", "off"],
    "terminal.backend": ["local", "docker", "ssh", "daytona", "modal"],
    "model.reasoning_effort": ["", "low", "medium", "high", "xhigh", "max"],
    "agent.personality": ["", "helpful", "concise", "technical", "teacher", "creative", "skeptic"],
    "memory.provider": ["builtin", "session_recall"],
}


class HTTPError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def mask_secrets(obj: Any) -> Any:
    """Config view for the browser: secret-looking keys masked, values redacted."""
    if isinstance(obj, dict):
        return {k: ("***" if SECRET_KEY_RE.search(str(k)) and v and not isinstance(v, (dict, list, bool)) else mask_secrets(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [mask_secrets(v) for v in obj]
    if isinstance(obj, str):
        return redact(obj)
    return obj


# ---------------------------------------------------------------- chat runs
class ChatRun:
    """One agent turn, observable as an ordered event stream (SSE)."""

    def __init__(self, session_id: str):
        self.id = uuid.uuid4().hex[:12]
        self.session_id = session_id
        self.events: list[dict] = []
        self.cond = threading.Condition()
        self.done = False
        self.waiting: dict[str, dict] = {}  # prompt_id -> {"event": Event, "answer": ...}
        self._ids = itertools.count(1)
        self._tool_ids: dict[str, list[str]] = {}

    def emit(self, kind: str, **data: Any) -> None:
        with self.cond:
            self.events.append({"seq": len(self.events) + 1, "type": kind, "ts": time.time(), **data})
            if kind in ("final", "error"):
                self.done = True
            self.cond.notify_all()

    def wait_events(self, after: int, timeout: float = 15.0) -> tuple[list[dict], bool]:
        with self.cond:
            if len(self.events) <= after and not self.done:
                self.cond.wait(timeout)
            return self.events[after:], self.done

    def tool_started(self, name: str, args: dict) -> None:
        call_id = f"t{next(self._ids)}"
        self._tool_ids.setdefault(name, []).append(call_id)
        self.emit("tool_start", id=call_id, name=name, args=_trim(args))

    def tool_ended(self, name: str, output: str, is_error: bool, ms: int) -> None:
        pending = self._tool_ids.get(name) or [f"t{next(self._ids)}"]
        self.emit("tool_end", id=pending.pop(0), name=name, output=output[:6000], is_error=is_error, ms=ms)

    def ask(self, kind: str, timeout: float, **data: Any) -> str | None:
        pid = uuid.uuid4().hex[:8]
        box = {"event": threading.Event(), "answer": None, "kind": kind}
        self.waiting[pid] = box
        self.emit(kind, prompt_id=pid, **data)
        box["event"].wait(timeout)
        self.waiting.pop(pid, None)
        self.emit("prompt_resolved", prompt_id=pid, answer=box["answer"] if kind == "approval" else ("answered" if box["answer"] else "timeout"))
        return box["answer"]

    def answer(self, prompt_id: str, value: str) -> bool:
        box = self.waiting.get(prompt_id)
        if not box:
            return False
        box["answer"] = value
        box["event"].set()
        return True


def _trim(args: Any, limit: int = 2000) -> Any:
    text = json.dumps(args, default=str)
    return args if len(text) <= limit else {"_truncated": text[:limit]}


class Dashboard:
    def __init__(self, cfg: dict | None = None, token: str | None = None, agent_factory: Callable | None = None,
                 allowed_hosts: list[str] | None = None):
        self.cfg = cfg if cfg is not None else hconfig.load_config()
        self.token = token or os.environ.get((self.cfg.get("dashboard", {}) or {}).get("token_env", "HERMES_WEB_TOKEN"), "") or secrets.token_urlsafe(24)
        self.session_secret = secrets.token_urlsafe(32)  # cookie value, never shown
        self.allowed_hosts = set(allowed_hosts or ["127.0.0.1", "localhost", "[::1]"])
        if agent_factory is None:
            from hermes.agent import AIAgent

            agent_factory = AIAgent
        self.agent_factory = agent_factory
        self.agents: dict[str, Any] = {}
        self.busy: dict[str, ChatRun] = {}
        self.runs: dict[str, ChatRun] = {}
        self.lock = threading.Lock()
        from hermes.sessions.db import SessionDB

        self.db = SessionDB()

    # ------------------------------------------------------------ auth
    def login_url(self, host: str, port: int) -> str:
        return f"http://{host}:{port}/?token={urllib.parse.quote(self.token)}"

    def check_host(self, host_header: str) -> bool:
        host = (host_header or "").rsplit(":", 1)[0] if not (host_header or "").startswith("[") else host_header.split("]")[0] + "]"
        return host in self.allowed_hosts

    def is_authed(self, headers) -> bool:
        cookie = headers.get("Cookie", "") or ""
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE and hmac.compare_digest(value, self.session_secret):
                return True
        auth = headers.get("Authorization", "") or ""
        return auth.startswith("Bearer ") and hmac.compare_digest(auth[7:], self.token)

    # ------------------------------------------------------------ HTTP entry point
    def handle(self, h) -> None:
        """Serve one request on a BaseHTTPRequestHandler ``h``."""
        parsed = urllib.parse.urlparse(h.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        method = h.command
        try:
            if not self.check_host(h.headers.get("Host", "")):
                raise HTTPError(421, "unexpected Host header")
            if method == "GET" and path == "/" and "token" in query:
                if hmac.compare_digest(query["token"][0], self.token):
                    h.send_response(303)
                    h.send_header("Set-Cookie", f"{COOKIE}={self.session_secret}; HttpOnly; SameSite=Strict; Path=/")
                    h.send_header("Location", "/")
                    h.send_header("Content-Length", "0")
                    h.end_headers()
                    return
                raise HTTPError(403, "invalid login token")
            if method == "GET" and (path in ("/", "/index.html") or path.lstrip("/") in STATIC_FILES):
                return self._static(h, "index.html" if path in ("/", "/index.html") else path.lstrip("/"))
            if not path.startswith("/api/"):
                raise HTTPError(404, "not found")
            if not self.is_authed(h.headers):
                raise HTTPError(401, "not signed in - open the login link printed by `hermes dashboard`")
            if method in ("POST", "PUT", "DELETE") and h.headers.get("X-Hermes") != "1":
                raise HTTPError(403, "missing X-Hermes header")
            body = {}
            if method in ("POST", "PUT"):
                raw = h.rfile.read(int(h.headers.get("Content-Length") or 0))
                if len(raw) > 2_000_000:
                    raise HTTPError(413, "request too large")
                body = json.loads(raw or b"{}")
                if not isinstance(body, dict):
                    raise HTTPError(400, "JSON object expected")
            if method == "GET" and re.fullmatch(r"/api/runs/[a-f0-9]+/events", path):
                return self._sse(h, path.split("/")[3], int((query.get("after") or ["0"])[0]))
            if method == "GET" and path == "/api/media":
                return self._media(h, (query.get("path") or [""])[0])
            result = self.route(method, path, query, body)
            self._json(h, 200, result)
        except HTTPError as exc:
            self._json(h, exc.status, {"error": str(exc)})
        except json.JSONDecodeError:
            self._json(h, 400, {"error": "invalid JSON"})
        except (ValueError, KeyError) as exc:
            self._json(h, 400, {"error": str(exc)})
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("dashboard request failed")
            self._json(h, 500, {"error": f"{type(exc).__name__}: {exc}"})

    def _security_headers(self, h) -> None:
        h.send_header("Content-Security-Policy", CSP)
        h.send_header("X-Content-Type-Options", "nosniff")
        h.send_header("Referrer-Policy", "no-referrer")
        h.send_header("Cache-Control", "no-store")

    def _json(self, h, status: int, payload: Any) -> None:
        data = json.dumps(payload, default=str).encode()
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(data)))
        if status >= 400:
            # The request body may be unread (rejected before parsing); don't let it
            # be mistaken for the next request on a keep-alive connection.
            h.send_header("Connection", "close")
            h.close_connection = True
        self._security_headers(h)
        h.end_headers()
        h.wfile.write(data)

    def _static(self, h, name: str) -> None:
        if name not in STATIC_FILES:
            raise HTTPError(404, "not found")
        data = (STATIC_DIR / name).read_bytes()
        h.send_response(200)
        h.send_header("Content-Type", STATIC_FILES[name])
        h.send_header("Content-Length", str(len(data)))
        self._security_headers(h)
        h.end_headers()
        h.wfile.write(data)

    def _media(self, h, raw_path: str) -> None:
        cache = hconfig.path("cache").resolve()
        target = Path(raw_path).resolve()
        if not str(target).startswith(str(cache) + os.sep) or not target.is_file():
            raise HTTPError(404, "media not found")
        import mimetypes

        data = target.read_bytes()
        h.send_response(200)
        h.send_header("Content-Type", mimetypes.guess_type(str(target))[0] or "application/octet-stream")
        h.send_header("Content-Length", str(len(data)))
        self._security_headers(h)
        h.end_headers()
        h.wfile.write(data)

    def _sse(self, h, run_id: str, after: int) -> None:
        run = self.runs.get(run_id)
        if run is None:
            raise HTTPError(404, "unknown run")
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Cache-Control", "no-store")
        h.send_header("Connection", "close")
        self._security_headers(h)
        h.end_headers()
        h.close_connection = True
        try:
            while True:
                events, done = run.wait_events(after)
                for ev in events:
                    h.wfile.write(f"id: {ev['seq']}\ndata: {json.dumps(ev, default=str)}\n\n".encode())
                    after = ev["seq"]
                if not events:
                    h.wfile.write(b": keepalive\n\n")
                h.wfile.flush()
                if done and after >= len(run.events):
                    h.wfile.write(b"event: end\ndata: {}\n\n")
                    h.wfile.flush()
                    return
        except (BrokenPipeError, ConnectionResetError):
            return

    # ------------------------------------------------------------ routing
    def route(self, method: str, path: str, query: dict, body: dict) -> Any:
        q = {k: v[0] for k, v in query.items()}
        parts = path.strip("/").split("/")[1:]  # drop "api"
        key = (method, parts[0] if parts else "")
        if key == ("GET", "status"):
            return self.status()
        if parts[:1] == ["chat"] and method == "POST":
            return self.start_chat(body)
        if parts[:1] == ["runs"] and len(parts) == 3 and method == "POST":
            return self.run_action(parts[1], parts[2], body)
        if parts[:1] == ["sessions"]:
            return self.sessions(method, parts[1:], q, body)
        if parts[:1] == ["memory"]:
            return self.memory(method, parts[1:], body)
        if parts[:1] == ["skills"]:
            return self.skills(method, parts[1:], body)
        if parts[:1] == ["cron"]:
            return self.cron(method, parts[1:], body)
        if key == ("GET", "insights"):
            return self.db.insights(int(q.get("days") or 30))
        if parts[:1] == ["settings"]:
            return self.settings(method, body)
        if parts[:1] == ["tools"]:
            return self.tools(method, q, body)
        if key == ("GET", "doctor"):
            return self.doctor()
        if key == ("GET", "logs"):
            return self.logs(q.get("kind", "errors"), int(q.get("lines") or 200))
        raise HTTPError(404, f"no route for {method} {path}")

    # ------------------------------------------------------------ status
    def status(self) -> dict:
        from hermes.skills.manager import SkillManager
        from hermes.tools import registry

        registry.load_builtin_tools()
        mcfg = self.cfg.get("model", {}) or {}
        home = hconfig.hermes_home()
        profile = home.name if home.parent.name == "profiles" else "default"
        return {"version": __version__, "provider": mcfg.get("provider"), "model": mcfg.get("default"), "home": str(home),
                "profile": profile, "skills": len(SkillManager(self.cfg).discover()),
                "tools": len(registry.resolve_tools(self._web_toolsets(), self.cfg.get("disabled_tools"))),
                "approvals": (self.cfg.get("approvals", {}) or {}).get("mode"), "busy_sessions": list(self.busy)}

    def _web_toolsets(self) -> list[str]:
        pts = self.cfg.get("platform_toolsets", {}) or {}
        return pts.get("web") or pts.get("cli") or ["hermes-cli"]

    # ------------------------------------------------------------ chat
    def _agent_for(self, session_id: str | None):
        with self.lock:
            if session_id and session_id in self.agents:
                return self.agents[session_id]
        holder: dict[str, ChatRun | None] = {"run": None}

        def current() -> ChatRun | None:
            return holder["run"]

        def approval(cmd: str, reason: str) -> str:
            run = current()
            if run is None:
                return "deny"
            timeout = float((self.cfg.get("approvals", {}) or {}).get("timeout", 300))
            return run.ask("approval", timeout, command=cmd, reason=reason) or "deny"

        def clarify(question: str, choices) -> str:
            run = current()
            if run is None:
                return "(no answer)"
            return run.ask("clarify", 600, question=question, choices=choices or []) or "(no answer - use your best judgement)"

        callbacks = {
            "on_tool_start": lambda name, args: current() and current().tool_started(name, args),
            "on_tool_end": lambda name, out, err, ms: current() and current().tool_ended(name, out, err, ms),
            "on_thinking": lambda text: current() and current().emit("thinking", text=text[:4000]),
            "on_interim": lambda text: current() and current().emit("interim", text=text[:4000]),
        }
        exists = bool(session_id and self.db.get_session(session_id))
        agent = self.agent_factory(config=self.cfg, platform="web", toolsets=self._web_toolsets(), session_id=session_id if exists else None,
                                   resume=exists, approval_prompt=approval, clarify_fn=clarify, callbacks=callbacks, db=self.db)
        agent._web_holder = holder
        with self.lock:
            self.agents[agent.session_id] = agent
        return agent

    def start_chat(self, body: dict) -> dict:
        message = (body.get("message") or "").strip()
        if not message:
            raise HTTPError(400, "message is required")
        agent = self._agent_for(body.get("session_id") or None)
        sid = agent.session_id
        with self.lock:
            if sid in self.busy:
                raise HTTPError(409, "this session is already working - stop it or wait")
            run = ChatRun(sid)
            self.busy[sid] = run
            self.runs[run.id] = run
            if len(self.runs) > 200:  # forget old finished runs
                for rid in [r for r, v in self.runs.items() if v.done][:50]:
                    self.runs.pop(rid, None)
        agent._web_holder["run"] = run
        run.emit("user", text=message)

        def work():
            try:
                res = agent.run_conversation(message)
                media = [f"/api/media?path={urllib.parse.quote(m)}" for m in res.get("media", [])]
                run.emit("final", text=res.get("final_response") or "", error=res.get("error") or "", turns=res.get("turns"),
                         tool_calls=res.get("tool_calls"), cost_usd=round(agent.cost_usd, 6), media=media,
                         usage={"input": agent.usage.input_tokens, "output": agent.usage.output_tokens})
            except Exception as exc:
                log.exception("chat run failed")
                run.emit("error", text=f"{type(exc).__name__}: {exc}")
            finally:
                agent._web_holder["run"] = None
                with self.lock:
                    self.busy.pop(sid, None)
                if agent.session_id != sid:  # compression moved the conversation to a child session
                    with self.lock:
                        self.agents[agent.session_id] = agent

        threading.Thread(target=work, daemon=True, name=f"web-run-{run.id}").start()
        return {"run_id": run.id, "session_id": sid}

    def run_action(self, run_id: str, action: str, body: dict) -> dict:
        run = self.runs.get(run_id)
        if run is None:
            raise HTTPError(404, "unknown run")
        if action == "stop":
            agent = self.agents.get(run.session_id)
            if agent:
                agent.interrupt()
            for pid in list(run.waiting):
                run.answer(pid, "deny")
            return {"stopping": True}
        if action in ("approve", "answer"):
            value = str(body.get("choice") if action == "approve" else body.get("answer") or "")
            if action == "approve" and value not in ("once", "session", "always", "deny"):
                raise HTTPError(400, "choice must be once, session, always or deny")
            if not run.answer(str(body.get("prompt_id") or ""), value):
                raise HTTPError(404, "no such pending prompt")
            return {"ok": True}
        raise HTTPError(404, "unknown action")

    # ------------------------------------------------------------ sessions
    def sessions(self, method: str, rest: list[str], q: dict, body: dict) -> Any:
        if not rest and method == "GET":
            if q.get("q"):
                return {"sessions": self.db.search(q["q"], int(q.get("limit") or 30)), "search": True}
            return {"sessions": self.db.list_sessions(int(q.get("limit") or 50), q.get("source") or None)}
        sid = rest[0] if rest else ""
        if not self.db.get_session(sid):
            raise HTTPError(404, "session not found")
        if method == "GET":
            msgs = []
            for m in self.db.get_messages(sid):
                if m["role"] == "system":
                    continue
                item = {"role": m["role"], "content": m.get("content") or ""}
                if m.get("tool_calls"):
                    item["tool_calls"] = [{"name": tc["function"]["name"], "arguments": tc["function"]["arguments"][:2000]} for tc in m["tool_calls"]]
                if m["role"] == "tool":
                    item["name"] = m.get("name")
                    item["content"] = item["content"][:6000]
                    item["is_error"] = bool(m.get("_is_error"))
                msgs.append(item)
            return {"session": self.db.get_session(sid), "messages": msgs, "busy": sid in self.busy}
        if method == "PUT":
            self.db.set_title(sid, str(body.get("title") or "")[:120])
            return {"ok": True}
        if method == "DELETE":
            if sid in self.busy:
                raise HTTPError(409, "session is running")
            with self.lock:
                agent = self.agents.pop(sid, None)
            if agent:
                agent.close()
            return {"deleted": self.db.delete_session(sid)}
        raise HTTPError(405, "method not allowed")

    # ------------------------------------------------------------ memory
    def memory(self, method: str, rest: list[str], body: dict) -> dict:
        from hermes.memory.store import DELIM, MemoryStore

        store = MemoryStore(self.cfg)
        if method == "GET" and not rest:
            return {t: {"entries": f.entries(), "limit": f.limit, "used": f.size(), "path": str(f.path)}
                    for t, f in (("memory", store.memory), ("user", store.user))}
        if method == "PUT" and rest and rest[0] in ("memory", "user"):
            f = store.memory if rest[0] == "memory" else store.user
            entries = [str(e).strip() for e in body.get("entries", []) if str(e).strip()]
            for e in entries:
                err = store.validate(e)
                if err:
                    raise HTTPError(400, f"{err}: {e[:60]!r}")
            if len(DELIM.join(entries)) > f.limit:
                raise HTTPError(400, f"{len(DELIM.join(entries))} chars exceeds the {f.limit}-char limit - shorten or remove entries")
            f.write(entries)
            return {"ok": True, "used": f.size(), "limit": f.limit}
        raise HTTPError(405, "method not allowed")

    # ------------------------------------------------------------ skills
    def skills(self, method: str, rest: list[str], body: dict) -> dict:
        from hermes.skills import hub
        from hermes.skills.manager import SkillManager

        mgr = SkillManager(self.cfg)
        if method == "GET" and not rest:
            lock = hub.read_lock()
            issues = {r["name"]: r["issues"] for r in mgr.check()}
            return {"skills": [{"name": s.name, "category": s.category, "description": s.description, "hub": s.name in lock,
                                "editable": str(s.path).startswith(str(mgr.root)), "issues": issues.get(s.name, [])}
                               for s in mgr.discover()]}
        if method == "POST" and rest == ["install"]:
            res = hub.install(str(body.get("source") or ""), force=bool(body.get("force")), cfg=self.cfg)
            if not res.get("ok"):
                raise HTTPError(400, "; ".join(res.get("errors", ["install failed"])))
            return res
        if method == "POST" and rest == ["sync"]:
            return {"synced": mgr.sync_bundled()}
        name = rest[0] if rest else ""
        if method == "GET":
            res = mgr.view(name)
            if "error" in res:
                raise HTTPError(404, res["error"])
            return res
        if method == "PUT":
            content = str(body.get("content") or "")
            res = mgr.edit(name, content) if mgr.get(name) else mgr.create(name, content, str(body.get("category") or ""))
            if not res.get("ok"):
                raise HTTPError(400, "; ".join(res.get("errors", ["invalid skill"])))
            return res
        if method == "DELETE":
            res = mgr.delete(name)
            if not res.get("ok"):
                raise HTTPError(400, "; ".join(res.get("errors", ["delete failed"])))
            return res
        raise HTTPError(405, "method not allowed")

    # ------------------------------------------------------------ cron
    def cron(self, method: str, rest: list[str], body: dict) -> dict:
        from hermes.cron import jobs

        if method == "GET" and not rest:
            return {"jobs": [j.summary() for j in jobs.load_jobs()], "status": jobs.status_report()}
        if method == "POST" and not rest:
            if not body.get("prompt") or not body.get("schedule"):
                raise HTTPError(400, "prompt and schedule are required")
            job = jobs.create_job(str(body["prompt"]), str(body["schedule"]), name=str(body.get("name") or ""),
                                  deliver=str(body.get("deliver") or "local"),
                                  skills=[s.strip() for s in str(body.get("skills") or "").split(",") if s.strip()],
                                  origin={"platform": "web"})
            return {"job": job.summary()}
        jid = rest[0] if rest else ""
        job = jobs.find(jid)
        if job is None:
            raise HTTPError(404, "job not found")
        action = rest[1] if len(rest) > 1 else ""
        if method == "GET" and action == "outputs":
            return {"outputs": [{"name": p.name, "content": p.read_text(errors="replace")[:20000]} for p in jobs.outputs(jid, 10)]}
        if method == "POST" and action == "pause":
            return {"job": jobs.pause(jid).summary()}
        if method == "POST" and action == "resume":
            return {"job": jobs.resume(jid).summary()}
        if method == "POST" and action == "run":
            from hermes.gateway.delivery import deliver

            threading.Thread(target=jobs.run_job, args=(job, self.cfg, lambda t, text, o: deliver(t, text, o, self.cfg)),
                             daemon=True).start()
            return {"started": True}
        if method == "PUT" and not action:
            j = jobs.update_job(jid, prompt=body.get("prompt"), schedule=body.get("schedule"), name=body.get("name"), deliver=body.get("deliver"))
            return {"job": j.summary()}
        if method == "DELETE" and not action:
            return {"removed": jobs.remove(jid)}
        raise HTTPError(405, "method not allowed")

    # ------------------------------------------------------------ settings & tools
    def settings(self, method: str, body: dict) -> dict:
        if method == "PUT":
            changes = body.get("changes") or {}
            disk = hconfig.load_config(load_env=False)
            for k, v in changes.items():
                if k not in EDITABLE_SETTINGS:
                    raise HTTPError(400, f"{k} is not editable from the dashboard")
                typ = EDITABLE_SETTINGS[k]
                try:
                    v = typ(v) if typ is not bool else (v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on"))
                except (TypeError, ValueError):
                    raise HTTPError(400, f"{k} must be {typ.__name__}")
                if k in CHOICES and v not in CHOICES[k]:
                    raise HTTPError(400, f"{k} must be one of {CHOICES[k]}")
                disk.set_path(k, v)
                self.cfg.set_path(k, v)
            hconfig.save_config(disk)
            with self.lock:  # idle agents pick up the new settings on their next message
                for sid in [s for s in self.agents if s not in self.busy]:
                    self.agents.pop(sid).close(cleanup_env=False)
        from hermes.providers import PROVIDER_CHOICES

        return {"editable": {k: self.cfg.get_path(k) for k in EDITABLE_SETTINGS}, "choices": CHOICES,
                "providers": [{"id": p, "label": d, "default_model": m} for p, d, m in PROVIDER_CHOICES],
                "config": mask_secrets(dict(self.cfg)), "config_path": str(hconfig.path("config.yaml"))}

    def tools(self, method: str, q: dict, body: dict) -> dict:
        from hermes.tools import registry

        registry.load_builtin_tools()
        platform = str(body.get("platform") or q.get("platform") or "web")
        if method == "POST":
            ts, enabled = str(body.get("toolset") or ""), bool(body.get("enabled"))
            if ts not in registry.TOOLSETS:
                raise HTTPError(400, f"unknown toolset {ts}")
            disk = hconfig.load_config(load_env=False)
            current = registry.expand_toolsets(disk.get_path(f"platform_toolsets.{platform}") or self._web_toolsets())
            new = [t for t in current if t != ts] + ([ts] if enabled else [])
            disk.set_path(f"platform_toolsets.{platform}", new)
            hconfig.save_config(disk)
            self.cfg.set_path(f"platform_toolsets.{platform}", new)
            with self.lock:
                for sid in [s for s in self.agents if s not in self.busy]:
                    self.agents.pop(sid).close(cleanup_env=False)
        sets = registry.expand_toolsets(self.cfg.get_path(f"platform_toolsets.{platform}") or self._web_toolsets())
        disabled = set(self.cfg.get("disabled_tools") or [])
        out = []
        for ts, names in sorted(registry.TOOLSETS.items()):
            tools = []
            for n in names:
                t = registry.REGISTRY.get(n)
                if not t:
                    continue
                ok, why = t.availability()
                tools.append({"name": n, "description": t.description[:200], "available": ok, "reason": why, "disabled": n in disabled})
            out.append({"toolset": ts, "enabled": ts in sets, "tools": tools})
        return {"platform": platform, "toolsets": out}

    def doctor(self) -> dict:
        from hermes.doctor import run_doctor

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = run_doctor()
        lines = []
        for line in buf.getvalue().splitlines():
            s = line.strip()
            if s[:1] in ("✓", "⚠", "✗"):
                what, _, hint = s[2:].partition("  → ")
                lines.append({"status": {"✓": "ok", "⚠": "warn", "✗": "fail"}[s[0]], "check": what.strip(), "hint": hint.strip()})
        return {"exit_code": code, "checks": lines}

    def logs(self, kind: str, lines: int) -> dict:
        p = hconfig.path("logs", "errors.log" if kind == "errors" else "agent.log")
        text = p.read_text(errors="replace").splitlines()[-min(lines, 2000):] if p.exists() else []
        return {"path": str(p), "lines": [redact(t) for t in text]}

    def shutdown(self) -> None:
        for agent in list(self.agents.values()):
            agent.interrupt()
            agent.close()
