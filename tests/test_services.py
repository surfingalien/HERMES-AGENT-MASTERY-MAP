"""Messaging, browser & MCP, deployment-ish services and power-user features (map sections 5, 7, 8, 10)."""

from __future__ import annotations

import io
import json
import os
import sys
import textwrap
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from hermes import config
from hermes.gateway import delivery
from hermes.gateway.base import MessageEvent, PlatformAdapter, chunk_text
from hermes.gateway.runner import GatewayRunner, approve_pairing, load_pairing
from hermes.providers.base import ChatResponse
from hermes.providers.mock import MockProvider, scripted, tool_step
from hermes.tools import registry
from hermes.tools.registry import ToolContext, run_tool

from tests.helpers import HermesTestCase


class FakeAdapter(PlatformAdapter):
    name = "fake"
    max_len = 50

    def __init__(self, cfg=None, on_message=None):
        super().__init__(cfg, on_message)
        self.sent: list[tuple[str, str]] = []
        self.files: list[str] = []
        self.got = threading.Event()

    def allowed_users(self):
        return {"alice"}

    def send(self, chat_id, text, thread_id="", reply_to=""):
        self.sent.append((chat_id, text))
        self.got.set()

    def send_file(self, chat_id, path, caption="", thread_id=""):
        self.files.append(path)


