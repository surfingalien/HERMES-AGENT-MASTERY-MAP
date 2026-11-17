"""`hermes` command-line interface (#7 CLI basics and every subcommand on the map)."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

from hermes import __version__, config

DIM, BOLD, CYAN, YELLOW, RED, GREEN, RESET = ("\033[2m", "\033[1m", "\033[36m", "\033[33m", "\033[31m", "\033[32m", "\033[0m") \
    if sys.stdout.isatty() and not os.environ.get("NO_COLOR") else ("",) * 7

TOOL_EMOJI = {"terminal": "💻", "process": "⚙️", "read_file": "📖", "write_file": "✍️", "patch": "🩹", "search_files": "🔎",
              "web_search": "🔍", "web_extract": "🌐", "memory": "🧠", "session_search": "🗂", "skill_view": "📚",
              "skills_list": "📚", "skill_manage": "🛠", "delegate_task": "👥", "cronjob": "⏰", "execute_code": "🐍",
              "send_message": "📨", "todo": "📝", "clarify": "❓", "vision_analyze": "👁", "image_generate": "🎨",
              "text_to_speech": "🔊", "kanban": "📋"}

SLASH_HELP = """Slash commands:
  /help                  this help                /new, /reset        fresh conversation
  /model [spec]          show/switch model        /tools              enabled tools
  /skills                list skills              /<skill> [request]  run a skill
  /memory                show memory              /personality <p>    concise|technical|teacher|...
  /usage                 tokens & cost            /insights           30-day usage report
  /compress [focus]      summarise context        /title <name>       name this session
  /sessions              recent sessions          /resume <id>        switch to a session
  /undo                  drop last exchange       /retry              redo last message
  /checkpoints           list file snapshots      /rollback [sha]     restore files
  /todo                  show task list           /yolo               toggle approvals off/on
  /voice                 push-to-talk voice mode  /verbose            toggle tool output
  /exit                  quit
Tip: reference files with @path/to/file, @folder:dir, @diff, @url:https://..."""


# ---------------------------------------------------------------- helpers
def _cfg():
    return config.load_config()


