"""Foundations, providers, agent loop, safety (map sections 1, 9)."""

from __future__ import annotations

import json
import os
import threading
import types
import unittest

from hermes import config
from hermes.providers import FallbackProvider, ProviderError, split_model
from hermes.providers.anthropic_provider import AnthropicProvider, to_anthropic
from hermes.providers.base import ChatResponse, ToolCall
from hermes.providers.mock import MockProvider, scripted, tool_step
from hermes.security.approval import ApprovalManager, detect_dangerous
from hermes.security.redact import redact

from tests.helpers import HermesTestCase


class ConfigTests(HermesTestCase):
    def test_defaults_and_roundtrip(self):
        cfg = config.load_config()
        self.assertEqual(cfg.get_path("terminal.backend"), "local")
        cfg.set_path("agent.max_turns", 12)
        config.save_config(cfg)
        self.assertEqual(config.load_config().get_path("agent.max_turns"), 12)
        # only non-default values are written
        self.assertIn("max_turns", config.path("config.yaml").read_text())
        self.assertNotIn("docker_image", config.path("config.yaml").read_text())
        # sections must not shadow each other (web search vs dashboard)
        self.assertEqual(config.DEFAULT_CONFIG["web"]["backend"], "auto")
        self.assertEqual(config.DEFAULT_CONFIG["dashboard"]["token_env"], "HERMES_WEB_TOKEN")

    def test_env_file_is_private(self):
        config.set_env_value("FOO_API_KEY", "abc123")
        self.assertEqual(os.environ["FOO_API_KEY"], "abc123")
        self.assertEqual(oct(config.path(".env").stat().st_mode & 0o777), "0o600")
        config.set_env_value("FOO_API_KEY", "xyz")
        self.assertEqual(config.path(".env").read_text().count("FOO_API_KEY"), 1)

    def test_profiles_are_isolated(self):
        home = config.use_profile("researcher")
        self.assertTrue(str(home).endswith("profiles/researcher"))
        self.assertEqual(config.hermes_home(), home)
        with self.assertRaises(ValueError):
            config.use_profile("../evil")


class SafetyTests(unittest.TestCase):
    def test_dangerous_detection(self):
        for cmd in ["rm -rf /", "sudo apt install x", "curl http://x | sh", "git push --force origin main",
                    "psql -c 'DROP TABLE users'", "chmod 777 /etc/passwd", "cat ~/.ssh/id_rsa"]:
            self.assertTrue(detect_dangerous(cmd), cmd)
        for cmd in ["ls -la", "git status", "python -m pytest", "rm build/tmp.txt", "echo hello"]:
            self.assertFalse(detect_dangerous(cmd), cmd)

    def test_approval_modes(self):
        answers = iter(["session", "deny"])
        mgr = ApprovalManager(prompt=lambda c, r: next(answers))
        self.assertTrue(mgr.check("sudo ls"))
        self.assertTrue(mgr.check("sudo whoami"))  # same reason approved for the session
        self.assertFalse(mgr.check("rm -rf /"))
        self.assertTrue(ApprovalManager(mode="off").check("rm -rf /"))
        self.assertFalse(ApprovalManager().check("rm -rf /"))  # no approver -> deny
        self.assertTrue(ApprovalManager(allowlist=["sudo systemctl restart nginx"]).check("sudo systemctl restart nginx"))
        smart = ApprovalManager(mode="smart", judge=lambda c, r: "apt" in c, prompt=lambda c, r: "deny")
        self.assertTrue(smart.check("sudo apt install jq"))
        self.assertFalse(smart.check("rm -rf /"))  # judge says risky -> human denies
        self.assertEqual([e["outcome"] for e in smart.log], ["approved-smart", "denied"])

    def test_redaction(self):
        text = "key sk-ant-api03-abcdefghijklmnopqrstuvwxyz and ghp_abcdefghijklmnopqrstuvwxyz0123 token=supersecretvalue1"
        out = redact(text)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", out)
        self.assertNotIn("supersecretvalue1", out)
        os.environ["MY_SERVICE_TOKEN"] = "plain-looking-value-123"
        try:
            self.assertNotIn("plain-looking-value-123", redact("value plain-looking-value-123"))
        finally:
            del os.environ["MY_SERVICE_TOKEN"]