def wait_for(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


class GatewayTests(HermesTestCase):
    def make_runner(self, provider=None):
        from hermes.agent import AIAgent

        adapter = FakeAdapter()
        prov = provider or MockProvider()
        runner = GatewayRunner(self.cfg, adapters={"fake": adapter},
                               agent_factory=lambda **kw: AIAgent(provider=prov, **kw))
        adapter.on_message = runner.handle_event
        return runner, adapter

    def ev(self, text, user="alice", chat="c1", **kw):
        return MessageEvent(platform="fake", chat_id=chat, user_id=user, text=text, **kw)

    def test_chunking_keeps_code_fences_balanced(self):
        chunks = chunk_text("intro\n\n```\n" + "\n".join(f"line {i}" for i in range(40)) + "\n```\n\nend", 60)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertEqual(c.count("```") % 2, 0, c)

    def test_authorized_conversation_persists(self):
        runner, adapter = self.make_runner()
        runner.handle_event(self.ev("hello there"))
        self.assertTrue(wait_for(lambda: adapter.sent))
        self.assertEqual(adapter.sent[0], ("c1", "Echo: hello there"))
        sid = runner.session_map["fake:c1"]
        runner2, adapter2 = self.make_runner()
        self.assertEqual(runner2.session_map["fake:c1"], sid)  # survives restarts

    def test_pairing_flow(self):
        runner, adapter = self.make_runner()
        runner.handle_event(self.ev("hi", user="bob"))
        self.assertIn("Pairing code", adapter.sent[0][1])
        code = adapter.sent[0][1].split("Pairing code: ")[1].split()[0]
        self.assertEqual(approve_pairing("fake", code)["user_id"], "bob")
        self.assertIn("bob", load_pairing()["approved"]["fake"])
        adapter.sent.clear()
        runner.handle_event(self.ev("now?", user="bob"))
        self.assertTrue(wait_for(lambda: adapter.sent))
        self.assertEqual(adapter.sent[0][1], "Echo: now?")

    def test_groups_require_mention(self):
        runner, adapter = self.make_runner()
        runner.handle_event(self.ev("chatter", is_group=True, mentioned=False))
        time.sleep(0.2)
        self.assertEqual(adapter.sent, [])

    def test_slash_commands(self):
        runner, adapter = self.make_runner()
        runner.handle_event(self.ev("/help"))
        self.assertIn("/new", "".join(t for _, t in adapter.sent))  # chunked at max_len
        runner.handle_event(self.ev("/sethome"))
        self.assertEqual(config.load_config().get_path("gateway.home_channels.fake"), "c1")
        runner.handle_event(self.ev("/voice on"))
        self.assertTrue(runner.chats["fake:c1"].voice_replies)

    def test_approval_over_chat(self):
        prov = scripted(tool_step("terminal", {"command": "sudo true || true"}), ChatResponse(content="finished"))
        self.cfg.set_path("approvals.mode", "manual")
        runner, adapter = self.make_runner(prov)
        runner.handle_event(self.ev("run it"))
        self.assertTrue(wait_for(lambda: any("Approval needed" in t for _, t in adapter.sent)))
        runner.handle_event(self.ev("/approve"))
        self.assertTrue(wait_for(lambda: any(t == "finished" for _, t in adapter.sent)))
        agent = runner.chats["fake:c1"].agent
        self.assertEqual(agent.approvals.log[0]["outcome"], "approved-once")

    def test_skill_slash_command(self):
        from hermes.skills.manager import SkillManager

        SkillManager(self.cfg).sync_bundled()
        prov = MockProvider()
        runner, adapter = self.make_runner(prov)
        runner.handle_event(self.ev("/reminders call mum at 6"))
        self.assertTrue(wait_for(lambda: adapter.sent))
        self.assertIn("[Skill invoked: reminders]", prov.calls[0]["messages"][-1]["content"])

    def test_delivery_resolution(self):
        self.cfg.set_path("gateway.home_channels.telegram", "555")
        self.assertEqual(delivery.resolve("telegram", None, self.cfg), ("telegram", "555", ""))
        self.assertEqual(delivery.resolve("slack:C1:171.2", None, self.cfg), ("slack", "C1", "171.2"))
        self.assertEqual(delivery.resolve("email:a@b.co", None, self.cfg), ("email", "a@b.co", ""))
        self.assertEqual(delivery.resolve("origin", {"platform": "discord", "chat_id": "9"}, self.cfg), ("discord", "9", ""))
        self.assertEqual(delivery.resolve("origin", {"platform": "cli"}, self.cfg)[0], "")
        with self.assertRaises(ValueError):
            delivery.resolve("signal", None, self.cfg)
        delivery.RUNNING["fake"] = FakeAdapter()
        try:
            res = delivery.deliver("fake:x", "hello", None, self.cfg)
            self.assertTrue(res["delivered"])
            self.assertEqual(delivery.RUNNING["fake"].sent, [("x", "hello")])
        finally:
            delivery.RUNNING.pop("fake")

    def test_whatsapp_webhook_signature(self):
        import hashlib
        import hmac

        from hermes.gateway.platforms.whatsapp import WhatsAppAdapter

        got = []
        a = WhatsAppAdapter({"whatsapp_access_token": "t", "whatsapp_phone_number_id": "1", "whatsapp_verify_token": "v",
                             "whatsapp_app_secret": "s3cret"}, got.append)
        self.assertEqual(a.handle_http("/?hub.mode=subscribe&hub.verify_token=v&hub.challenge=42", {}, b"", "GET"), (200, "42"))
        body = json.dumps({"entry": [{"changes": [{"value": {"contacts": [{"wa_id": "15551", "profile": {"name": "Al"}}],
                                                             "messages": [{"from": "15551", "id": "m1", "type": "text", "text": {"body": "yo"}}]}}]}]}).encode()
        self.assertEqual(a.handle_http("/", {"X-Hub-Signature-256": "sha256=bad"}, body)[0], 403)
        sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        self.assertEqual(a.handle_http("/", {"X-Hub-Signature-256": sig}, body)[0], 200)
        self.assertEqual((got[0].user_id, got[0].text, got[0].user_name), ("15551", "yo", "Al"))

    def test_platform_registry(self):
        from hermes.gateway.platforms import ADAPTERS, PLATFORM_ENV

        for name in ["telegram", "discord", "slack", "whatsapp", "signal", "email", "teams", "google_chat"]:
            self.assertIn(name, ADAPTERS)
            self.assertIn(name, PLATFORM_ENV)
        os.environ["TELEGRAM_BOT_TOKEN"] = "123:abc"
        self.assertTrue(ADAPTERS["telegram"].configured())


FAKE_MCP = textwrap.dedent('''
    import json, sys
    for line in sys.stdin:
        msg = json.loads(line)
        mid, method = msg.get("id"), msg.get("method")
        if mid is None:
            continue
        if method == "initialize":
            res = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "fake"}, "capabilities": {"tools": {}}}
        elif method == "tools/list":
            res = {"tools": [
                {"name": "echo", "description": "Echo text", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
                {"name": "delete_everything", "description": "nope", "inputSchema": {"type": "object", "properties": {}}}]}
        elif method == "tools/call":
            res = {"content": [{"type": "text", "text": "echo:" + msg["params"]["arguments"]["text"]}]}
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "nope"}}), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}), flush=True)
''')


class MCPTests(HermesTestCase):
    def test_stdio_server_with_filtering(self):
        from hermes.mcp import client

        script = self.tmp / "fake_mcp.py"
        script.write_text(FAKE_MCP)
        names = client.register_server("fake", {"command": sys.executable, "args": [str(script)], "tools": {"exclude": ["delete_everything"]}})
        try:
            self.assertEqual(names, ["mcp_fake_echo"])
            out, err = run_tool("mcp_fake_echo", {"text": "hi"}, ToolContext(config=self.cfg))
            self.assertEqual((out, err), ("echo:hi", False))
            self.assertIn("mcp-fake", registry.expand_toolsets(["hermes-cli"]))
        finally:
            client.shutdown()
        self.assertNotIn("mcp_fake_echo", registry.REGISTRY)

    def test_http_transport_sse(self):
        from hermes.mcp.client import MCPClient

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if "id" not in msg:
                    self.send_response(202)
                    self.end_headers()
                    return
                result = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "http-fake"}} if msg["method"] == "initialize" \
                    else {"tools": [{"name": "ping", "inputSchema": {"type": "object"}}]}
                body = f"event: message\ndata: {json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': result})}\n\n".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Mcp-Session-Id", "sess-1")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            c = MCPClient("h", {"url": f"http://127.0.0.1:{srv.server_address[1]}/mcp"})
            c.connect()
            self.assertEqual(c.server_info["name"], "http-fake")
            self.assertEqual(c.transport.session_id, "sess-1")
            self.assertEqual(c.list_tools()[0]["name"], "ping")
        finally:
            srv.shutdown()


