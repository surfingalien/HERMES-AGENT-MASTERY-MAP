"""The agent loop. Agent = model + tools + context (Core building block).

``How Hermes works``: INPUT (message or schedule) -> REASON (model + context)
-> ACT (tools, skills, subagents) -> LEARN (memory + skill improvement).
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import re
import sys
import threading
import time
from typing import Any, Callable

from hermes import config as hconfig
from hermes.context import compressor
from hermes.context.prompt_builder import build_system_prompt
from hermes.context.references import expand_references
from hermes.hooks import BUS, emit
from hermes.memory.store import MEMORY_PROVIDERS, MemoryStore
from hermes.power.insights import estimate_cost
from hermes.providers import ChatResponse, Provider, ProviderError, Usage, choose_model_for_turn, create_provider
from hermes.security.approval import ApprovalManager, cli_prompt
from hermes.security.checkpoints import CheckpointManager
from hermes.security.redact import redact
from hermes.sessions.db import SessionDB, new_session_id
from hermes.skills.manager import SkillManager
from hermes.tools import registry
from hermes.tools.registry import ToolContext

log = logging.getLogger("hermes.agent")
_MEDIA_RE = re.compile(r"MEDIA:(\S+)")
_mcp_lock = threading.Lock()
_mcp_loaded = False
_plugins_loaded = False


def _bootstrap(cfg: dict) -> None:
    """Load builtin tools, plugins, hooks and MCP servers once per process."""
    global _mcp_loaded, _plugins_loaded
    registry.load_builtin_tools()
    if not _plugins_loaded:
        _plugins_loaded = True
        try:
            from hermes.plugins import load_plugins

            load_plugins(cfg)
            BUS.load_directory_hooks()
        except Exception:
            log.exception("plugin/hook bootstrap failed")
    if cfg.get("mcp_servers") and not _mcp_loaded:
        with _mcp_lock:
            if not _mcp_loaded:
                _mcp_loaded = True
                try:
                    from hermes.mcp.client import load_mcp_servers

                    load_mcp_servers(cfg)
                except Exception:
                    log.exception("MCP bootstrap failed")


class AIAgent:
    def __init__(
        self,
        config: dict | None = None,
        provider: Provider | None = None,
        model: str | None = None,
        platform: str = "cli",
        toolsets: list[str] | None = None,
        session_id: str | None = None,
        resume: bool = False,
        max_turns: int | None = None,
        parent: "AIAgent | None" = None,
        depth: int = 0,
        skip_memory: bool = False,
        quiet: bool = False,
        cwd: str | None = None,
        task_id: str | None = None,
        approval_mode: str | None = None,
        approval_prompt: Callable[[str, str], str] | None = None,
        clarify_fn: Callable[[str, list[str] | None], str] | None = None,
        user_id: str = "",
        chat_id: str = "",
        callbacks: dict[str, Callable] | None = None,
        personality: str = "",
        ephemeral_system: str = "",
        db: SessionDB | None = None,
        persist: bool = True,
    ):
        self.cfg = config if config is not None else hconfig.load_config()
        _bootstrap(self.cfg)
        self.platform = platform
        self.quiet = quiet
        self.parent = parent
        self.depth = depth
        self.cwd = os.path.abspath(os.path.expanduser(cwd or (self.cfg.get("terminal", {}) or {}).get("cwd") or os.getcwd()))
        self.provider = provider or create_provider(self.cfg, model)
        self.model = model or (self.provider.model if self.provider else "") or (self.cfg.get("model", {}) or {}).get("default", "")
        if model and ":" in model:
            self.model = model.split(":", 1)[1]
        acfg = self.cfg.get("agent", {}) or {}
        self.max_turns = int(max_turns or acfg.get("max_turns", 60))
        self.toolsets = toolsets or (self.cfg.get("platform_toolsets", {}) or {}).get(platform, ["hermes-cli"])
        self.callbacks = callbacks or {}
        self.personality = personality
        self.ephemeral_system = ephemeral_system
        self.skip_memory = skip_memory
        self.user_id, self.chat_id = user_id, chat_id
        self.task_id = task_id or ""
        self.interrupt_event = threading.Event()
        self.todos: list[dict] = []
        self.skills_used: set[str] = set()
        self.usage = Usage()
        self.cost_usd = 0.0
        self.tool_calls_total = 0
        self._tool_calls_since_skill = 0
        self._user_turns = 0
        self._pending_skill_nudge = False
        self.persist = persist
        self.db = db or (parent.db if parent else None) or (SessionDB() if persist else None)

        # Safety layer
        amode = approval_mode or (self.cfg.get("approvals", {}) or {}).get("mode", "manual")
        prompt_fn = approval_prompt
        if prompt_fn is None and amode != "deny-dangerous" and platform == "cli" and sys.stdin.isatty():
            prompt_fn = cli_prompt
        if parent is not None and prompt_fn is None:
            prompt_fn = parent.approvals.prompt
        self.approvals = ApprovalManager(
            mode="manual" if amode == "deny-dangerous" else amode,
            allowlist=list((self.cfg.get("approvals", {}) or {}).get("allowlist", [])),
            prompt=prompt_fn,
            on_always=self._persist_allow,
            judge=self._judge_command,
        )
        ccfg = self.cfg.get("checkpoints", {}) or {}
        self.checkpoints = CheckpointManager(bool(ccfg.get("enabled", True)), int(ccfg.get("max_snapshots", 50)))
        self.clarify_fn = clarify_fn

        # Memory & skills (frozen at session start)
        self.memory = MemoryStore(self.cfg)
        mcfg = self.cfg.get("memory", {}) or {}
        self.memory_snapshot = "" if skip_memory else self.memory.snapshot(
            include_user=mcfg.get("user_profile_enabled", True), include_memory=mcfg.get("memory_enabled", True))
        self.memory_provider = None
        prov = mcfg.get("provider", "builtin")
        if prov and prov != "builtin" and prov in MEMORY_PROVIDERS and not skip_memory:
            mp = MEMORY_PROVIDERS[prov]
            self.memory_provider = mp(self.db) if isinstance(mp, type) else mp
        self.skills = SkillManager(self.cfg)
        self.tools = self._resolve_tools()

        # Session
        self.session_id = session_id or new_session_id()
        self.task_id = self.task_id or self.session_id  # stable key for terminal/browser state
        self.messages: list[dict] = []
        resumed = []
        if resume and self.db and session_id:
            resumed = self.db.get_messages(session_id)
        if self.db:
            self.db.create_session(self.session_id, source=platform, model=self.model, user_id=user_id, chat_id=chat_id,
                                   parent_session_id=parent.session_id if parent else None)
        if resumed and resumed[0].get("role") == "system":
            self.messages = resumed
        else:
            self.messages = [{"role": "system", "content": self._system_prompt()}] + resumed
            if self.db and not resumed:
                self.db.add_message(self.session_id, self.messages[0])
        emit("session:start", session_id=self.session_id, platform=platform, resumed=bool(resumed))

    # ---------------------------------------------------------------- setup helpers
    def _resolve_tools(self) -> list[registry.Tool]:
        disabled = list(self.cfg.get("disabled_tools", []) or [])
        if self.skip_memory:
            disabled.append("memory")
        return registry.resolve_tools(self.toolsets, disabled)

    def tool_schemas(self) -> list[dict]:
        return [t.schema() for t in self.tools]

    def _system_prompt(self) -> str:
        skills_index = "" if "skills" not in registry.expand_toolsets(self.toolsets) else self.skills.index_for_prompt()
        return build_system_prompt(
            self.cfg, platform=self.platform, cwd=self.cwd, model=self.model, memory_snapshot=self.memory_snapshot,
            skills_index=skills_index, personality=self.personality, extra=self.ephemeral_system,
            skip_context_files=self.platform in ("api",),
        )

    def _persist_allow(self, command: str) -> None:
        try:
            cfg = hconfig.load_config(load_env=False)
            allow = cfg.setdefault("approvals", {}).setdefault("allowlist", [])
            if command not in allow:
                allow.append(command)
                hconfig.save_config(cfg)
        except Exception:
            log.exception("could not persist approval allowlist")

    def _judge_command(self, command: str, reason: str) -> bool:
        """Smart approvals: ask the model whether a flagged command is clearly safe in context."""
        recent = [m for m in self.messages if m.get("role") == "user"][-1:]
        task = str(recent[0].get("content"))[:1500] if recent else ""
        resp = self.provider.chat([{"role": "user", "content": (
            "You are a security reviewer for an autonomous agent. A command was flagged as risky "
            f"({reason}).\nUser's request: {task}\nCommand: {command}\n\nAnswer SAFE only if the command is clearly "
            "what the user asked for, cannot destroy data or credentials, and cannot affect systems beyond the task. "
            "Otherwise answer RISKY. Reply with one word.")}], tools=None, max_tokens=2000)
        self.record_usage(resp)
        return resp.content.strip().upper().startswith("SAFE")

    def _tool_context(self) -> ToolContext:
        return ToolContext(
            config=self.cfg, agent=self, session_id=self.session_id, platform=self.platform, cwd=self.cwd,
            task_id=self.task_id, depth=self.depth, user_id=self.user_id, chat_id=self.chat_id,
            approve=self.approvals.check, clarify=self.clarify_fn,
            checkpoint=lambda p: self.checkpoints.snapshot_for(p, f"before edit: {os.path.basename(p)}"),
            interrupt=self.interrupt_event,
            extra={"memory_read_only": self.platform in ((self.cfg.get("memory", {}) or {}).get("read_only_platforms") or [])},
        )

    # ---------------------------------------------------------------- bookkeeping
    def record_usage(self, resp: ChatResponse) -> None:
        self.usage.add(resp.usage)
        cost = estimate_cost(resp.model or self.model, resp.usage.input_tokens, resp.usage.output_tokens,
                             resp.usage.cache_read_tokens, self.cfg.get("pricing"))
        self.cost_usd += cost
        if self.db:
            self.db.add_usage(self.session_id, resp.usage.input_tokens, resp.usage.output_tokens, resp.usage.cache_read_tokens,
                              cost, resp.model or "")

    def _append(self, msg: dict) -> None:
        self.messages.append(msg)
        if self.db:
            self.db.add_message(self.session_id, msg)

    def on_skill_changed(self) -> None:
        self._tool_calls_since_skill = 0

    def interrupt(self) -> None:
        self.interrupt_event.set()

    def _cb(self, name: str, *args: Any) -> None:
        fn = self.callbacks.get(name)
        if fn:
            try:
                fn(*args)
            except Exception:
                log.exception("callback %s failed", name)

    # ---------------------------------------------------------------- tool execution
    def _execute_one(self, call, ctx: ToolContext) -> dict:
        name, args = call.name, call.arguments
        start = time.time()
        self._cb("on_tool_start", name, args)
        vetoes = [r for r in emit("tool:pre", tool=name, args=args, session_id=self.session_id) if isinstance(r, dict) and r.get("block")]
        if call.parse_error:
            out, is_err = json.dumps({"error": call.parse_error + " - resend the call with valid arguments"}), True
        elif vetoes:
            out, is_err = json.dumps({"error": f"blocked by hook: {vetoes[0]['block']}"}), True
        elif name not in {t.name for t in self.tools}:
            out, is_err = json.dumps({"error": f"tool '{name}' is not enabled in this session"}), True
        else:
            out, is_err = registry.run_tool(name, args, ctx)
        if (self.cfg.get("security", {}) or {}).get("redact_secrets", True):
            out = redact(out)
        limit = int((self.cfg.get("agent", {}) or {}).get("tool_output_limit", 20000))
        if len(out) > limit:
            out = out[: limit // 2] + f"\n...[{len(out) - limit} chars truncated]...\n" + out[-limit // 2 :]
        ms = int((time.time() - start) * 1000)
        if self.db:
            self.db.record_tool(self.session_id, name, not is_err, ms)
        emit("tool:post", tool=name, args=args, result=out[:2000], is_error=is_err, session_id=self.session_id)
        self._cb("on_tool_end", name, out, is_err, ms)
        msg = {"role": "tool", "tool_call_id": call.id, "name": name, "content": out}
        if is_err:
            msg["_is_error"] = True
        return msg

    def _execute_tools(self, calls) -> list[dict]:
        ctx = self._tool_context()
        parallel = (self.cfg.get("agent", {}) or {}).get("parallel_tools", True) and len(calls) > 1 and all(
            registry.REGISTRY.get(c.name) is not None and registry.REGISTRY[c.name].parallel_safe for c in calls)
        if parallel:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(calls))) as pool:
                return list(pool.map(lambda c: self._execute_one(c, ctx), calls))
        results = []
        for c in calls:
            if self.interrupt_event.is_set():
                results.append({"role": "tool", "tool_call_id": c.id, "name": c.name, "content": '{"error": "interrupted by user"}', "_is_error": True})
                continue
            results.append(self._execute_one(c, ctx))
        return results

    # ---------------------------------------------------------------- main loop
    def _decorate_user_message(self, text: str) -> str:
        notes = []
        mcfg = self.cfg.get("memory", {}) or {}
        nudge = int(mcfg.get("nudge_interval", 10) or 0)
        if nudge and not self.skip_memory and self._user_turns and self._user_turns % nudge == 0:
            notes.append("[System reminder: if you have learned durable facts about the user or environment in this "
                         "conversation, save them with the memory tool.]")
        if self._pending_skill_nudge:
            notes.append("[System reminder: the previous task needed many tool calls. If it is a procedure worth "
                         "repeating, save it as a skill with skill_manage(create); if a skill you used was wrong, patch it.]")
            self._pending_skill_nudge = False
        if self.memory_provider:
            try:
                recalled = self.memory_provider.prefetch(text, self.session_id)
                if recalled:
                    notes.append(f"<recalled-context>\n{recalled}\n</recalled-context>")
            except Exception:
                log.exception("memory provider prefetch failed")
        return text + ("\n\n" + "\n".join(notes) if notes else "")

    def run_conversation(self, user_message: str | list, images: list[str] | None = None) -> dict:
        """Run one user turn to completion. Returns a result dict."""
        self.interrupt_event.clear()
        self.checkpoints.new_turn()
        emit("agent:start", session_id=self.session_id, message=str(user_message)[:500], platform=self.platform)
        if isinstance(user_message, str):
            text = user_message
            if self.platform in ("cli", "acp", "tui"):
                text, _ = expand_references(text, self.cwd)
            text = self._decorate_user_message(text)
            content: str | list = text
            if images:
                from hermes.tools.media import to_image_url

                content = [{"type": "text", "text": text}] + [{"type": "image_url", "image_url": {"url": to_image_url(i)}} for i in images]
        else:
            content = user_message
        self._user_turns += 1
        self._append({"role": "user", "content": content})
        if self.db and self._user_turns == 1:
            sess = self.db.get_session(self.session_id)
            if sess and not sess.get("title"):
                self.db.set_title(self.session_id, re.sub(r"\s+", " ", str(user_message if isinstance(user_message, str) else "multimodal"))[:80])

        turn_model = choose_model_for_turn(self.cfg, text if isinstance(user_message, str) else "", self.model)
        schemas = self.tool_schemas()
        max_tokens = int((self.cfg.get("model", {}) or {}).get("max_tokens", 16000))
        final, error, turns, calls_this_run = "", "", 0, 0
        media: list[str] = []
        try:
            while turns < self.max_turns:
                if self.interrupt_event.is_set():
                    final = "[interrupted]"
                    break
                turns += 1
                if compressor.should_compress(self.messages, self.cfg):
                    self.compress()
                emit("llm:pre", session_id=self.session_id, turn=turns)
                resp = self.provider.chat(self.messages, tools=schemas or None, model=turn_model if turn_model != self.model else None,
                                          max_tokens=max_tokens)
                self.record_usage(resp)
                emit("llm:post", session_id=self.session_id, turn=turns, tool_calls=len(resp.tool_calls))
                if resp.reasoning:
                    self._cb("on_thinking", resp.reasoning)
                self._append(resp.to_message())
                if not resp.tool_calls:
                    final = resp.content
                    break
                if resp.content:
                    self._cb("on_interim", resp.content)
                results = self._execute_tools(resp.tool_calls)
                for r in results:
                    self._append(r)
                    media += _MEDIA_RE.findall(r.get("content") or "")
                calls_this_run += len(resp.tool_calls)
                self.tool_calls_total += len(resp.tool_calls)
                self._tool_calls_since_skill += len(resp.tool_calls)
                emit("agent:step", session_id=self.session_id, turn=turns, tools=[c.name for c in resp.tool_calls])
            else:
                final = self._final_summary()
        except ProviderError as exc:
            error = str(exc)
            final = f"⚠️ Model provider error: {exc}"
            log.error("provider error: %s", exc)
        if not error and (self.cfg.get("agent", {}) or {}).get("verify_outputs") and calls_this_run and final:
            final = self._verify(final)
        skill_nudge = int((self.cfg.get("skills", {}) or {}).get("creation_nudge_interval", 15) or 0)
        if skill_nudge and self._tool_calls_since_skill >= skill_nudge and "skills" in registry.expand_toolsets(self.toolsets):
            self._pending_skill_nudge = True
            self._tool_calls_since_skill = 0
        if self.memory_provider and final:
            try:
                self.memory_provider.sync_turn(str(user_message)[:4000], final[:4000], self.session_id)
            except Exception:
                log.exception("memory provider sync failed")
        media += [m for m in _MEDIA_RE.findall(final or "") if m not in media]
        emit("agent:end", session_id=self.session_id, response=(final or "")[:2000], turns=turns, tool_calls=calls_this_run)
        self._cb("on_final", final)
        return {"final_response": final, "messages": self.messages, "turns": turns, "tool_calls": calls_this_run,
                "usage": self.usage, "cost_usd": self.cost_usd, "session_id": self.session_id, "error": error,
                "media": [m for m in media if os.path.exists(m)]}

    def _final_summary(self) -> str:
        self._append({"role": "user", "content": "[System: iteration budget reached. Stop calling tools and give your best "
                                                 "final answer now: what is done, what remains, and next steps.]"})
        resp = self.provider.chat(self.messages, tools=None, max_tokens=4000)
        self.record_usage(resp)
        self._append(resp.to_message())
        return resp.content

    def _verify(self, answer: str) -> str:
        """Optional self-check pass (#86 verify outputs)."""
        probe = self.messages + [{"role": "user", "content": (
            "[System: verification pass] Check your last answer against the tool results above. If it contains claims not "
            "supported by the evidence, missed requirements, or errors, reply with a corrected final answer. If it is "
            "correct, reply with exactly: VERIFIED")}]
        resp = self.provider.chat(probe, tools=None, max_tokens=8000)
        self.record_usage(resp)
        if resp.content.strip().upper().startswith("VERIFIED") or not resp.content.strip():
            return answer
        self._append({"role": "assistant", "content": resp.content})
        return resp.content

    # ---------------------------------------------------------------- session controls
    def compress(self, focus: str = "") -> dict:
        new, info = compressor.compress(self.messages, self.provider, self.cfg, focus)
        if info.get("compressed"):
            self.messages = new
            if isinstance(info.get("usage"), Usage):
                self.usage.add(info["usage"])
            if self.db:
                # Continue in a child session so the full history stays searchable.
                old = self.session_id
                self.session_id = new_session_id()
                self.db.create_session(self.session_id, source=self.platform, model=self.model, parent_session_id=old,
                                       title=f"(continued) {(self.db.get_session(old) or {}).get('title') or ''}"[:80])
                for m in self.messages:
                    self.db.add_message(self.session_id, m)
        return info

    def reset(self) -> None:
        emit("session:reset", session_id=self.session_id)
        self.close(cleanup_env=False)
        self.session_id = new_session_id()
        self.memory_snapshot = "" if self.skip_memory else self.memory.snapshot()
        self.tools = self._resolve_tools()
        self.messages = [{"role": "system", "content": self._system_prompt()}]
        self._user_turns = 0
        if self.db:
            self.db.create_session(self.session_id, source=self.platform, model=self.model, user_id=self.user_id, chat_id=self.chat_id)
            self.db.add_message(self.session_id, self.messages[0])

    def undo(self) -> str | None:
        """Remove the last user turn and everything after it. Returns that user text."""
        for i in range(len(self.messages) - 1, 0, -1):
            if self.messages[i].get("role") == "user":
                text = self.messages[i].get("content")
                self.messages = self.messages[:i]
                return text if isinstance(text, str) else None
        return None

    def close(self, cleanup_env: bool = True) -> None:
        if self.memory_provider:
            try:
                self.memory_provider.on_session_end(self.session_id, self.messages)
            except Exception:
                log.exception("memory provider on_session_end failed")
        if self.db:
            self.db.end_session(self.session_id)
        emit("session:end", session_id=self.session_id)
        if cleanup_env:
            from hermes.tools.terminal import cleanup_env as _cleanup
            from hermes.tools.browser import close_browser

            key = self.task_id or self.session_id
            _cleanup(key)
            close_browser(key)