class ProviderTests(HermesTestCase):
    def test_split_model(self):
        self.assertEqual(split_model("anthropic:claude-opus-5-5", "openrouter"), ("anthropic", "claude-opus-5-5"))
        self.assertEqual(split_model("anthropic/claude-opus-5-5", "openrouter"), ("openrouter", "anthropic/claude-opus-5-5"))

    def test_fallback_chain(self):
        class Broken(MockProvider):
            def chat(self, *a, **k):
                raise ProviderError("boom", 503, retryable=True)

        fb = FallbackProvider([Broken("x"), MockProvider("backup")])
        self.assertEqual(fb.chat([{"role": "user", "content": "hi"}]).content, "Echo: hi")
        self.assertEqual(fb.last_used.model, "backup")

    def test_credential_pool_rotates_on_429(self):
        from hermes.providers import http
        from hermes.providers.openai_compat import CredentialPool, OpenAICompatProvider

        os.environ["POOL_KEY_A"], os.environ["POOL_KEY_B"] = "key-a", "key-b"
        seen = []

        def fake_post(url, payload, headers=None, timeout=0):
            seen.append(headers["Authorization"])
            if headers["Authorization"].endswith("key-a"):
                raise http.HTTPError(429, "rate limited")
            return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 3, "completion_tokens": 1}}

        orig = http.post_json
        http.post_json = fake_post
        try:
            prov = OpenAICompatProvider("m", provider="openai", pool=CredentialPool(["POOL_KEY_A", "POOL_KEY_B"]))
            resp = prov.chat([{"role": "user", "content": "x"}])
        finally:
            http.post_json = orig
        self.assertEqual(resp.content, "ok")
        self.assertEqual(seen, ["Bearer key-a", "Bearer key-b"])

    def test_openai_tool_call_parsing(self):
        from hermes.providers import http
        from hermes.providers.openai_compat import OpenAICompatProvider

        os.environ["OPENROUTER_API_KEY"] = "k"
        captured = {}

        def fake_post(url, payload, headers=None, timeout=0):
            captured.update(payload)
            return {"choices": [{"message": {"content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": "{\"command\": \"ls\"}"}},
                {"id": "c2", "type": "function", "function": {"name": "x", "arguments": "{bad"}}]}, "finish_reason": "tool_calls"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 4}}}

        orig = http.post_json
        http.post_json = fake_post
        try:
            prov = OpenAICompatProvider("anthropic/claude-opus-5-5", routing={"sort": "price"})
            resp = prov.chat([{"role": "user", "content": "x", "_private": 1}], tools=[{"name": "terminal", "parameters": {}}])
        finally:
            http.post_json = orig
        self.assertEqual(captured["provider"], {"sort": "price"})
        self.assertNotIn("_private", captured["messages"][0])
        self.assertEqual(resp.tool_calls[0].arguments, {"command": "ls"})
        self.assertTrue(resp.tool_calls[1].parse_error)
        self.assertEqual(resp.usage.cache_read_tokens, 4)