class BrowserLiteTests(HermesTestCase):
    def test_navigate_fill_submit(self):
        class Site(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/search?"):
                    body = f"<html><title>Results</title><body><p>You searched: {self.path.split('q=')[1]}</p></body></html>"
                else:
                    body = ("<html><title>Home</title><body><a href='/about'>About us</a><form action='/search'>"
                            "<input name='q' placeholder='Search'><input type='submit' value='Go'></form></body></html>")
                data = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Site)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        os.environ["HERMES_ALLOW_PRIVATE_URLS"] = "1"
        self.cfg.set_path("browser.engine", "lite")
        registry.load_builtin_tools()
        ctx = ToolContext(config=self.cfg, task_id="browser-test")
        try:
            snap = json.loads(run_tool("browser_navigate", {"url": f"http://127.0.0.1:{srv.server_address[1]}/"}, ctx)[0])
            self.assertEqual(snap["title"], "Home")
            field = next(e.split()[0] for e in snap["elements"] if "[input:text]" in e)
            res = json.loads(run_tool("browser_type", {"ref": field, "text": "hermes", "submit": True}, ctx)[0])
            self.assertIn("You searched: hermes", res["text"])
            back = json.loads(run_tool("browser_back", {}, ctx)[0])
            self.assertEqual(back["title"], "Home")
        finally:
            run_tool("browser_close", {}, ctx)
            srv.shutdown()


class DelegationTests(HermesTestCase):
    def test_parallel_subagents_are_isolated(self):
        prov = MockProvider()
        parent = self.agent(prov)
        registry.load_builtin_tools()
        ctx = parent._tool_context()
        out, err = run_tool("delegate_task", {"tasks": [{"goal": "alpha"}, {"goal": "beta"}], "toolsets": ["file", "delegation"]}, ctx)
        res = json.loads(out)
        self.assertFalse(err, out)
        self.assertEqual(len(res["results"]), 2)
        self.assertTrue(all(r["status"] == "completed" for r in res["results"]))
        child_calls = [c for c in prov.calls if "subagent" in c["messages"][0]["content"]]
        self.assertTrue(child_calls)
        tool_names = {t["name"] for c in child_calls for t in (c["tools"] or [])}
        self.assertNotIn("delegate_task", tool_names)  # no recursive delegation from children
        self.assertNotIn("memory", tool_names)
        ctx.depth = 2
        out, err = run_tool("delegate_task", {"goal": "too deep"}, ctx)
        self.assertTrue(err)