def _print_json(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _short(v, n=120) -> str:
    s = v if isinstance(v, str) else json.dumps(v, default=str)
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def make_callbacks(state: dict) -> dict:
    def on_tool_start(name, args):
        if state.get("quiet"):
            return
        key = next((k for k in ("command", "query", "path", "url", "urls", "goal", "action", "name", "code") if k in args), None)
        preview = _short(args.get(key), 90) if key else ""
        print(f"{DIM}  ┊ {TOOL_EMOJI.get(name, '🔧')} {name} {preview}{RESET}", flush=True)

    def on_tool_end(name, out, is_err, ms):
        if state.get("quiet"):
            return
        mark = f"{RED}✗{RESET}" if is_err else f"{GREEN}✓{RESET}"
        detail = f" {_short(out, 160)}" if (state.get("verbose") or is_err) else ""
        print(f"{DIM}  ┊   {mark}{DIM} {ms}ms{detail}{RESET}", flush=True)

    def on_thinking(text):
        if state.get("verbose"):
            print(f"{DIM}  💭 {_short(text, 300)}{RESET}")

    def on_interim(text):
        if text.strip():
            print(f"{DIM}  {text.strip()[:500]}{RESET}")

    return {"on_tool_start": on_tool_start, "on_tool_end": on_tool_end, "on_thinking": on_thinking, "on_interim": on_interim}


def build_agent(args, cfg, state: dict | None = None, **overrides):
    from hermes.agent import AIAgent

    state = state if state is not None else {}
    if getattr(args, "yolo", False):
        cfg.set_path("approvals.mode", "off")
    toolsets = [t.strip() for t in args.toolsets.split(",")] if getattr(args, "toolsets", None) else None
    session_id, resume = None, False
    from hermes.sessions.db import SessionDB

    db = SessionDB()
    if getattr(args, "resume", None):
        session_id = db.resolve(args.resume)
        if not session_id:
            raise SystemExit(f"session not found: {args.resume}")
        resume = True
    elif getattr(args, "continue_", False):
        session_id = db.latest("cli")
        resume = bool(session_id)
    clarify = None
    if sys.stdin.isatty():
        def clarify(q, choices):
            print(f"\n{YELLOW}❓ {q}{RESET}")
            for i, c in enumerate(choices or [], 1):
                print(f"   {i}. {c}")
            ans = input("   > ").strip()
            if choices and ans.isdigit() and 1 <= int(ans) <= len(choices):
                return choices[int(ans) - 1]
            return ans
    return AIAgent(config=cfg, model=getattr(args, "model", None), toolsets=toolsets, session_id=session_id, resume=resume,
                   personality=getattr(args, "personality", "") or "", cwd=getattr(args, "cwd", None), clarify_fn=clarify,
                   callbacks=make_callbacks(state), db=db, **overrides)


# ---------------------------------------------------------------- chat
def cmd_chat(args) -> int:
    cfg = _cfg()
    state = {"quiet": args.quiet, "verbose": False}
    if args.query:
        agent = build_agent(args, cfg, state)
        res = agent.run_conversation(args.query, images=args.image or None)
        print(res["final_response"])
        agent.close()
        return 1 if res.get("error") else 0
    return repl(args, cfg, state)


def _banner(agent) -> None:
    from hermes.skills.manager import SkillManager

    n_skills = len(SkillManager(agent.cfg).discover())
    print(f"{BOLD}{CYAN}☤ Hermes{RESET} {DIM}v{__version__} · {agent.provider.describe()} · {len(agent.tools)} tools · "
          f"{n_skills} skills · session {agent.session_id}{RESET}")
    print(f"{DIM}Type /help for commands, Ctrl+C to interrupt, /exit to quit.{RESET}")


def repl(args, cfg, state) -> int:
    try:
        import readline

        hist = config.path("cli_history")
        try:
            readline.read_history_file(hist)
        except OSError:
            pass
        import atexit

        atexit.register(lambda: _save_history(readline, hist))
    except ImportError:
        pass
    agent = build_agent(args, cfg, state)
    _banner(agent)
    if agent.messages and len(agent.messages) > 1:
        print(f"{DIM}(resumed {len(agent.messages) - 1} messages){RESET}")
    while True:
        try:
            line = input(f"\n{BOLD}❯{RESET} ").strip()
        except EOFError:
            break
        except KeyboardInterrupt:
            print()
            continue
        if not line:
            continue
        if line.startswith("/"):
            res = handle_slash(line, agent, cfg, state, args)
            if res == "exit":
                break
            if isinstance(res, tuple) and res[0] == "agent":
                agent = res[1]
                continue
            if isinstance(res, tuple) and res[0] == "run":
                line = res[1]
            else:
                continue
        try:
            start = time.time()
            result = agent.run_conversation(line)
            print(f"\n{result['final_response']}")
            print(f"{DIM}  {result['turns']} turn(s) · {result['tool_calls']} tool call(s) · {time.time() - start:.1f}s · "
                  f"${agent.cost_usd:.4f} session{RESET}")
        except KeyboardInterrupt:
            agent.interrupt()
            print(f"\n{YELLOW}interrupted{RESET}")
    agent.close()
    print(f"{DIM}Session saved: {agent.session_id}  (resume with `hermes --resume {agent.session_id}`){RESET}")
    return 0


def _save_history(readline, hist) -> None:
    try:
        readline.set_history_length(2000)
        readline.write_history_file(hist)
    except OSError:
        pass


def handle_slash(line: str, agent, cfg, state, args):
    cmd, _, arg = line[1:].partition(" ")
    arg = arg.strip()
    if cmd in ("exit", "quit", "q"):
        return "exit"
    if cmd == "help":
        print(SLASH_HELP)
    elif cmd in ("new", "reset"):
        agent.reset()
        print(f"✨ new session {agent.session_id}")
    elif cmd == "model":
        if not arg:
            print(f"model: {agent.provider.describe()}")
        else:
            from hermes.providers import create_provider

            args.model = arg
            agent.provider = create_provider(cfg, arg)
            agent.model = agent.provider.model
            print(f"switched to {agent.provider.describe()} (conversation kept)")
    elif cmd == "tools":
        for t in agent.tools:
            print(f"  {TOOL_EMOJI.get(t.name, '🔧')} {t.name:20} {DIM}[{t.toolset}] {t.description[:70]}{RESET}")
    elif cmd == "skills":
        for s in agent.skills.discover():
            print(f"  /{s.name:24} {DIM}{s.description[:80]}{RESET}")
    elif cmd == "memory":
        print(agent.memory.snapshot() or "(memory is empty)")
    elif cmd == "personality":
        agent.personality = arg
        agent.messages[0]["content"] = agent._system_prompt()
        print(f"personality: {arg or 'default'}")
    elif cmd == "usage":
        u = agent.usage
        print(f"tokens: {u.input_tokens:,} in / {u.output_tokens:,} out ({u.cache_read_tokens:,} cache reads) · est. ${agent.cost_usd:.4f}")
    elif cmd == "insights":
        from hermes.power.insights import format_report

        print(format_report(agent.db.insights(int(arg or 30))))
    elif cmd == "compress":
        print(agent.compress(arg))
    elif cmd == "title":
        agent.db.set_title(agent.session_id, arg)
        print(f"title set: {arg}")
    elif cmd == "sessions":
        for s in agent.db.list_sessions(15):
            print(f"  {s['id']}  {DIM}{s['source']:8}{RESET} {(s['title'] or '')[:60]}")
    elif cmd == "resume":
        args.resume = arg
        agent.close(cleanup_env=False)
        new = build_agent(args, cfg, state)
        print(f"resumed {new.session_id} ({len(new.messages) - 1} messages)")
        return ("agent", new)
    elif cmd == "undo":
        print("removed last exchange" if agent.undo() else "nothing to undo")
    elif cmd == "retry":
        last = agent.undo()
        if last:
            return ("run", last)
    elif cmd == "checkpoints":
        cps = agent.checkpoints.list(Path(agent.checkpoints.workdir_for(agent.cwd)))
        for c in cps[:20]:
            print(f"  {c['sha']}  {c['date']}  {c['label']}")
        if not cps:
            print("no checkpoints yet")
    elif cmd == "rollback":
        wd = agent.checkpoints.workdir_for(agent.cwd)
        cps = agent.checkpoints.list(Path(wd))
        sha = arg or (cps[0]["sha"] if cps else "")
        if not sha:
            print("no checkpoints")
        else:
            print(agent.checkpoints.diff(Path(wd), sha) or "(no differences)")
            if input(f"Roll back {wd} to {sha}? (y/N) ").lower().startswith("y"):
                print(agent.checkpoints.rollback(Path(wd), sha))
    elif cmd == "todo":
        for t in agent.todos:
            print(f"  [{ {'completed': 'x', 'in_progress': '~'}.get(t.get('status'), ' ') }] {t.get('content')}")
        if not agent.todos:
            print("no todos")
    elif cmd == "yolo":
        agent.approvals.mode = "off" if agent.approvals.mode != "off" else "manual"
        print(f"approvals: {agent.approvals.mode}")
    elif cmd == "verbose":
        state["verbose"] = not state.get("verbose")
        print(f"verbose: {state['verbose']}")
    elif cmd == "voice":
        from hermes.power.voice import voice_loop

        voice_loop(agent, cfg)
    else:
        from hermes.plugins import COMMANDS

        if cmd in COMMANDS:
            print(COMMANDS[cmd](arg, platform="cli"))
            return None
        prompt = agent.skills.slash_prompt(cmd, arg)
        if prompt:
            return ("run", prompt)
        print(f"unknown command /{cmd} - try /help")
    return None


# ---------------------------------------------------------------- setup / model / config
def cmd_setup(args) -> int:
    from hermes.setup_wizard import run_setup

    ni = None
    if args.non_interactive:
        ni = {"model.provider": args.provider, "model.default": args.model_name, "model.base_url": args.base_url,
              "terminal.backend": args.backend}
        if args.api_key_env and args.api_key:
            config.set_env_value(args.api_key_env, args.api_key)
    run_setup(portal=args.portal, non_interactive=ni)
    return 0


def cmd_model(args) -> int:
    cfg = _cfg()
    if args.list:
        from hermes.providers import create_provider

        prov = create_provider(cfg)
        if hasattr(prov, "list_models"):
            for m in prov.list_models():
                print(m)
        else:
            print("this provider does not support listing models")
        return 0
    if args.spec:
        from hermes.providers import split_model

        provider, model = split_model(args.spec, cfg.get_path("model.provider"))
        cfg.set_path("model.provider", provider)
        cfg.set_path("model.default", model)
        if args.base_url:
            cfg.set_path("model.base_url", args.base_url)
        config.save_config(cfg)
        print(f"model set to {provider}:{model}")
        return 0
    from hermes.setup_wizard import choose_provider

    choose_provider(cfg)
    config.save_config(cfg)
    return 0


def cmd_config(args) -> int:
    cfg = _cfg()
    a = args.action
    if a in (None, "show"):
        import yaml

        print(yaml.safe_dump(dict(cfg), sort_keys=False))
    elif a == "path":
        print(config.path("config.yaml"))
    elif a == "get":
        _print_json(cfg.get_path(args.key))
    elif a == "set":
        cfg.set_path(args.key, config.parse_value(args.value))
        config.save_config(cfg)
        print(f"{args.key} = {cfg.get_path(args.key)!r}")
    elif a == "edit":
        config.ensure_home()
        p = config.path("config.yaml")
        if not p.exists():
            config.save_config(cfg)
        subprocess.call([os.environ.get("EDITOR", "nano"), str(p)])
    elif a == "env":
        env = config.load_env_file()
        if args.key and args.value is not None:
            config.set_env_value(args.key, args.value)
            print(f"saved {args.key} to .env")
        else:
            for k, v in env.items():
                print(f"{k}={'***' if config.is_secret_name(k) else v}")
    return 0


# ---------------------------------------------------------------- tools / plugins
def cmd_tools(args) -> int:
    from hermes.tools import registry

    cfg = _cfg()
    registry.load_builtin_tools()
    from hermes.plugins import load_plugins

    load_plugins(cfg)
    platform = args.platform
    enabled_sets = registry.expand_toolsets(cfg.get_path(f"platform_toolsets.{platform}", ["hermes-cli"]))
    disabled = set(cfg.get("disabled_tools") or [])
    if args.action in (None, "list"):
        print(f"Toolsets for platform '{platform}':")
        for ts, names in sorted(registry.TOOLSETS.items()):
            on = ts in enabled_sets
            print(f" {GREEN + '●' + RESET if on else DIM + '○' + RESET} {BOLD}{ts}{RESET}")
            for n in names:
                t = registry.REGISTRY[n]
                ok, why = t.availability()
                state = "disabled" if n in disabled else ("ok" if ok else why)
                print(f"     {TOOL_EMOJI.get(n, '🔧')} {n:20} {DIM}{state}{RESET}")
        return 0
    target = args.name
    is_toolset = target in registry.TOOLSETS or target in registry.COMPOSITES
    if args.action == "enable":
        if is_toolset:
            sets = cfg.get_path(f"platform_toolsets.{platform}", ["hermes-cli"])
            if target not in sets:
                sets.append(target)
            cfg.set_path(f"platform_toolsets.{platform}", sets)
        cfg["disabled_tools"] = [d for d in cfg.get("disabled_tools", []) if d != target]
    elif args.action == "disable":
        if is_toolset and target in cfg.get_path(f"platform_toolsets.{platform}", []):
            cfg.set_path(f"platform_toolsets.{platform}", [s for s in cfg.get_path(f"platform_toolsets.{platform}") if s != target])
        elif is_toolset:
            cfg["disabled_tools"] = sorted(set(cfg.get("disabled_tools", [])) | set(registry.TOOLSETS.get(target, [])))
        else:
            cfg["disabled_tools"] = sorted(set(cfg.get("disabled_tools", [])) | {target})
    config.save_config(cfg)
    print(f"{args.action}d {target} for {platform}")
    return 0


def cmd_plugins(args) -> int:
    from hermes import plugins

    cfg = _cfg()
    if args.action in (None, "list"):
        infos = plugins.load_plugins(cfg)
        if not infos:
            print(f"No plugins in {config.path('plugins')}. Create one with `hermes plugins create <name>`.")
        for p in infos:
            status = "loaded" if p.loaded else ("enabled" if p.enabled else "disabled")
            print(f"  {p.name:20} {status:9} {p.meta.get('version', ''):8} {p.meta.get('description', '')} {RED + p.error + RESET if p.error else ''}")
            for t in p.tools:
                print(f"      tool: {t}")
        return 0
    if args.action == "create":
        print(f"created {plugins.scaffold(args.name)} - enable with `hermes plugins enable {args.name}`")
        return 0
    if args.action == "install":
        print(f"installed {plugins.install_git(args.name)} (review its code, then `hermes plugins enable <name>`)")
        return 0
    enabled = set(cfg.get_path("plugins.enabled", []) or [])
    if args.action == "enable":
        enabled.add(args.name)
    elif args.action == "disable":
        enabled.discard(args.name)
    cfg.set_path("plugins.enabled", sorted(enabled))
    config.save_config(cfg)
    print(f"{args.action}d plugin {args.name}")
    return 0


# ---------------------------------------------------------------- messaging
def cmd_send(args) -> int:
    from hermes.gateway.delivery import deliver, list_targets

    cfg = _cfg()
    if args.target == "list":
        _print_json(list_targets(cfg))
        return 0
    message = args.message if args.message != "-" else sys.stdin.read()
    _print_json(deliver(args.target, message or "", None, cfg, files=args.file or []))
    return 0


def cmd_gateway(args) -> int:
    from hermes import cli_gateway

    if args.action == "setup":
        cli_gateway.gateway_setup()
    elif args.action == "install":
        print(cli_gateway.install_service(dry_run=args.dry_run))
    elif args.action == "status":
        from hermes.gateway.delivery import list_targets

        _print_json({"platforms": list_targets(_cfg()), "pid_file": str(config.path("gateway", "gateway.pid"))})
    else:
        from hermes.gateway.runner import GatewayRunner
        from hermes.logging_setup import setup_logging

        setup_logging()
        config.path("gateway").mkdir(parents=True, exist_ok=True)
        config.path("gateway", "gateway.pid").write_text(str(os.getpid()))
        runner = GatewayRunner(_cfg())
        print("Starting gateway… (Ctrl+C to stop)")
        runner.start(block=True)
    return 0


def cmd_pairing(args) -> int:
    from hermes.gateway import runner

    if args.action == "approve":
        entry = runner.approve_pairing(args.platform, args.code)
        print(f"approved {entry['user_name'] or entry['user_id']} on {args.platform}" if entry else "no such pending code")
    elif args.action == "revoke":
        print("revoked" if runner.revoke_user(args.platform, args.code) else "not found")
    else:
        _print_json(runner.load_pairing())
    return 0


# ---------------------------------------------------------------- sessions / memory / insights
def cmd_sessions(args) -> int:
    from hermes.sessions.db import SessionDB

    db = SessionDB()
    a = args.action or "list"
    if a == "list":
        for s in db.list_sessions(args.limit, args.source):
            print(f"{s['id']}  {s['source']:9} {s['started_at']}  {s['message_count']:>4} msgs  ${s['cost_usd']:.4f}  {(s['title'] or '')[:50]}")
    elif a == "search":
        for h in db.search(args.query, args.limit):
            print(f"{h['session_id']}  [{h['source']}] {h['title'][:40]}\n    {h['snippet']}")
    elif a in ("show", "export"):
        sid = db.resolve(args.query)
        if not sid:
            print("not found")
            return 1
        msgs = db.get_messages(sid)
        if a == "export":
            out = Path(args.output or f"{sid}.json")
            out.write_text(json.dumps({"session": db.get_session(sid), "messages": msgs}, indent=2, default=str))
            print(f"exported to {out}")
        else:
            for m in msgs:
                if m["role"] == "system":
                    continue
                body = m.get("content") or ""
                if m.get("tool_calls"):
                    body += " " + ", ".join(f"→{tc['function']['name']}" for tc in m["tool_calls"])
                print(f"{BOLD}{m['role']}{RESET}: {textwrap.shorten(body, 400) if m['role'] == 'tool' else body}\n")
    elif a == "delete":
        print("deleted" if db.delete_session(db.resolve(args.query) or args.query) else "not found")
    elif a == "rename":
        db.set_title(db.resolve(args.query), args.title)
        print("renamed")
    elif a == "prune":
        print(f"pruned {db.prune(args.days)} sessions older than {args.days} days")
    return 0


def cmd_memory(args) -> int:
    from hermes.memory.store import MemoryStore

    store = MemoryStore(_cfg())
    a = args.action or "show"
    if a == "show":
        print(store.snapshot() or "(memory is empty)")
        print(f"\n{DIM}{store.memory.path}\n{store.user.path}{RESET}")
    elif a == "add":
        _print_json(store.add(args.target, args.text))
    elif a == "remove":
        _print_json(store.remove(args.target, args.text))
    elif a == "clear":
        (store.user if args.target == "user" else store.memory).write([])
        print(f"cleared {args.target}")
    elif a == "edit":
        f = store.user if args.target == "user" else store.memory
        f.path.parent.mkdir(parents=True, exist_ok=True)
        f.path.touch()
        subprocess.call([os.environ.get("EDITOR", "nano"), str(f.path)])
    return 0


def cmd_insights(args) -> int:
    from hermes.power.insights import format_report
    from hermes.sessions.db import SessionDB

    data = SessionDB().insights(args.days)
    if args.json:
        _print_json(data)
    else:
        print(format_report(data))
    return 0


# ---------------------------------------------------------------- skills
def cmd_skills(args) -> int:
    from hermes.skills import hub
    from hermes.skills.manager import SkillManager

    cfg = _cfg()
    mgr = SkillManager(cfg)
    a = args.action or "list"
    if a == "list":
        cat = None
        for s in mgr.discover():
            if s.category != cat:
                cat = s.category
                print(f"{BOLD}{cat or 'general'}{RESET}")
            src = " (hub)" if s.name in hub.read_lock() else ""
            print(f"  {s.name:28} {DIM}{s.description[:90]}{src}{RESET}")
    elif a == "view":
        res = mgr.view(args.name, args.file)
        print(res.get("content") or res.get("error"))
    elif a == "sync":
        print(f"synced {len(mgr.sync_bundled())} bundled skills")
    elif a == "check":
        bad = 0
        for r in mgr.check():
            if r["issues"]:
                bad += 1
                print(f"{YELLOW}{r['name']}{RESET}: " + "; ".join(r["issues"]))
        print(f"{bad} skill(s) with issues")
    elif a == "install":
        _print_json(hub.install(args.name, category=args.category, force=args.force, cfg=cfg))
    elif a == "uninstall":
        _print_json(hub.uninstall(args.name))
    elif a == "update":
        updates = hub.check_updates()
        for u in updates:
            if u.get("update_available") and not u.get("locally_modified"):
                entry = hub.read_lock()[u["name"]]
                print(f"updating {u['name']}: {hub.install(entry['source'], force=args.force, cfg=cfg).get('ok')}")
        _print_json(updates)
    elif a == "search":
        taps = list(cfg.get_path("skills.taps", []) or [])
        local = [{"name": s.name, "description": s.description, "source": "installed"} for s in mgr.discover()
                 if args.name.lower() in f"{s.name} {s.description}".lower()]
        _print_json(local + hub.search(args.name, taps))
    elif a == "create":
        template = f"""---
name: {args.name}
description: Describe what this skill does and when to use it.
version: 1.0.0
---

# {args.name}

## When to use
- ...

## Steps
1. ...

## Pitfalls
- ...

## Verification
- ...
"""
        _print_json(mgr.create(args.name, template, args.category if args.category != "community" else ""))
    return 0


# ---------------------------------------------------------------- cron
def cmd_cron(args) -> int:
    from hermes.cron import jobs

    cfg = _cfg()
    a = args.action or "list"
    if a == "list":
        js = jobs.load_jobs()
        if not js:
            print("No scheduled jobs. Create one: hermes cron create 'daily at 9am' 'Summarise top AI news'")
        for j in js:
            state = GREEN + "on " + RESET if j.enabled else DIM + "off" + RESET
            print(f"{j.id}  {state} {j.sched.describe():28} next: {j.next_run or '-':19}  last: {j.last_status or '-':5}  {j.name[:40]}  → {j.deliver}")
    elif a == "create":
        j = jobs.create_job(args.prompt, args.schedule, name=args.name or "", deliver=args.deliver,
                            skills=[s for s in (args.skills or "").split(",") if s], repeat=args.repeat)
        print(f"created {j.id}: {j.sched.describe()}, next run {j.next_run}")
    elif a == "pause":
        print("paused" if jobs.pause(args.job) else "not found")
    elif a == "resume":
        print("resumed" if jobs.resume(args.job) else "not found")
    elif a == "remove":
        print("removed" if jobs.remove(args.job) else "not found")
    elif a == "run":
        j = jobs.find(args.job)
        if not j:
            print("not found")
            return 1
        from hermes.gateway.delivery import deliver

        _print_json(jobs.run_job(j, cfg, lambda t, text, o: deliver(t, text, o, cfg)))
    elif a == "tick":
        from hermes.gateway.delivery import deliver

        res = jobs.tick(cfg, lambda t, text, o: deliver(t, text, o, cfg))
        print(f"ran {len(res)} due job(s)")
        for r in res:
            _print_json(r)
    elif a == "status":
        _print_json(jobs.status_report())
    elif a == "logs":
        for p in jobs.outputs(args.job, args.limit):
            print(f"{BOLD}── {p.name}{RESET}\n{p.read_text()[:3000]}")
    return 0


# ---------------------------------------------------------------- mcp
def cmd_mcp(args) -> int:
    cfg = _cfg()
    servers = cfg.setdefault("mcp_servers", {})
    a = args.action or "list"
    if a == "list":
        for name, spec in servers.items():
            kind = f"http {spec.get('url')}" if spec.get("url") else f"stdio {spec.get('command')} {' '.join(spec.get('args', []))}"
            print(f"  {name:16} {kind}  {DIM}{spec.get('tools', '')}{RESET}")
        if not servers:
            print("No MCP servers. Add one: hermes mcp add fs -- npx -y @modelcontextprotocol/server-filesystem ~/docs")
    elif a == "add":
        spec: dict = {}
        if args.url:
            spec["url"] = args.url
        else:
            if not args.server_command:
                print("give a command after --, or --url")
                return 1
            spec["command"], spec["args"] = args.server_command[0], args.server_command[1:]
        if args.include:
            spec.setdefault("tools", {})["include"] = args.include.split(",")
        if args.exclude:
            spec.setdefault("tools", {})["exclude"] = args.exclude.split(",")
        servers[args.name] = spec
        config.save_config(cfg)
        print(f"added MCP server {args.name}; test with `hermes mcp test {args.name}`")
    elif a == "remove":
        servers.pop(args.name, None)
        config.save_config(cfg)
        print("removed")
    elif a == "test":
        from hermes.mcp.client import MCPClient

        c = MCPClient(args.name, servers[args.name])
        c.connect()
        tools = c.list_tools()
        print(f"connected to {c.server_info.get('name', args.name)} - {len(tools)} tools:")
        for t in tools:
            print(f"  {t['name']:30} {DIM}{(t.get('description') or '')[:80]}{RESET}")
        c.close()
    return 0


# ---------------------------------------------------------------- profiles / kanban
def cmd_profile(args) -> int:
    base = config.base_home() / "profiles"
    a = args.action or "list"
    if a == "list":
        print("  default" + ("  (active)" if not os.environ.get("HERMES_HOME") else ""))
        for d in sorted(base.iterdir()) if base.exists() else []:
            print(f"  {d.name}")
    elif a == "create":
        home = config.use_profile(args.name)
        config.ensure_home()
        src = config.base_home()
        if args.clone:
            for f in ("config.yaml", ".env"):
                if (src / f).exists():
                    shutil.copy2(src / f, home / f)
        soul = home / "SOUL.md"
        soul.write_text(args.soul or f"You are {args.name}, a specialist Hermes agent. Stay focused on your specialty.\n")
        from hermes.skills.manager import SkillManager

        SkillManager().sync_bundled()
        print(f"created profile {args.name} at {home}\nUse it: hermes -p {args.name}   (edit {soul} to specialise it)")
    elif a == "delete":
        target = base / args.name
        if target.exists() and input(f"Delete {target}? (y/N) ").lower().startswith("y"):
            shutil.rmtree(target)
            print("deleted")
    elif a == "show":
        print(config.hermes_home())
    return 0


def cmd_kanban(args) -> int:
    from hermes.power.kanban import Board, run_worker

    board = Board(board=args.board)
    a = args.action or "list"
    if a == "list":
        cols: dict[str, list] = {}
        for c in board.list():
            cols.setdefault(c["status"], []).append(c)
        for status in ("todo", "doing", "blocked", "review", "done"):
            print(f"{BOLD}{status.upper()}{RESET} ({len(cols.get(status, []))})")
            for c in cols.get(status, []):
                who = f" @{c['assignee']}" if c["assignee"] else ""
                print(f"  #{c['id']:<4} {c['title'][:60]}{DIM}{who}{RESET}")
    elif a == "add":
        deps = [int(x) for x in (args.depends or "").split(",") if x.strip()]
        c = board.add(args.title, args.body or "", args.assignee or "", depends_on=deps)
        print(f"added #{c['id']}")
    elif a == "move":
        board.move(args.card, args.status)
        print("moved")
    elif a == "show":
        _print_json(board.get(args.card))
    elif a == "work":
        import threading

        cfg = _cfg()
        profile = os.environ.get("HERMES_HOME", "").rstrip("/").split("/")[-1] if os.environ.get("HERMES_HOME") else ""
        stop = threading.Event()
        threads = [threading.Thread(target=run_worker, args=(cfg, board, profile, args.once, 5, stop), daemon=True) for _ in range(args.workers)]
        for t in threads:
            t.start()
        try:
            for t in threads:
                while t.is_alive():
                    t.join(1)
        except KeyboardInterrupt:
            stop.set()
        print("workers stopped")
    return 0


# ---------------------------------------------------------------- misc power features
def cmd_serve(args) -> int:
    from hermes.power.api_server import serve

    serve(_cfg(), args.host, args.port, with_dashboard=not args.no_dashboard)
    return 0


def cmd_dashboard(args) -> int:
    from hermes.power.api_server import serve

    serve(_cfg(), args.host, args.port, with_dashboard=True, open_browser=not args.no_open)
    return 0


def cmd_acp(args) -> int:
    from hermes.power.acp import ACPServer

    ACPServer(_cfg()).serve()
    return 0


def cmd_batch(args) -> int:
    from hermes.power.batch import run_batch

    toolsets = [t.strip() for t in args.toolsets.split(",")] if args.toolsets else None
    _print_json(run_batch(_cfg(), args.input, args.output, args.workers, toolsets, args.model))
    return 0


def cmd_voice(args) -> int:
    from hermes.power.voice import voice_loop

    cfg = _cfg()
    agent = build_agent(args, cfg, {"quiet": False})
    voice_loop(agent, cfg, args.seconds, not args.no_speak)
    agent.close()
    return 0


def cmd_backup(args) -> int:
    from hermes.power.backup import create_backup

    print(f"backup written to {create_backup(args.output, args.include_secrets)}")
    return 0


def cmd_restore(args) -> int:
    from hermes.power.backup import restore_backup

    print(f"restored into {restore_backup(args.archive, args.force)}")
    return 0


def cmd_checkpoints(args) -> int:
    from hermes.security.checkpoints import CheckpointManager

    mgr = CheckpointManager()
    wd = Path(args.dir or os.getcwd()).resolve()
    if args.action in (None, "list"):
        for c in mgr.list(wd):
            print(f"  {c['sha']}  {c['date']}  {c['label']}")
    elif args.action == "diff":
        print(mgr.diff(wd, args.sha))
    elif args.action == "rollback":
        print(mgr.rollback(wd, args.sha, args.file))
    elif args.action == "snapshot":
        print(mgr.snapshot(wd, "manual") or "nothing to snapshot")
    return 0


def cmd_logs(args) -> int:
    p = config.path("logs", "errors.log" if args.errors else "agent.log")
    if not p.exists():
        print(f"no log at {p}")
        return 0
    lines = p.read_text(errors="replace").splitlines()[-args.lines:]
    print("\n".join(lines))
    return 0


def cmd_doctor(args) -> int:
    from hermes.doctor import run_doctor

    return run_doctor(fix=args.fix, ping=args.ping)


def cmd_update(args) -> int:
    root = Path(__file__).resolve().parent.parent
    if (root / ".git").exists():
        subprocess.call(["git", "-C", str(root), "pull", "--ff-only"])
        subprocess.call([sys.executable, "-m", "pip", "install", "-e", str(root), "-q"])
    from hermes.skills.manager import SkillManager

    print(f"synced {len(SkillManager().sync_bundled())} bundled skills")
    return 0


# ---------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hermes", description="Hermes Agent replica - a self-improving AI agent.")
    p.add_argument("-p", "--profile", help="use a named profile (isolated specialist bot)")
    p.add_argument("--version", action="version", version=f"hermes {__version__}")
    sub = p.add_subparsers(dest="command")

    def chat_args(sp):
        sp.add_argument("-q", "--query", help="one-shot query (non-interactive)")
        sp.add_argument("-m", "--model", help="override model, e.g. anthropic:claude-opus-5-5")
        sp.add_argument("-t", "--toolsets", help="comma-separated toolsets, e.g. web,file,terminal")
        sp.add_argument("-r", "--resume", help="resume a session by id/title")
        sp.add_argument("-c", "--continue", dest="continue_", action="store_true", help="continue the latest CLI session")
        sp.add_argument("--personality", help="concise | technical | teacher | creative | skeptic | helpful | custom text")
        sp.add_argument("--cwd", help="working directory for tools")
        sp.add_argument("--image", action="append", help="attach an image (repeatable)")
        sp.add_argument("--yolo", action="store_true", help="skip approval prompts (dangerous)")
        sp.add_argument("--quiet", action="store_true", help="hide tool activity")

    chat_args(p)
    chat_args(sub.add_parser("chat", help="interactive chat (default)"))

    s = sub.add_parser("setup", help="interactive setup wizard")
    s.add_argument("--portal", action="store_true", help="quick setup with Nous Portal")
    s.add_argument("--non-interactive", action="store_true")
    s.add_argument("--provider")
    s.add_argument("--model-name")
    s.add_argument("--base-url")
    s.add_argument("--backend")
    s.add_argument("--api-key-env")
    s.add_argument("--api-key")

    s = sub.add_parser("model", help="choose provider/model")
    s.add_argument("spec", nargs="?", help="provider:model, e.g. openrouter:anthropic/claude-opus-5-5")
    s.add_argument("--base-url")
    s.add_argument("--list", action="store_true", help="list models offered by the provider")

    s = sub.add_parser("config", help="show/get/set configuration")
    s.add_argument("action", nargs="?", choices=["show", "get", "set", "edit", "path", "env"])
    s.add_argument("key", nargs="?")
    s.add_argument("value", nargs="?")

    s = sub.add_parser("tools", help="list/enable/disable tools and toolsets")
    s.add_argument("action", nargs="?", choices=["list", "enable", "disable"])
    s.add_argument("name", nargs="?")
    s.add_argument("--platform", default="cli")

    s = sub.add_parser("plugins", help="manage plugins")
    s.add_argument("action", nargs="?", choices=["list", "enable", "disable", "create", "install"])
    s.add_argument("name", nargs="?")

    s = sub.add_parser("send", help="send a message to a platform: hermes send telegram 'hello'")
    s.add_argument("target", help="telegram | telegram:<chat_id> | slack:<channel> | email:<addr> | list")
    s.add_argument("message", nargs="?", default="", help="text, or - for stdin")
    s.add_argument("--file", action="append")

    s = sub.add_parser("gateway", help="messaging gateway")
    s.add_argument("action", nargs="?", choices=["run", "setup", "install", "status"], default="run")
    s.add_argument("--dry-run", action="store_true")

    s = sub.add_parser("pairing", help="approve users who messaged the bot")
    s.add_argument("action", nargs="?", choices=["list", "approve", "revoke"], default="list")
    s.add_argument("platform", nargs="?")
    s.add_argument("code", nargs="?", help="pairing code (approve) or user id (revoke)")

    s = sub.add_parser("sessions", help="browse/search past sessions")
    s.add_argument("action", nargs="?", choices=["list", "search", "show", "export", "delete", "rename", "prune"])
    s.add_argument("query", nargs="?", default="")
    s.add_argument("title", nargs="?", default="")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--source")
    s.add_argument("--output")
    s.add_argument("--days", type=int, default=90)

    s = sub.add_parser("memory", help="view/edit persistent memory")
    s.add_argument("action", nargs="?", choices=["show", "add", "remove", "clear", "edit"])
    s.add_argument("text", nargs="?", default="")
    s.add_argument("--target", choices=["memory", "user"], default="memory")

    s = sub.add_parser("skills", help="skills & Skills Hub")
    s.add_argument("action", nargs="?", choices=["list", "view", "search", "install", "uninstall", "check", "update", "sync", "create"])
    s.add_argument("name", nargs="?", default="")
    s.add_argument("--file")
    s.add_argument("--category", default="community")
    s.add_argument("--force", action="store_true")

    s = sub.add_parser("cron", help="scheduled tasks")
    s.add_argument("action", nargs="?", choices=["list", "create", "pause", "resume", "remove", "run", "tick", "status", "logs"])
    s.add_argument("schedule_or_job", nargs="?")
    s.add_argument("prompt", nargs="?")
    s.add_argument("--name")
    s.add_argument("--deliver", default="local")
    s.add_argument("--skills")
    s.add_argument("--repeat", type=int)
    s.add_argument("--limit", type=int, default=5)

    s = sub.add_parser("mcp", help="MCP servers")
    s.add_argument("action", nargs="?", choices=["list", "add", "remove", "test"])
    s.add_argument("name", nargs="?")
    s.add_argument("--url")
    s.add_argument("--include")
    s.add_argument("--exclude")
    s.set_defaults(server_command=[])  # everything after `--` (split off before parsing)

    s = sub.add_parser("profile", help="specialist bot profiles")
    s.add_argument("action", nargs="?", choices=["list", "create", "delete", "show"])
    s.add_argument("name", nargs="?")
    s.add_argument("--clone", action="store_true", help="copy config + .env from the default profile")
    s.add_argument("--soul", help="personality text for SOUL.md")

    s = sub.add_parser("kanban", help="multi-agent kanban board")
    s.add_argument("action", nargs="?", choices=["list", "add", "move", "show", "work"])
    s.add_argument("title", nargs="?")
    s.add_argument("--body")
    s.add_argument("--assignee")
    s.add_argument("--depends")
    s.add_argument("--card", type=int)
    s.add_argument("--status")
    s.add_argument("--workers", type=int, default=2)
    s.add_argument("--once", action="store_true")
    s.add_argument("--board", default="default")

    s = sub.add_parser("serve", help="OpenAI-compatible API server (+ web dashboard)")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--no-dashboard", action="store_true", help="serve only the /v1 API")
    s = sub.add_parser("dashboard", help="open the web dashboard (chat, sessions, memory, skills, cron, insights, settings)")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--no-open", action="store_true", help="don't open a browser")
    sub.add_parser("acp", help="Agent Client Protocol server for editors (stdio)")

    s = sub.add_parser("batch", help="run prompts from a file in parallel")
    s.add_argument("input")
    s.add_argument("--output", default="batch_output")
    s.add_argument("--workers", type=int, default=4)
    s.add_argument("--toolsets")
    s.add_argument("--model")

    s = sub.add_parser("voice", help="push-to-talk voice mode")
    chat_args(s)
    s.add_argument("--seconds", type=int, default=8)
    s.add_argument("--no-speak", action="store_true")

    s = sub.add_parser("insights", help="usage, tools and cost report")
    s.add_argument("--days", type=int, default=30)
    s.add_argument("--json", action="store_true")

    s = sub.add_parser("backup", help="archive HERMES_HOME")
    s.add_argument("--output")
    s.add_argument("--include-secrets", action="store_true")
    s = sub.add_parser("restore", help="restore a backup archive")
    s.add_argument("archive")
    s.add_argument("--force", action="store_true")

    s = sub.add_parser("checkpoints", help="file snapshots & rollback")
    s.add_argument("action", nargs="?", choices=["list", "diff", "rollback", "snapshot"])
    s.add_argument("sha", nargs="?")
    s.add_argument("--dir")
    s.add_argument("--file")

    s = sub.add_parser("logs", help="show logs")
    s.add_argument("--errors", action="store_true")
    s.add_argument("-n", "--lines", type=int, default=50)

    s = sub.add_parser("doctor", help="diagnose problems")
    s.add_argument("--fix", action="store_true")
    s.add_argument("--ping", action="store_true", help="make a real model call")

    sub.add_parser("update", help="pull latest code and sync bundled skills")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    server_command: list[str] = []
    if "mcp" in argv and "--" in argv:
        cut = argv.index("--")
        argv, server_command = argv[:cut], argv[cut + 1:]
    args = parser.parse_args(argv)
    if server_command:
        args.server_command = server_command
    if args.profile:
        config.use_profile(args.profile)
    from hermes.logging_setup import setup_logging

    setup_logging()
    cmd = args.command
    if cmd == "cron":
        # positional juggling: create <schedule> <prompt>; others take <job>
        args.schedule = args.schedule_or_job
        args.job = args.schedule_or_job
    if cmd == "kanban" and args.action in ("move", "show") and args.card is None and args.title and args.title.isdigit():
        args.card = int(args.title)
    if cmd == "sessions" and args.action == "rename":
        args.title = args.title or ""
    if not config.path("config.yaml").exists() and cmd in (None, "chat") and sys.stdin.isatty() and not os.environ.get("HERMES_SKIP_SETUP"):
        print("No configuration found - starting setup.\n")
        cmd_setup(argparse.Namespace(portal=False, non_interactive=False))
    handlers = {
        None: cmd_chat, "chat": cmd_chat, "setup": cmd_setup, "model": cmd_model, "config": cmd_config, "tools": cmd_tools,
        "plugins": cmd_plugins, "send": cmd_send, "gateway": cmd_gateway, "pairing": cmd_pairing, "sessions": cmd_sessions,
        "memory": cmd_memory, "skills": cmd_skills, "cron": cmd_cron, "mcp": cmd_mcp, "profile": cmd_profile, "kanban": cmd_kanban,
        "serve": cmd_serve, "dashboard": cmd_dashboard, "acp": cmd_acp, "batch": cmd_batch, "voice": cmd_voice, "insights": cmd_insights, "backup": cmd_backup,
        "restore": cmd_restore, "checkpoints": cmd_checkpoints, "logs": cmd_logs, "doctor": cmd_doctor, "update": cmd_update,
    }
    try:
        return handlers[cmd](args) or 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
