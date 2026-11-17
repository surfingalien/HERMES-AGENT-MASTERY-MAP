"""Web dashboard backend (served over real HTTP)."""

from __future__ import annotations

import http.client
import json
import threading
import time

from hermes import config
from hermes.providers.base import ChatResponse
from hermes.providers.mock import MockProvider, scripted, tool_step

from tests.helpers import HermesTestCase


class DashboardTests(HermesTestCase):
    def start(self, provider=None):
        from hermes.agent import AIAgent
        from hermes.power.api_server import make_server
        from hermes.web.dashboard import Dashboard

        prov = provider or MockProvider()
        self.dash = Dashboard(self.cfg, token="tok-123", agent_factory=lambda **kw: AIAgent(provider=prov, **kw))
        self.srv = make_server(self.cfg, "127.0.0.1", 0, dashboard=self.dash)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]
        self.cookie = None
        self.addCleanup(self.srv.shutdown)
        self.addCleanup(self.dash.shutdown)

    def req(self, method, path, body=None, cookie=True, headers=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = {"Host": host or f"127.0.0.1:{self.port}"}
        if cookie and self.cookie:
            hdrs["Cookie"] = self.cookie
        if method != "GET":
            hdrs["X-Hermes"] = "1"
        hdrs.update(headers or {})
        data = json.dumps(body).encode() if body is not None else None
        if data:
            hdrs["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        try:
            return resp.status, json.loads(raw), resp
        except ValueError:
            return resp.status, raw.decode(errors="replace"), resp

    def login(self):
        status, _, resp = self.req("GET", "/?token=tok-123", cookie=False)
        self.assertEqual(status, 303)
        set_cookie = resp.getheader("Set-Cookie")
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=Strict", set_cookie)
        self.cookie = set_cookie.split(";")[0]

    def events(self, run_id, timeout=10):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        conn.request("GET", f"/api/runs/{run_id}/events", headers={"Host": f"127.0.0.1:{self.port}", "Cookie": self.cookie})
        resp = conn.getresponse()
        self.assertEqual(resp.getheader("Content-Type"), "text/event-stream")
        out = []
        for line in resp:
            line = line.decode().strip()
            if line.startswith("data: ") and line != "data: {}":
                out.append(json.loads(line[6:]))
            if line == "event: end":
                break
        conn.close()
        return out

    # ------------------------------------------------------------ security
    def test_static_and_security_headers(self):
        self.start()
        status, body, resp = self.req("GET", "/", cookie=False)
        self.assertEqual(status, 200)
        self.assertIn("<script src=\"/app.js\">", body)
        self.assertIn("script-src 'self'", resp.getheader("Content-Security-Policy"))
        self.assertEqual(resp.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(self.req("GET", "/app.js", cookie=False)[0], 200)
        self.assertEqual(self.req("GET", "/../config.yaml", cookie=False)[0], 404)
        self.assertEqual(self.req("GET", "/%2e%2e/state.db", cookie=False)[0], 404)

    def test_auth_csrf_and_host_checks(self):
        self.start()
        self.assertEqual(self.req("GET", "/api/status")[0], 401)
        self.assertEqual(self.req("GET", "/?token=wrong", cookie=False)[0], 403)
        self.login()
        self.assertEqual(self.req("GET", "/api/status")[0], 200)
        self.assertEqual(self.req("GET", "/api/status", headers={"Cookie": "hermes_session=forged"}, cookie=False)[0], 401)
        # bearer token also works (scripts)
        self.assertEqual(self.req("GET", "/api/status", cookie=False, headers={"Authorization": "Bearer tok-123"})[0], 200)
        # mutating request without the custom header is rejected (CSRF)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/chat", body=b'{"message":"x"}', headers={"Host": f"127.0.0.1:{self.port}", "Cookie": self.cookie})
        self.assertEqual(conn.getresponse().status, 403)
        conn.close()
        # DNS-rebinding style Host header is rejected
        self.assertEqual(self.req("GET", "/api/status", host="evil.example.com")[0], 421)
        # the OpenAI API keeps working on the same port
        self.assertEqual(self.req("GET", "/health", cookie=False)[0], 200)

    def test_public_bind_needs_token(self):
        import os

        from hermes.power.api_server import make_server
        from hermes.web.dashboard import Dashboard

        os.environ["API_SERVER_KEY"] = "k"
        os.environ.pop("HERMES_WEB_TOKEN", None)
        with self.assertRaises(RuntimeError):
            make_server(self.cfg, "0.0.0.0", 0, dashboard=Dashboard(self.cfg, token="x"))

    # ------------------------------------------------------------ chat
    def test_chat_streams_tools_and_final(self):
        self.start(scripted(tool_step("todo", {"todos": [{"id": "1", "content": "plan", "status": "in_progress"}]}),
                            ChatResponse(content="**Done** - planned.")))
        self.login()
        status, res, _ = self.req("POST", "/api/chat", {"message": "make a plan"})
        self.assertEqual(status, 200, res)
        evs = self.events(res["run_id"])
        kinds = [e["type"] for e in evs]
        self.assertEqual(kinds, ["user", "tool_start", "tool_end", "final"])
        self.assertEqual(evs[1]["id"], evs[2]["id"])
        self.assertEqual(evs[-1]["text"], "**Done** - planned.")
        # session is persisted and listed
        status, sess, _ = self.req("GET", f"/api/sessions/{res['session_id']}")
        self.assertEqual([m["role"] for m in sess["messages"]], ["user", "assistant", "tool", "assistant"])
        self.assertEqual(sess["session"]["source"], "web")
        listed = self.req("GET", "/api/sessions")[1]["sessions"]
        self.assertIn(res["session_id"], [s["id"] for s in listed])
        # continuing the same session reuses it
        status, res2, _ = self.req("POST", "/api/chat", {"message": "and again", "session_id": res["session_id"]})
        self.assertEqual(res2["session_id"], res["session_id"])
        self.events(res2["run_id"])

    def test_approval_round_trip(self):
        self.start(scripted(tool_step("terminal", {"command": "sudo true || echo ran"}), ChatResponse(content="finished")))
        self.login()
        _, res, _ = self.req("POST", "/api/chat", {"message": "run it"})
        run = self.dash.runs[res["run_id"]]
        for _ in range(100):
            if run.waiting:
                break
            time.sleep(0.05)
        pid = next(iter(run.waiting))
        self.assertEqual(self.req("POST", f"/api/runs/{res['run_id']}/approve", {"prompt_id": pid, "choice": "bogus"})[0], 400)
        self.assertEqual(self.req("POST", f"/api/runs/{res['run_id']}/approve", {"prompt_id": pid, "choice": "once"})[0], 200)
        evs = self.events(res["run_id"])
        kinds = [e["type"] for e in evs]
        self.assertIn("approval", kinds)
        self.assertEqual(kinds[-1], "final")
        tool_out = next(e for e in evs if e["type"] == "tool_end")["output"]
        self.assertNotIn("blocked", tool_out)

    def test_busy_session_conflict_and_stop(self):
        gate = threading.Event()

        def slow(messages, tools):
            gate.wait(5)
            return ChatResponse(content="late")

        self.start(MockProvider(script=[slow]))
        self.login()
        _, res, _ = self.req("POST", "/api/chat", {"message": "slow"})
        self.assertEqual(self.req("POST", "/api/chat", {"message": "again", "session_id": res["session_id"]})[0], 409)
        self.assertEqual(self.req("POST", f"/api/runs/{res['run_id']}/stop", {})[0], 200)
        gate.set()
        self.assertEqual(self.events(res["run_id"])[-1]["type"], "final")

    # ------------------------------------------------------------ pages
    def test_memory_skills_cron_settings_tools(self):
        self.start()
        self.login()
        # memory
        mem = self.req("GET", "/api/memory")[1]
        self.assertEqual(mem["memory"]["limit"], 2200)
        self.assertEqual(self.req("PUT", "/api/memory/user", {"entries": ["Prefers metric units", "Works in UTC"]})[0], 200)
        self.assertEqual(self.req("GET", "/api/memory")[1]["user"]["entries"], ["Prefers metric units", "Works in UTC"])
        status, err, _ = self.req("PUT", "/api/memory/user", {"entries": ["key sk-ant-api03-aaaaaaaaaaaaaaaaaaaaaaa"]})
        self.assertEqual(status, 400)
        self.assertEqual(self.req("PUT", "/api/memory/memory", {"entries": ["x" * 3000]})[0], 400)
        # skills
        skill = "---\nname: demo-skill\ndescription: Demo skill for the dashboard test, use when testing.\n---\n# Demo\n1. step\n"
        self.assertEqual(self.req("PUT", "/api/skills/demo-skill", {"content": skill, "category": "test"})[0], 200)
        self.assertIn("demo-skill", [s["name"] for s in self.req("GET", "/api/skills")[1]["skills"]])
        self.assertIn("# Demo", self.req("GET", "/api/skills/demo-skill")[1]["content"])
        self.assertEqual(self.req("PUT", "/api/skills/demo-skill", {"content": "no frontmatter"})[0], 400)
        self.assertEqual(self.req("DELETE", "/api/skills/demo-skill")[0], 200)
        self.assertGreaterEqual(len(self.req("POST", "/api/skills/sync", {})[1]["synced"]), 10)
        # cron
        status, job, _ = self.req("POST", "/api/cron", {"prompt": "Summarise news", "schedule": "weekdays at 8am", "name": "news"})
        self.assertEqual(status, 200, job)
        jid = job["job"]["id"]
        self.assertFalse(self.req("POST", f"/api/cron/{jid}/pause", {})[1]["job"]["enabled"])
        self.assertTrue(self.req("POST", f"/api/cron/{jid}/resume", {})[1]["job"]["enabled"])
        self.assertEqual(self.req("POST", "/api/cron", {"prompt": "x", "schedule": "whenever"})[0], 400)
        self.assertTrue(self.req("DELETE", f"/api/cron/{jid}")[1]["removed"])
        # settings
        config.set_env_value("OPENROUTER_API_KEY", "sk-or-v1-abcdefghijklmnopqrstuvwxyz0123")
        self.cfg.set_path("mcp_servers", {"x": {"url": "https://m", "headers": {"Authorization": "Bearer abcdefghijklmnop12345"}}})
        st = self.req("GET", "/api/settings")[1]
        self.assertNotIn("abcdefghijklmnop12345", json.dumps(st))
        self.assertEqual(self.req("PUT", "/api/settings", {"changes": {"approvals.mode": "smart", "agent.max_turns": "40"}})[0], 200)
        disk = config.load_config()
        self.assertEqual((disk.get_path("approvals.mode"), disk.get_path("agent.max_turns")), ("smart", 40))
        self.assertEqual(self.req("PUT", "/api/settings", {"changes": {"approvals.mode": "yolo"}})[0], 400)
        self.assertEqual(self.req("PUT", "/api/settings", {"changes": {"security.redact_secrets": False}})[0], 400)
        # tools
        tools = self.req("GET", "/api/tools?platform=web")[1]
        browser = next(t for t in tools["toolsets"] if t["toolset"] == "browser")
        self.assertTrue(browser["enabled"])
        self.req("POST", "/api/tools", {"platform": "web", "toolset": "browser", "enabled": False})
        tools = self.req("GET", "/api/tools?platform=web")[1]
        self.assertFalse(next(t for t in tools["toolsets"] if t["toolset"] == "browser")["enabled"])
        self.assertTrue(next(t for t in tools["toolsets"] if t["toolset"] == "web")["enabled"])
        # insights, doctor, logs, status
        self.assertIn("totals", self.req("GET", "/api/insights?days=7")[1])
        doc = self.req("GET", "/api/doctor")[1]
        self.assertTrue(any(c["status"] == "ok" for c in doc["checks"]))
        self.assertIn("lines", self.req("GET", "/api/logs")[1])
        st = self.req("GET", "/api/status")[1]
        self.assertEqual(st["provider"], "mock")

    def test_media_is_confined_to_cache(self):
        self.start()
        self.login()
        img = config.path("cache", "images", "x.png")
        img.parent.mkdir(parents=True, exist_ok=True)
        img.write_bytes(b"\x89PNG fake")
        status, _, resp = self.req("GET", f"/api/media?path={img}")
        self.assertEqual(status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "image/png")
        self.assertEqual(self.req("GET", f"/api/media?path={config.path('config.yaml')}")[0], 404)
        self.assertEqual(self.req("GET", f"/api/media?path={img.parent}/../../.env")[0], 404)