class APIServerTests(HermesTestCase):
    def test_chat_completions(self):
        from hermes.power.api_server import make_server

        os.environ["API_SERVER_KEY"] = "test-key"
        srv = make_server(self.cfg, "127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            req = urllib.request.Request(f"{base}/v1/chat/completions", method="POST",
                                         data=json.dumps({"model": "hermes-agent", "messages": [{"role": "user", "content": "ping"}]}).encode(),
                                         headers={"Content-Type": "application/json", "Authorization": "Bearer test-key"})
            data = json.loads(urllib.request.urlopen(req, timeout=10).read())
            self.assertEqual(data["choices"][0]["message"]["content"], "Echo: ping")
            self.assertTrue(data["session_id"])
            bad = urllib.request.Request(f"{base}/v1/models", headers={"Authorization": "Bearer nope"})
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(bad, timeout=5)
            self.assertEqual(cm.exception.code, 401)
            req = urllib.request.Request(f"{base}/v1/chat/completions", method="POST",
                                         data=json.dumps({"stream": True, "messages": [{"role": "user", "content": "stream me"}]}).encode(),
                                         headers={"Content-Type": "application/json", "Authorization": "Bearer test-key"})
            body = urllib.request.urlopen(req, timeout=10).read().decode()
            self.assertIn("Echo: stream me", body)
            self.assertTrue(body.strip().endswith("[DONE]"))
        finally:
            srv.shutdown()

    def test_refuses_public_bind_without_key(self):
        from hermes.power.api_server import make_server

        os.environ.pop("API_SERVER_KEY", None)
        with self.assertRaises(RuntimeError):
            make_server(self.cfg, "0.0.0.0", 0)