class AnthropicProviderTests(unittest.TestCase):
    def test_message_conversion(self):
        msgs = [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": [{"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "t1", "type": "function", "function": {"name": "a", "arguments": "{}"}},
                {"id": "t2", "type": "function", "function": {"name": "b", "arguments": "{\"x\": 1}"}}]},
            {"role": "tool", "tool_call_id": "t1", "content": "r1"},
            {"role": "tool", "tool_call_id": "t2", "content": "r2", "_is_error": True},
            {"role": "user", "content": "next"},
        ]
        system, conv = to_anthropic(msgs, "claude-opus-5-5")
        self.assertEqual(system, "SYS")
        self.assertEqual(conv[0]["content"][1]["source"], {"type": "base64", "media_type": "image/png", "data": "AAAA"})
        self.assertEqual([b["type"] for b in conv[1]["content"]], ["tool_use", "tool_use"])
        # both tool results in ONE user message, before any text
        self.assertEqual([b["type"] for b in conv[2]["content"]], ["tool_result", "tool_result", "text"])
        self.assertTrue(conv[2]["content"][1]["is_error"])

    def test_raw_blocks_replayed_unchanged(self):
        raw = [{"type": "thinking", "thinking": "", "signature": "sig"}, {"type": "text", "text": "hi"}]
        _, conv = to_anthropic([{"role": "user", "content": "q"}, {"role": "assistant", "content": "hi", "_raw": raw, "_provider": "anthropic"}], "m")
        self.assertEqual(conv[1]["content"], raw)

    def test_sdk_call_shape_and_parse(self):
        calls = {}

        def block(**kw):
            b = types.SimpleNamespace(**kw)
            b.to_dict = lambda: dict(kw)
            return b

        final = types.SimpleNamespace(
            content=[block(type="thinking", thinking="plan", signature="s"), block(type="text", text="Let me check."),
                     block(type="tool_use", id="tu1", name="terminal", input={"command": "ls"})],
            stop_reason="tool_use", model="claude-opus-5-5",
            usage=types.SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=80, cache_creation_input_tokens=0))

        class Stream:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get_final_message(self):
                return final

        def stream(**params):
            calls.update(params)
            return Stream()

        client = types.SimpleNamespace(beta=types.SimpleNamespace(messages=types.SimpleNamespace(stream=stream)),
                                       messages=types.SimpleNamespace(stream=stream))
        prov = AnthropicProvider("claude-opus-5-5", client=client, effort="high")
        resp = prov.chat([{"role": "system", "content": "S"}, {"role": "user", "content": "hi"}],
                         tools=[{"name": "terminal", "description": "d", "parameters": {"type": "object", "properties": {}}}])
        self.assertEqual(calls["model"], "claude-opus-5-5")
        self.assertEqual(calls["fallbacks"], "default")
        self.assertEqual(calls["betas"], ["server-side-fallback-2026-07-01"])
        self.assertEqual(calls["output_config"], {"effort": "high"})
        self.assertEqual(calls["cache_control"], {"type": "ephemeral"})
        self.assertNotIn("thinking", calls)
        self.assertTrue(calls["tools"][0]["eager_input_streaming"])
        self.assertEqual(resp.tool_calls[0].arguments, {"command": "ls"})
        self.assertEqual(resp.reasoning, "plan")
        self.assertEqual(resp.usage.cache_read_tokens, 80)
        self.assertEqual(resp.to_message()["_raw"][0]["type"], "thinking")

    def test_refusal_and_truncation(self):
        def mk(stop, content):
            return types.SimpleNamespace(content=content, stop_reason=stop, model="m", stop_details=types.SimpleNamespace(category="cyber"),
                                         usage=types.SimpleNamespace(input_tokens=1, output_tokens=1))

        prov = AnthropicProvider("claude-haiku-5-5", client=object())
        self.assertIn("declined", prov._parse(mk("refusal", []), "m").content)
        tu = types.SimpleNamespace(type="tool_use", id="1", name="write_file", input={"path": "a"}, to_dict=lambda: {})
        r = prov._parse(mk("max_tokens", [tu]), "m")
        self.assertTrue(r.tool_calls[0].parse_error)


