"""ACP editor integration (#97) - Agent Client Protocol over stdio (JSON-RPC 2.0).

Editors such as Zed launch ``hermes acp`` and talk JSON-RPC over stdin/stdout:

    initialize            -> capabilities
    session/new           -> {sessionId}
    session/load          -> resume a stored session
    session/prompt        -> streams session/update notifications, returns {stopReason}
    session/cancel        -> interrupts the running turn

Tool calls are reported as ``tool_call`` / ``tool_call_update`` updates and the
answer as ``agent_message_chunk`` updates.
"""

from __future__ import annotations

import json
import sys
import threading
from typing import Any

PROTOCOL_VERSION = 1


class ACPServer:
    def __init__(self, cfg: dict, stdin=None, stdout=None, agent_factory=None):
        self.cfg = cfg
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout
        self.sessions: dict[str, Any] = {}
        self.write_lock = threading.Lock()
        if agent_factory is None:
            from hermes.agent import AIAgent

            agent_factory = AIAgent
        self.agent_factory = agent_factory

    def send(self, msg: dict) -> None:
        with self.write_lock:
            self.stdout.write(json.dumps(msg) + "\n")
            self.stdout.flush()

    def notify(self, method: str, params: dict) -> None:
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    def update(self, sid: str, update: dict) -> None:
        self.notify("session/update", {"sessionId": sid, "update": update})

    def _new_agent(self, sid: str | None, cwd: str | None, resume: bool = False):
        sid_box: dict[str, str] = {}
        call_ids: dict[str, list[str]] = {}
        counter = iter(range(1, 10**9))

        def on_start(name, args):
            call_id = f"call_{next(counter)}"
            call_ids.setdefault(name, []).append(call_id)
            self.update(sid_box["id"], {"sessionUpdate": "tool_call", "toolCallId": call_id, "title": name,
                                        "kind": "execute" if name in ("terminal", "process") else "edit" if name in ("write_file", "patch") else "other",
                                        "status": "in_progress", "rawInput": args})

        def on_end(name, out, is_err, ms):
            pending = call_ids.get(name) or [name]
            call_id = pending.pop(0)
            self.update(sid_box["id"], {"sessionUpdate": "tool_call_update", "toolCallId": call_id, "status": "failed" if is_err else "completed",
                                        "content": [{"type": "content", "content": {"type": "text", "text": out[:4000]}}]})

        def on_thinking(text):
            self.update(sid_box["id"], {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": text}})

        agent = self.agent_factory(config=self.cfg, platform="acp", session_id=sid, resume=resume, cwd=cwd,
                                   approval_mode="deny-dangerous", callbacks={"on_tool_start": on_start, "on_tool_end": on_end, "on_thinking": on_thinking})
        sid_box["id"] = agent.session_id
        return agent

    def handle(self, msg: dict) -> dict | None:
        method, params, mid = msg.get("method"), msg.get("params") or {}, msg.get("id")
        try:
            if method == "initialize":
                result = {"protocolVersion": PROTOCOL_VERSION, "agentCapabilities": {"loadSession": True, "promptCapabilities": {"image": True, "embeddedContext": True}},
                          "authMethods": [], "agentInfo": {"name": "hermes-agent-replica", "version": "0.1.0"}}
            elif method == "session/new":
                agent = self._new_agent(None, params.get("cwd"))
                self.sessions[agent.session_id] = agent
                result = {"sessionId": agent.session_id}
            elif method == "session/load":
                agent = self._new_agent(params["sessionId"], params.get("cwd"), resume=True)
                self.sessions[agent.session_id] = agent
                result = None
            elif method == "session/prompt":
                sid = params["sessionId"]
                agent = self.sessions.get(sid)
                if agent is None:
                    raise KeyError(f"unknown session {sid}")
                parts = []
                for block in params.get("prompt", []):
                    if block.get("type") == "text":
                        parts.append(block.get("text", ""))
                    elif block.get("type") == "resource":
                        res = block.get("resource") or {}
                        parts.append(f"<attachment uri=\"{res.get('uri')}\">\n{res.get('text', '')}\n</attachment>")
                    elif block.get("type") == "resource_link":
                        parts.append(f"(see {block.get('uri')})")
                res = agent.run_conversation("\n\n".join(parts))
                if res.get("final_response"):
                    self.update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": res["final_response"]}})
                result = {"stopReason": "cancelled" if res.get("final_response") == "[interrupted]" else "end_turn"}
            elif method == "session/cancel":
                agent = self.sessions.get(params.get("sessionId"))
                if agent:
                    agent.interrupt()
                return None
            else:
                if mid is None:
                    return None
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
        except Exception as exc:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32000, "message": str(exc)}} if mid is not None else None
        return {"jsonrpc": "2.0", "id": mid, "result": result} if mid is not None else None

    def serve(self) -> None:
        for line in self.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("method") == "session/prompt":
                threading.Thread(target=lambda m=msg: self._reply(self.handle(m)), daemon=True).start()
            else:
                self._reply(self.handle(msg))

    def _reply(self, resp: dict | None) -> None:
        if resp is not None:
            self.send(resp)