class ACPTests(HermesTestCase):
    def test_protocol_roundtrip(self):
        from hermes.agent import AIAgent
        from hermes.power.acp import ACPServer

        out = io.StringIO()
        srv = ACPServer(self.cfg, stdout=out, agent_factory=lambda **kw: AIAgent(provider=MockProvider(), **kw))
        init = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}})
        self.assertEqual(init["result"]["protocolVersion"], 1)
        sid = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": str(self.work)}})["result"]["sessionId"]
        res = srv.handle({"jsonrpc": "2.0", "id": 3, "method": "session/prompt",
                          "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "call:todo {}"}]}})
        self.assertEqual(res["result"]["stopReason"], "end_turn")
        updates = [json.loads(line)["params"]["update"] for line in out.getvalue().splitlines()]
        kinds = [u["sessionUpdate"] for u in updates]
        self.assertEqual(kinds, ["tool_call", "tool_call_update", "agent_message_chunk"])
        self.assertEqual(updates[0]["toolCallId"], updates[1]["toolCallId"])
        self.assertIn("error", srv.handle({"jsonrpc": "2.0", "id": 4, "method": "nope"}))


class KanbanBatchTests(HermesTestCase):
    def test_kanban_dependencies_and_worker(self):
        from hermes.power.kanban import Board, run_worker

        board = Board()
        research = board.add("research topic")
        write = board.add("write article", depends_on=[research["id"]], assignee="writer")
        first = board.claim("w1", profile="")
        self.assertEqual(first["id"], research["id"])
        self.assertIsNone(board.claim("w2", profile="writer"))  # dependency not done yet
        board.move(research["id"], "done", "facts: sky is blue")
        card = board.claim("w2", profile="writer")
        self.assertEqual(card["id"], write["id"])
        self.assertIn("sky is blue", board.context_for(card))
        board.move(card["id"], "todo")

        class A:
            session_id = "s"

            def __init__(self, **kw):
                pass

            def run_conversation(self, p):
                return {"final_response": "article done" if "write article" in p else "BLOCKED: no access"}

        n = run_worker(self.cfg, board, profile="writer", once=True, agent_factory=A)
        self.assertEqual(n, 1)
        self.assertEqual(board.get(write["id"])["status"], "done")

    def test_batch_run(self):
        from hermes.power.batch import run_batch

        inp = self.tmp / "prompts.jsonl"
        inp.write_text('{"id": "a", "prompt": "first"}\n{"id": "b", "prompt": "call:todo {}"}\n')
        summary = run_batch(self.cfg, str(inp), str(self.tmp / "out"), workers=2)
        self.assertEqual((summary["items"], summary["ok"]), (2, 2))
        traj = [json.loads(line) for line in (self.tmp / "out" / "trajectories.jsonl").read_text().splitlines()]
        self.assertEqual(traj[0]["conversations"][1]["from"], "human")
        self.assertTrue(any("<tool_call>" in turn["value"] for t in traj for turn in t["conversations"]))


class PluginHookTests(HermesTestCase):
    def test_plugin_scaffold_enable_load(self):
        from hermes import plugins

        plugins.scaffold("greeter")
        self.assertFalse(plugins.load_plugins(self.cfg)[0].loaded)  # opt-in
        self.cfg.set_path("plugins.enabled", ["greeter"])
        info = plugins.load_plugins(self.cfg)[0]
        self.assertTrue(info.loaded, info.error)
        out, err = run_tool("greeter_hello", {"name": "Ada"}, ToolContext(config=self.cfg))
        self.assertIn("Hello, Ada!", out)

    def test_directory_hooks_and_veto(self):
        from hermes.hooks import HookBus

        hook_dir = config.path("hooks", "audit")
        hook_dir.mkdir(parents=True)
        (hook_dir / "HOOK.yaml").write_text("name: audit\nevents: ['tool:*']\n")
        log_file = self.tmp / "audit.log"
        (hook_dir / "handler.py").write_text(
            f"def handle(event, ctx):\n    open({str(log_file)!r}, 'a').write(event + ':' + ctx.get('tool', '') + '\\n')\n"
            "    if ctx.get('tool') == 'terminal' and event == 'tool:pre':\n        return {'block': 'terminal disabled by policy'}\n")
        bus = HookBus()
        self.assertEqual(bus.load_directory_hooks(), ["audit"])
        import hermes.hooks as hooks_mod

        old = hooks_mod.BUS.handlers.copy()
        hooks_mod.BUS.handlers.update(bus.handlers)
        try:
            prov = scripted(tool_step("terminal", {"command": "ls"}), ChatResponse(content="ok"))
            agent = self.agent(prov)
            agent.run_conversation("list")
        finally:
            hooks_mod.BUS.handlers.clear()
            hooks_mod.BUS.handlers.update(old)
        self.assertIn("blocked by hook", [m for m in agent.messages if m["role"] == "tool"][0]["content"])
        self.assertIn("tool:pre:terminal", log_file.read_text())


class BackupProfileTests(HermesTestCase):
    def test_backup_restore(self):
        from hermes.memory.store import MemoryStore
        from hermes.power.backup import create_backup, restore_backup
        from hermes.sessions.db import SessionDB

        MemoryStore(self.cfg).add("user", "Name is Ada")
        config.set_env_value("SECRET_TOKEN", "abc")
        SessionDB().create_session(title="keep me")
        archive = create_backup(str(self.tmp / "b.tar.gz"))
        import shutil
        import tarfile

        names = tarfile.open(archive).getnames()
        self.assertNotIn(".env", names)  # secrets excluded by default
        self.assertIn("state.db", names)
        shutil.rmtree(self.home)
        restore_backup(str(archive))
        self.assertIn("Name is Ada", MemoryStore(self.cfg).snapshot())
        self.assertEqual(SessionDB().list_sessions()[0]["title"], "keep me")

    def test_cli_profile_and_commands(self):
        from hermes.cli import main

        os.environ["HERMES_SKIP_SETUP"] = "1"
        os.environ.pop("HERMES_HOME")
        self.assertEqual(main(["profile", "create", "researcher", "--soul", "You research."]), 0)
        os.environ["HERMES_HOME"] = str(config.base_home() / "profiles" / "researcher")
        self.assertIn("You research.", (config.hermes_home() / "SOUL.md").read_text())
        self.assertEqual(main(["config", "set", "model.provider", "mock"]), 0)
        self.assertEqual(main(["-q", "hello from profile"]), 0)
        self.assertEqual(main(["sessions", "list"]), 0)
        self.assertEqual(main(["cron", "create", "every 2h", "check the news"]), 0)
        self.assertEqual(main(["insights", "--json"]), 0)
        self.assertEqual(main(["skills", "check"]), 0)
        self.assertEqual(main(["mcp", "add", "fs", "--include", "read_file", "--", "python3", "srv.py", "--flag"]), 0)
        spec = config.load_config().get_path("mcp_servers.fs")
        self.assertEqual((spec["command"], spec["args"], spec["tools"]), ("python3", ["srv.py", "--flag"], {"include": ["read_file"]}))


if __name__ == "__main__":
    unittest.main()
