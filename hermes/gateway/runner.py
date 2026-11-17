"""GatewayRunner: one process serving every connected messaging platform (#41-#50).

Per chat it keeps a persistent agent session, enforces user authorization
(allowlists + DM pairing codes), routes slash commands, relays approval
prompts into the chat, transcribes voice notes, sends media and runs the cron
ticker so scheduled jobs fire while the gateway is up.
"""

from __future__ import annotations

import json
import logging
import random
import string
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from hermes import config as hconfig
from hermes.gateway import delivery
from hermes.gateway.base import MessageEvent, PlatformAdapter, chunk_text
from hermes.hooks import BUS, emit

log = logging.getLogger("hermes.gateway")

HELP = """Commands:
/new or /reset - start a fresh conversation
/stop - interrupt the current task
/status - session info  ·  /usage - tokens & cost
/model [provider:model] - show or switch model
/compress [focus] - summarise the conversation to save context
/undo - drop the last exchange  ·  /retry - redo the last message
/approve [session|always] · /deny - answer an approval prompt
/sethome - make this chat the home channel for deliveries
/skills - list skills  ·  /<skill-name> [request] - run a skill
/personality <name> - concise, technical, teacher, creative, skeptic, helpful
/voice on|off - reply with voice messages
/help - this message"""


def _pairing_path():
    return hconfig.path("gateway", "pairing.json")


def load_pairing() -> dict:
    p = _pairing_path()
    return json.loads(p.read_text()) if p.exists() else {"approved": {}, "pending": {}}


def save_pairing(data: dict) -> None:
    p = _pairing_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))


def approve_pairing(platform: str, code: str) -> dict | None:
    data = load_pairing()
    entry = data["pending"].pop(f"{platform}:{code.upper()}", None)
    if not entry:
        return None
    data["approved"].setdefault(platform, [])
    if entry["user_id"] not in data["approved"][platform]:
        data["approved"][platform].append(entry["user_id"])
    save_pairing(data)
    return entry


def revoke_user(platform: str, user_id: str) -> bool:
    data = load_pairing()
    users = data["approved"].get(platform, [])
    if user_id in users:
        users.remove(user_id)
        save_pairing(data)
        return True
    return False


@dataclass
class ChatState:
    key: str
    session_id: str = ""
    agent: Any = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    busy: bool = False
    queue: list[MessageEvent] = field(default_factory=list)
    pending_approval: dict | None = None
    pending_clarify: dict | None = None
    voice_replies: bool = False
    personality: str = ""
    model: str = ""
    last_user_text: str = ""