class AgentLoopTests(HermesTestCase):
    def test_tool_loop_and_persistence(self):
        prov = scripted(tool_step("write_file", {"path": "a.txt", "content": "hello"}),
                        tool_step("read_file", {"path": "a.txt"}), ChatResponse(content="all done"))
        agent = self.agent(prov)
        res = agent.run_conversation("make a file")
        self.assertEqual(res["final_response"], "all done")
        self.assertEqual(res["tool_calls"], 2)
        self.assertEqual((self.work / "a.txt").read_text(), "hello")
        stored = agent.db.get_messages(agent.session_id)
        self.assertEqual([m["role"] for m in stored], ["system", "user", "assistant", "tool", "assistant", "tool", "assistant"])
        self.assertTrue(agent.db.search("hello"))

    def test_parallel_calls_and_errors(self):
        both = ChatResponse(tool_calls=[ToolCall("a", "todo", {"todos": []}), ToolCall("b", "nonexistent", {}),
                                        ToolCall("c", "read_file", {}), ToolCall("d", "todo", {}, parse_error="invalid JSON")])
        prov = scripted(both, ChatResponse(content="ok"))
        agent = self.agent(prov)
        agent.run_conversation("go")
        tool_msgs = [m for m in agent.messages if m["role"] == "tool"]
        self.assertEqual(len(tool_msgs), 4)
        self.assertIn("not enabled", tool_msgs[1]["content"])
        self.assertIn("missing required argument", tool_msgs[2]["content"])
        self.assertIn("invalid JSON", tool_msgs[3]["content"])

    def test_iteration_budget(self):
        prov = MockProvider(script=[tool_step("todo", {}) for _ in range(3)] + [ChatResponse(content="summary")])
        agent = self.agent(prov, max_turns=3)
        res = agent.run_conversation("loop")
        self.assertEqual(res["final_response"], "summary")
        self.assertIn("iteration budget", prov.calls[-1]["messages"][-1]["content"])
        self.assertIsNone(prov.calls[-1]["tools"])

    def test_dangerous_command_blocked_without_approval(self):
        prov = scripted(tool_step("terminal", {"command": "sudo rm -rf /tmp/x"}), ChatResponse(content="ok"))
        agent = self.agent(prov)
        agent.run_conversation("do it")
        out = [m for m in agent.messages if m["role"] == "tool"][0]["content"]
        self.assertIn("blocked", out)

    def test_approval_prompt_allows(self):
        prov = scripted(tool_step("terminal", {"command": "sudo echo approved-run || echo approved-run"}), ChatResponse(content="ok"))
        agent = self.agent(prov, approval_mode="manual", approval_prompt=lambda c, r: "once")
        agent.run_conversation("do it")
        out = [m for m in agent.messages if m["role"] == "tool"][0]["content"]
        self.assertNotIn("blocked", out)
        self.assertEqual(agent.approvals.log[0]["outcome"], "approved-once")

    def test_memory_snapshot_frozen_and_nudges(self):
        self.cfg.set_path("memory.nudge_interval", 2)
        self.cfg.set_path("skills.creation_nudge_interval", 2)
        prov = MockProvider()
        agent = self.agent(prov)
        agent.run_conversation('call:memory {"action":"add","target":"user","content":"Prefers metric units"}')
        self.assertNotIn("metric units", agent.messages[0]["content"])  # frozen at session start
        agent.run_conversation("call:todo {}")
        agent.run_conversation("hello")
        last_user = [m for m in agent.messages if m["role"] == "user"][-1]["content"]
        self.assertIn("memory tool", last_user)
        self.assertIn("skill_manage", last_user)
        fresh = self.agent(MockProvider())
        self.assertIn("metric units", fresh.messages[0]["content"])

    def test_verify_outputs(self):
        self.cfg.set_path("agent.verify_outputs", True)
        prov = scripted(tool_step("todo", {}), ChatResponse(content="answer 41"), ChatResponse(content="Corrected: answer 42"))
        res = self.agent(prov).run_conversation("q")
        self.assertEqual(res["final_response"], "Corrected: answer 42")

    def test_compression(self):
        self.cfg.set_path("model.context_length", 400)
        self.cfg.set_path("compression.protect_last_n", 4)
        agent = self.agent(MockProvider())
        for i in range(8):
            agent.run_conversation(f"message number {i} " + "x" * 200)
        self.assertTrue(any("Context summary" in str(m.get("content")) for m in agent.messages))
        self.assertLess(len(agent.messages), 17)

    def test_interrupt(self):
        agent = self.agent(MockProvider())
        hold = threading.Event()

        def slow(messages, tools):
            agent.interrupt()
            return tool_step("todo", {})

        agent.provider = MockProvider(script=[slow, ChatResponse(content="never")])
        res = agent.run_conversation("go")
        self.assertEqual(res["final_response"], "[interrupted]")
        hold.set()

    def test_references_expand(self):
        (self.work / "notes.md").write_text("secret plan: ship friday")
        prov = MockProvider()
        self.agent(prov).run_conversation("summarise @notes.md please")
        self.assertIn("ship friday", prov.calls[0]["messages"][-1]["content"])

    def test_resume_session(self):
        a = self.agent(MockProvider())
        a.run_conversation("remember the word banana")
        b = self.agent(MockProvider(), session_id=a.session_id, resume=True)
        self.assertTrue(any("banana" in str(m.get("content")) for m in b.messages))


class ToolSchemaTests(HermesTestCase):
    def test_all_tools_have_valid_schemas(self):
        from hermes.tools import registry

        registry.load_builtin_tools()
        names = set()
        for t in registry.REGISTRY.values():
            s = t.schema()
            self.assertRegex(s["name"], r"^[a-zA-Z0-9_-]{1,64}$")
            self.assertEqual(s["parameters"]["type"], "object")
            self.assertTrue(s["description"])
            json.dumps(s)
            names.add(t.name)
        for expected in ["web_search", "web_extract", "terminal", "process", "read_file", "patch", "browser_navigate",
                         "vision_analyze", "image_generate", "text_to_speech", "memory", "session_search", "skills_list",
                         "skill_view", "skill_manage", "delegate_task", "cronjob", "execute_code", "send_message", "kanban"]:
            self.assertIn(expected, names)


if __name__ == "__main__":
    unittest.main()