class GatewayRunner:
    def __init__(self, cfg: dict | None = None, adapters: dict[str, PlatformAdapter] | None = None, agent_factory=None):
        self.cfg = cfg or hconfig.load_config()
        self.gcfg = self.cfg.get("gateway", {}) or {}
        self.adapters: dict[str, PlatformAdapter] = adapters or {}
        self.chats: dict[str, ChatState] = {}
        self.sessions_path = hconfig.path("gateway", "sessions.json")
        self.session_map: dict[str, str] = json.loads(self.sessions_path.read_text()) if self.sessions_path.exists() else {}
        self.agent_factory = agent_factory
        self.threads: list[threading.Thread] = []
        self.ticker = None
        self.http: ThreadingHTTPServer | None = None
        self.lock = threading.Lock()

    # ---------------------------------------------------------------- setup
    def build_adapters(self) -> None:
        from hermes.gateway.platforms import ADAPTERS

        pcfgs = self.gcfg.get("platforms", {}) or {}
        for name, cls in ADAPTERS.items():
            pcfg = pcfgs.get(name, {}) or {}
            if name not in self.adapters and cls.configured(pcfg):
                self.adapters[name] = cls(pcfg, self.handle_event)
        for a in self.adapters.values():
            a.on_message = self.handle_event

    def start(self, block: bool = True, cron: bool = True) -> None:
        self.build_adapters()
        if not self.adapters:
            raise RuntimeError("no messaging platforms configured - run `hermes gateway setup`")
        delivery.RUNNING.update(self.adapters)
        for name, a in self.adapters.items():
            t = threading.Thread(target=self._run_adapter, args=(name, a), daemon=True, name=f"adapter-{name}")
            t.start()
            self.threads.append(t)
        self._start_http()
        if cron:
            from hermes.cron.jobs import CronTicker

            self.ticker = CronTicker(self.cfg, self.deliver, int(self.gcfg.get("cron_tick_seconds", 60)))
            self.ticker.start()
        BUS.load_directory_hooks()
        emit("gateway:startup", platforms=list(self.adapters))
        log.info("gateway started: %s", ", ".join(self.adapters))
        if block:
            try:
                while any(t.is_alive() for t in self.threads):
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
            finally:
                self.stop()

    def _run_adapter(self, name: str, adapter: PlatformAdapter) -> None:
        backoff = 2
        while not adapter.stop_event.is_set():
            try:
                adapter.start()
                if adapter.stop_event.is_set():
                    return
            except Exception:
                log.exception("adapter %s crashed; restarting in %ss", name, backoff)
            adapter.stop_event.wait(backoff)
            backoff = min(backoff * 2, 300)

    def _start_http(self) -> None:
        webhook_adapters = {n: a for n, a in self.adapters.items() if getattr(a, "uses_webhook", False)}
        if not webhook_adapters:
            return
        class Handler(BaseHTTPRequestHandler):
            def _dispatch(self):
                parsed = urllib.parse.urlparse(self.path)
                parts = parsed.path.lstrip("/").split("/", 1)
                adapter = webhook_adapters.get(parts[0].replace("-", "_"))
                sub = "/" + (parts[1] if len(parts) > 1 else "") + (f"?{parsed.query}" if parsed.query else "")
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)) if self.command == "POST" else b""
                if adapter is None:
                    status, payload = (200, "ok") if parsed.path in ("/", "/health") else (404, "not found")
                else:
                    try:
                        status, payload = adapter.handle_http(sub, dict(self.headers), body, method=self.command)
                    except Exception:
                        log.exception("webhook handler failed")
                        status, payload = 500, "error"
                data = json.dumps(payload).encode() if isinstance(payload, (dict, list)) else str(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json" if isinstance(payload, (dict, list)) else "text/plain")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _dispatch

            def log_message(self, *args):
                pass

        host = self.gcfg.get("webhook_host", "0.0.0.0")
        port = int(self.gcfg.get("webhook_port", 8650))
        self.http = ThreadingHTTPServer((host, port), Handler)
        threading.Thread(target=self.http.serve_forever, daemon=True, name="gateway-http").start()
        log.info("webhook server on %s:%s for %s", host, port, ", ".join(webhook_adapters))

    def stop(self) -> None:
        for a in self.adapters.values():
            a.stop()
        if self.ticker:
            self.ticker.stop()
        if self.http:
            self.http.shutdown()
        for st in self.chats.values():
            if st.agent:
                st.agent.close()
        for n in list(self.adapters):
            delivery.RUNNING.pop(n, None)

    def deliver(self, target: str, text: str, origin: dict) -> None:
        delivery.deliver(target, text, origin, self.cfg)

    # ---------------------------------------------------------------- authorization (#29, pairing)
    def is_authorized(self, ev: MessageEvent) -> bool:
        if self.gcfg.get("allow_all_users"):
            return True
        adapter = self.adapters.get(ev.platform)
        allowed = adapter.allowed_users() if adapter else set()
        approved = set(load_pairing()["approved"].get(ev.platform, []))
        uid = str(ev.user_id).lower()
        return uid in allowed or str(ev.user_id) in approved or (ev.user_name and ev.user_name.lower() in allowed)

    def _pairing_reply(self, ev: MessageEvent) -> str | None:
        if ev.is_group or not self.gcfg.get("dm_pairing", True):
            return None
        data = load_pairing()
        for key, entry in data["pending"].items():
            if entry["user_id"] == str(ev.user_id) and key.startswith(ev.platform + ":"):
                code = key.split(":", 1)[1]
                break
        else:
            code = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
            data["pending"][f"{ev.platform}:{code}"] = {"user_id": str(ev.user_id), "user_name": ev.user_name,
                                                        "chat_id": ev.chat_id, "requested_at": time.time()}
            save_pairing(data)
        return (f"👋 I don't know you yet. Pairing code: {code}\n"
                f"Ask the owner to run:  hermes pairing approve {ev.platform} {code}")

    # ---------------------------------------------------------------- inbound
    def _state(self, key: str) -> ChatState:
        with self.lock:
            if key not in self.chats:
                self.chats[key] = ChatState(key, session_id=self.session_map.get(key, ""))
            return self.chats[key]

    def _reply(self, ev: MessageEvent, text: str) -> None:
        adapter = self.adapters.get(ev.platform)
        if not adapter or not text:
            return
        for chunk in chunk_text(text, adapter.max_len):
            try:
                adapter.send(ev.chat_id, chunk, ev.thread_id)
            except Exception:
                log.exception("send failed on %s", ev.platform)

    def handle_event(self, ev: MessageEvent) -> None:
        """Entry point for every adapter. Never blocks the adapter's receive loop for long."""
        if ev.is_group and self.gcfg.get("group_require_mention", True) and not ev.mentioned:
            return
        if not self.is_authorized(ev):
            reply = self._pairing_reply(ev)
            if reply:
                self._reply(ev, reply)
            return
        st = self._state(ev.session_key)
        text = (ev.text or "").strip()
        # Answers to pending approval / clarify prompts are consumed immediately.
        if st.pending_approval and (text.startswith("/approve") or text.startswith("/deny")):
            choice = "deny" if text.startswith("/deny") else ({"session": "session", "always": "always"}.get(text.split()[1] if len(text.split()) > 1 else "", "once"))
            st.pending_approval["answer"] = choice
            st.pending_approval["event"].set()
            return
        if st.pending_clarify and not text.startswith("/"):
            st.pending_clarify["answer"] = text
            st.pending_clarify["event"].set()
            return
        if text.startswith("/"):
            if self.handle_command(ev, st, text):
                return
        if st.busy:
            st.queue.append(ev)
            if st.agent:
                st.agent.interrupt()
            self._reply(ev, "⏸ Got it - wrapping up the current step and then handling your new message.")
            return
        threading.Thread(target=self._process, args=(ev, st), daemon=True).start()

    def _get_agent(self, ev: MessageEvent, st: ChatState):
        if st.agent is not None:
            return st.agent
        from hermes.agent import AIAgent

        factory = self.agent_factory or AIAgent
        resume = bool(st.session_id)
        agent = factory(
            config=self.cfg, platform=ev.platform, session_id=st.session_id or None, resume=resume,
            model=st.model or None, user_id=str(ev.user_id), chat_id=ev.chat_id, personality=st.personality,
            approval_prompt=lambda cmd, why: self._ask_approval(ev, st, cmd, why),
            clarify_fn=lambda q, choices: self._ask_clarify(ev, st, q, choices),
        )
        st.agent = agent
        st.session_id = agent.session_id
        self.session_map[st.key] = agent.session_id
        self._save_sessions()
        return agent

    def _save_sessions(self) -> None:
        self.sessions_path.parent.mkdir(parents=True, exist_ok=True)
        self.sessions_path.write_text(json.dumps(self.session_map, indent=2))

    def _ask_approval(self, ev: MessageEvent, st: ChatState, command: str, reason: str) -> str:
        box = {"event": threading.Event(), "answer": "deny"}
        st.pending_approval = box
        self._reply(ev, f"⚠️ Approval needed ({reason}):\n`{command[:500]}`\n\nReply /approve, /approve session, "
                        f"/approve always, or /deny.")
        box["event"].wait(int((self.cfg.get("approvals", {}) or {}).get("timeout", 300)))
        st.pending_approval = None
        return box["answer"]

    def _ask_clarify(self, ev: MessageEvent, st: ChatState, question: str, choices) -> str:
        box = {"event": threading.Event(), "answer": ""}
        st.pending_clarify = box
        opts = ("\n" + "\n".join(f"{i}. {c}" for i, c in enumerate(choices, 1))) if choices else ""
        self._reply(ev, f"❓ {question}{opts}")
        if not box["event"].wait(600):
            st.pending_clarify = None
            return "(no answer - use your best judgement)"
        st.pending_clarify = None
        ans = box["answer"]
        if choices and ans.isdigit() and 1 <= int(ans) <= len(choices):
            ans = choices[int(ans) - 1]
        return ans

    def _prepare_input(self, ev: MessageEvent) -> tuple[str, list[str]]:
        text, images = ev.text or "", []
        for path in ev.attachments:
            low = path.lower()
            if ev.media_type == "voice" or low.endswith((".ogg", ".oga", ".mp3", ".m4a", ".wav", ".opus")):
                try:
                    from hermes.tools.media import transcribe

                    transcript = transcribe(path, self.cfg)
                    text = f"[Voice message transcript]: {transcript}\n{text}".strip()
                except Exception as exc:
                    text = f"[Voice message received but could not be transcribed: {exc}]\n{text}".strip()
            elif low.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
                images.append(path)
            else:
                text = f"{text}\n[User sent a file saved at {path} - use read_file to inspect it]".strip()
        return text, images

    def _process(self, ev: MessageEvent, st: ChatState) -> None:
        with st.lock:
            st.busy = True
            try:
                adapter = self.adapters.get(ev.platform)
                stop_typing = threading.Event()
                if adapter:
                    threading.Thread(target=self._typing_loop, args=(adapter, ev, stop_typing), daemon=True).start()
                agent = self._get_agent(ev, st)
                text, images = self._prepare_input(ev)
                st.last_user_text = text
                emit("command:message", platform=ev.platform, chat_id=ev.chat_id)
                result = agent.run_conversation(text, images=images or None)
                stop_typing.set()
                reply = result.get("final_response") or ""
                for m in result.get("media", []):
                    reply = reply.replace(f"MEDIA:{m}", "").strip()
                if reply and reply != "[interrupted]":
                    if st.voice_replies:
                        self._send_voice(ev, reply)
                    else:
                        self._reply(ev, reply)
                for m in result.get("media", []):
                    try:
                        adapter.send_file(ev.chat_id, m, "", ev.thread_id) if adapter else None
                    except Exception:
                        log.exception("send_file failed")
            except Exception as exc:
                log.exception("processing failed")
                self._reply(ev, f"⚠️ Something went wrong: {exc}")
            finally:
                st.busy = False
        if st.queue:
            nxt = st.queue.pop(0)
            st.queue.clear()
            threading.Thread(target=self._process, args=(nxt, st), daemon=True).start()

    def _send_voice(self, ev: MessageEvent, text: str) -> None:
        adapter = self.adapters.get(ev.platform)
        try:
            from hermes.tools.media import synthesize

            path = synthesize(text, self.cfg)
            adapter.send_file(ev.chat_id, str(path), "", ev.thread_id)
        except Exception:
            self._reply(ev, text)

    def _typing_loop(self, adapter: PlatformAdapter, ev: MessageEvent, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                adapter.send_typing(ev.chat_id, ev.thread_id)
            except Exception:
                return
            stop.wait(4)

    # ---------------------------------------------------------------- slash commands
    def handle_command(self, ev: MessageEvent, st: ChatState, text: str) -> bool:
        cmd, _, arg = text[1:].partition(" ")
        cmd = cmd.split("@")[0].lower()
        arg = arg.strip()
        emit(f"command:{cmd}", platform=ev.platform, chat_id=ev.chat_id, args=arg)
        if cmd in ("new", "reset", "start"):
            if st.agent:
                st.agent.reset()
                st.session_id = st.agent.session_id
                self.session_map[st.key] = st.session_id
                self._save_sessions()
            else:
                st.session_id = ""
                self.session_map.pop(st.key, None)
                self._save_sessions()
            self._reply(ev, "✨ Fresh conversation started." if cmd != "start" else "👋 Hi! I'm Hermes. Send me a task. /help for commands.")
            return True
        if cmd == "help":
            self._reply(ev, HELP)
            return True
        if cmd == "stop":
            if st.agent:
                st.agent.interrupt()
            st.queue.clear()
            self._reply(ev, "⏹ Stopping.")
            return True
        if cmd == "status":
            a = st.agent
            self._reply(ev, f"Session: {st.session_id or '(new)'}\nModel: {a.model if a else st.model or self.cfg['model']['default']}\n"
                            f"Busy: {st.busy}\nMessages: {len(a.messages) if a else 0}")
            return True
        if cmd == "usage":
            a = st.agent
            if not a:
                self._reply(ev, "No usage yet in this session.")
            else:
                u = a.usage
                self._reply(ev, f"Tokens: {u.input_tokens:,} in / {u.output_tokens:,} out ({u.cache_read_tokens:,} cached)\n"
                                f"Estimated cost: ${a.cost_usd:.4f}")
            return True
        if cmd == "model":
            if not arg:
                self._reply(ev, f"Current model: {st.agent.model if st.agent else self.cfg['model']['default']}")
            else:
                st.model = arg
                if st.agent:
                    st.agent.close(cleanup_env=False)
                st.agent = None
                self._reply(ev, f"Model set to {arg} for this chat (session preserved).")
            return True
        if cmd == "personality":
            st.personality = arg
            if st.agent:
                st.agent.close(cleanup_env=False)
            st.agent = None
            self._reply(ev, f"Personality: {arg or 'default'}")
            return True
        if cmd == "compress":
            if st.agent:
                info = st.agent.compress(arg)
                st.session_id = st.agent.session_id
                self.session_map[st.key] = st.session_id
                self._save_sessions()
                self._reply(ev, f"Compressed: {info}" if info.get("compressed") else f"Nothing to compress ({info.get('reason')}).")
            return True
        if cmd == "undo":
            if st.agent and st.agent.undo():
                self._reply(ev, "↩️ Removed the last exchange.")
            return True
        if cmd == "retry":
            if st.agent:
                last = st.agent.undo()
                if last:
                    threading.Thread(target=self._process, args=(MessageEvent(**{**ev.__dict__, "text": st.last_user_text or last}), st), daemon=True).start()
            return True
        if cmd == "sethome":
            cfg = hconfig.load_config(load_env=False)
            cfg.setdefault("gateway", {}).setdefault("home_channels", {})[ev.platform] = ev.chat_id
            hconfig.save_config(cfg)
            self.cfg.setdefault("gateway", {}).setdefault("home_channels", {})[ev.platform] = ev.chat_id
            self._reply(ev, f"🏠 This chat is now the {ev.platform} home channel for scheduled deliveries.")
            return True
        if cmd == "voice":
            st.voice_replies = arg.lower() in ("on", "true", "1", "")
            self._reply(ev, f"🔊 Voice replies {'on' if st.voice_replies else 'off'}.")
            return True
        if cmd == "skills":
            from hermes.skills.manager import SkillManager

            skills = SkillManager(self.cfg).discover()
            self._reply(ev, "Skills:\n" + "\n".join(f"/{s.name} - {s.description[:80]}" for s in skills) if skills else "No skills installed.")
            return True
        if cmd in ("approve", "deny"):
            self._reply(ev, "Nothing is waiting for approval.")
            return True
        from hermes.plugins import COMMANDS

        if cmd in COMMANDS:
            self._reply(ev, str(COMMANDS[cmd](arg, platform=ev.platform, chat_id=ev.chat_id)))
            return True
        from hermes.skills.manager import SkillManager

        prompt = SkillManager(self.cfg).slash_prompt(cmd, arg)
        if prompt:
            ev.text = prompt
            return False  # fall through: run the skill as a normal message
        return False
