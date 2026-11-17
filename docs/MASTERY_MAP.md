# Mastery Map → implementation

Every one of the 100 items on the Hermes Agent Mastery Map, where it lives in this
replica, and how to use it. Paths are relative to the repo root.

## 1 · Foundations (1-10) — goal: get Hermes running

| # | Item | Implementation | Use it |
|---|------|----------------|--------|
| 1 | What is Hermes Agent | `hermes/agent.py` – the loop: model + tools + context, learning via memory & skills | `README.md` → *How it works* |
| 2 | Nous Research | Independent replica of their open-source agent; Nous Portal provider preset | `hermes setup --portal` |
| 3 | install macOS / Linux / Windows | `scripts/install.sh` (venv + `~/.local/bin/hermes`); WSL on Windows; `pip install -e .` | `./scripts/install.sh` |
| 4 | `hermes setup --portal` | `hermes/setup_wizard.py` (`run_setup`, `portal_flow`) | `hermes setup`, `hermes setup --portal` |
| 5 | choose model provider | `hermes/providers/` – OpenRouter, Nous, Anthropic (official SDK), OpenAI, Gemini, DeepSeek, Groq, xAI, Ollama, LM Studio, custom | `hermes model`, `hermes model anthropic:claude-opus-5-5` |
| 6 | first conversation | `hermes/cli.py` REPL + one-shot | `hermes`, `hermes -q "hi"` |
| 7 | CLI basics | slash commands, history, `@file` refs, interrupts, tool activity feed | `/help` inside chat |
| 8 | configuration | `hermes/config.py` – `~/.hermes/config.yaml` + `.env` (600) | `hermes config set agent.max_turns 80`, `examples/config.example.yaml` |
| 9 | sessions | `hermes/sessions/db.py` – SQLite + FTS5, resume/continue, export | `hermes sessions list`, `hermes -c`, `hermes --resume <id>` |
| 10 | troubleshooting | `hermes/doctor.py`, redacted logs in `~/.hermes/logs/` | `hermes doctor [--fix] [--ping]`, `hermes logs --errors`, `docs/TROUBLESHOOTING.md` |

## 2 · Tools & Toolsets (11-20) — goal: give the agent useful tools

| # | Item | Implementation | Notes |
|---|------|----------------|-------|
| 11 | `web_search` | `hermes/tools/web.py` | Tavily → Brave → Exa → Firecrawl → SearXNG → keyless DuckDuckGo |
| 12 | `web_extract` | `hermes/tools/web.py` (`fetch_page`, HTML→markdown) | SSRF guard blocks private addresses |
| 13 | `terminal` | `hermes/tools/terminal.py`, backends in `hermes/tools/environments.py` | persistent cwd, secrets stripped from child env, approval gating |
| 14 | `process_manage` | `hermes/tools/process.py` (`process` tool) | start / poll / wait / write / kill background jobs |
| 15 | `read_file` | `hermes/tools/files.py` | line numbers, paging, protected paths |
| 16 | `patch` | `hermes/tools/files.py` | unique-match replace + whitespace-tolerant fallback, checkpointed |
| 17 | browser automation | `hermes/tools/browser.py` | Playwright engine or dependency-free *lite* engine |
| 18 | `vision_analyze` | `hermes/tools/media.py` | images by URL or path, via the active model |
| 19 | `image_generate` | `hermes/tools/media.py` | FAL (Flux) or OpenAI images |
| 20 | `text_to_speech` | `hermes/tools/media.py` | ElevenLabs / OpenAI / edge-tts; returns `MEDIA:` path delivered as voice |

Toolsets & per-platform enablement: `hermes/tools/registry.py`; `hermes tools`, `hermes tools disable browser --platform telegram`.

## 3 · Memory (21-30) — goal: preserve useful context

| # | Item | Implementation |
|---|------|----------------|
| 21 | MEMORY.md | `hermes/memory/store.py` – agent notes, bounded (2,200 chars), `§`-delimited |
| 22 | USER.md | same store, user profile (1,375 chars) |
| 23 | persistent memory | `memory` tool (`hermes/tools/memory_tool.py`); frozen snapshot injected at session start; periodic save nudges |
| 24 | `session_search` | `hermes/tools/session_search_tool.py` – FTS5 with porter stemming, optional LLM recap |
| 25 | cross-session recall | `session_search(summarize=true)`; `memory.provider: session_recall` auto-prefetches related past sessions |
| 26 | context files | `hermes/context/prompt_builder.py` loads AGENTS.md / .hermes.md / CLAUDE.md / .cursorrules with injection scanning |
| 27 | SOUL.md personality | `~/.hermes/SOUL.md` + `/personality concise|technical|teacher|…` |
| 28 | memory providers | `MemoryProvider` interface (`prefetch`, `sync_turn`, `on_session_end`); plugins call `ctx.register_memory_provider()` |
| 29 | privacy boundaries | secrets & injections refused by memory, `memory.read_only_platforms`, subagents get no memory, profiles isolate homes |
| 30 | file references | `hermes/context/references.py` – `@file`, `@file:path:10-40`, `@folder:`, `@diff`, `@staged`, `@url:` |

## 4 · Skills (31-40) — goal: turn experience into reusable skills

| # | Item | Implementation |
|---|------|----------------|
| 31 | skills system | `hermes/skills/manager.py` – `~/.hermes/skills/<category>/<name>/SKILL.md` |
| 32 | progressive disclosure | level 0 index in system prompt → `skill_view` (SKILL.md) → `skill_view(file_path=…)` |
| 33 | `skills_list` | `hermes/tools/skills_tools.py` |
| 34 | `skill_view` | `hermes/tools/skills_tools.py` |
| 35 | `skill_manage` | create / patch / edit / delete / write_file / remove_file, validation + threat scan |
| 36 | create a skill | agent via `skill_manage`, you via `hermes skills create <name>` |
| 37 | reusable procedures | 12 bundled skills in `hermes/bundled_skills/`; run any as `/skill-name …` in CLI or chat |
| 38 | Skills Hub | `hermes/skills/hub.py` – install from path / URL / GitHub, lockfile, updates, security scan |
| 39 | agentskills.io | same SKILL.md format (frontmatter `name` + `description`); index "taps" via `skills.taps` |
| 40 | improve skills from experience | after complex tasks the agent is nudged to save/patch skills (`skills.creation_nudge_interval`) |

## 5 · Messaging (41-50) — goal: reach Hermes where you work

| # | Item | Implementation |
|---|------|----------------|
| 41 | Gateway setup | `hermes/gateway/runner.py`, `hermes/cli_gateway.py` – `hermes gateway setup / run / install` |
| 42 | Telegram | `gateway/platforms/telegram.py` – Bot API long polling (stdlib), voice/photo/document, topics |
| 43 | Discord | `gateway/platforms/discord.py` – discord.py receive, REST send |
| 44 | Slack | `gateway/platforms/slack.py` – Socket Mode (slack-bolt) receive, Web API send & file upload |
| 45 | WhatsApp | `gateway/platforms/whatsapp.py` – Cloud API webhook with HMAC signature verification |
| 46 | Signal | `gateway/platforms/signal.py` – signal-cli-rest-api |
| 47 | email | `gateway/platforms/email.py` – IMAP in, SMTP out, threaded replies |
| 48 | Microsoft Teams | `gateway/platforms/teams.py` – Bot Framework REST (secret-path webhook) |
| 49 | Google Chat | `gateway/platforms/google_chat.py` – HTTP Chat app, sync reply + webhook fallback |
| 50 | delivery routing | `gateway/delivery.py` – `origin`, `local`, `<platform>` (home channel), `<platform>:<chat>[:thread]`; `send_message` tool, `hermes send` |

Also: per-chat persistent sessions, DM pairing codes (`hermes pairing approve`), group mention rules, chat-based approvals (`/approve`), voice-note transcription, `/voice on` spoken replies, interrupt-on-new-message, generic `webhook` platform.

## 6 · Automation (51-60) — goal: automate repeatable work

| # | Item | Implementation |
|---|------|----------------|
| 51 | cronjob | `hermes/tools/cron_tool.py`, `hermes/cron/jobs.py`, `hermes cron …` |
| 52 | scheduled tasks | `CronTicker` inside the gateway; or `hermes cron tick` from system cron |
| 53 | natural-language schedules | `hermes/cron/schedule.py` – "30m", "every 2h", "weekdays at 6pm", "every monday at 8:30", cron |
| 54 | pause/resume jobs | `cronjob(action=pause/resume)`, `hermes cron pause <id>`; auto-pause after 5 failures |
| 55 | notifications | results delivered via delivery routing; `[SILENT]` suppresses no-change runs; failures alert |
| 56 | `delegate_task` | `hermes/tools/delegate.py` |
| 57 | isolated subagents | fresh context, restricted toolsets, own terminal, no memory/messaging/recursion |
| 58 | parallel workstreams | `tasks: [...]` batch mode, `delegation.max_concurrent` |
| 59 | `execute_code` | `hermes/tools/code_exec.py` – Python script calling tools over a token-guarded local RPC |
| 60 | event hooks | `hermes/hooks.py` – `~/.hermes/hooks/<name>/HOOK.yaml + handler.py`, plugin hooks, `tool:pre` veto |

## 7 · Browser & MCP (61-70) — goal: connect web & tools

| # | Item | Implementation |
|---|------|----------------|
| 61 | browser_exec | `browser_*` tools; Playwright runs on a dedicated thread |
| 62 | browser navigation | `browser_navigate`, `browser_click`, `browser_back`, `browser_scroll`, `browser_press` |
| 63 | page snapshots | `browser_snapshot` – page text + interactive elements with `@eN` refs |
| 64 | browser vision | `browser_vision` – screenshot + question to a vision model |
| 65 | form filling | `browser_type` (text & select, `submit=true`); lite engine submits real HTML forms |
| 66 | MCP over stdio | `hermes/mcp/client.py` `StdioTransport` |
| 67 | MCP over HTTP | `HTTPTransport` – Streamable HTTP, JSON or SSE, `Mcp-Session-Id` |
| 68 | server tool filtering | `tools: {include: [...], exclude: [...]}` per server |
| 69 | external APIs | MCP servers + plugins (`examples/plugins/weather`) + `execute_code` |
| 70 | web research | bundled skills `research-brief`, `web-research` |

## 8 · Deployment (71-80) — goal: run Hermes reliably

| # | Item | Implementation |
|---|------|----------------|
| 71 | local machine | `scripts/install.sh`, `terminal.backend: local` |
| 72 | Docker | `Dockerfile`, `docker-compose.yml`; `terminal.backend: docker` (hardened container per session) |
| 73 | SSH | `terminal.backend: ssh` with ControlMaster multiplexing |
| 74 | VPS | `hermes gateway install` (systemd user unit / launchd), `deploy/hermes-gateway.service`, `deploy/Caddyfile` |
| 75 | Daytona | `terminal.backend: daytona` (`pip install daytona`) |
| 76 | Modal | `terminal.backend: modal` (`pip install modal`) |
| 77 | environment variables | `.env` loader, `hermes config env`, `terminal.env_passthrough` |
| 78 | credentials | `.env` mode 600, secrets never in config, credential pools, redaction |
| 79 | terminal backends | `hermes/tools/environments.py` – local, docker, ssh, daytona, modal |
| 80 | security and isolation | docker `--cap-drop ALL`, `no-new-privileges`, pids/memory limits, network option; SSRF guard; protected paths |

## 9 · Best practices (81-90) — goal: build safe, reliable workflows

| # | Item | Implementation |
|---|------|----------------|
| 81 | least privilege | per-platform toolsets, `disabled_tools`, subagent restrictions, secret-free child env |
| 82 | approve risky actions | `hermes/security/approval.py` – 25+ dangerous patterns; once/session/always/deny |
| 83 | protect secrets | `hermes/security/redact.py` on tool output, logs and memory |
| 84 | review tool permissions | `hermes tools`, approvals log, `approvals.allowlist`, `hermes doctor` warns on `mode: off` |
| 85 | checkpoints before edits | `hermes/security/checkpoints.py` – shadow git snapshots; `/rollback`, `hermes checkpoints` |
| 86 | verify outputs | operating rules + `agent.verify_outputs: true` self-check pass; `safe-code-change` skill |
| 87 | clear task scopes | `--toolsets`, `--cwd`, cron/kanban per-job toolsets, self-contained job prompts |
| 88 | monitor scheduled jobs | `hermes cron list/status/logs`, last_status, failure counters, auto-pause |
| 89 | focused skills | `hermes skills check` linter, `skill-authoring` skill |
| 90 | backups | `hermes backup` / `hermes restore` (consistent SQLite snapshot, secrets excluded by default) |

## 10 · Power user (91-100) — goal: build an agent system that scales

| # | Item | Implementation |
|---|------|----------------|
| 91 | specialist bots | profiles: `hermes profile create researcher`, `hermes -p researcher` (own SOUL, memory, skills, config) |
| 92 | Kanban multi-agent | `hermes/power/kanban.py`, `kanban` tool, `hermes kanban add/list/work` with dependencies |
| 93 | provider routing | `provider_routing` (OpenRouter), `smart_model_routing` |
| 94 | fallback providers | `fallback_providers` → `FallbackProvider` |
| 95 | credential pools | `credential_pools` → `CredentialPool` rotation on 401/402/403/429 |
| 96 | API server | `hermes serve` – OpenAI-compatible `/v1/chat/completions` (+ streaming), `/v1/models` |
| 97 | ACP editor integration | `hermes acp` – Agent Client Protocol over stdio |
| 98 | voice mode | `hermes voice`, `/voice` – record → STT → agent → TTS |
| 99 | batch processing | `hermes batch prompts.jsonl --workers 8` → results + ShareGPT trajectories |
| 100 | insights & cost tracking | per-call usage & cost estimates in `state.db`; `hermes insights`, `/usage` |

## Bottom panels

* **Core building blocks** – Agent (`agent.py`), Tool & Toolset (`tools/registry.py`), Skill (`skills/`), Memory (`memory/`), Gateway (`gateway/`).
* **Popular channels / common tools** – see sections 5 and 2.
* **Safety checklist** – protect API keys, least privilege, approve sensitive actions, isolate execution, review logs, checkpoint file changes: section 9.
* **Example workflow (Request → Plan & Delegate → Search/Browse → Draft & Verify → Deliver)** – the `research-brief` skill walks exactly this path using `todo`, `delegate_task`, `web_*`/`browser_*`, verification and delivery.
* **How Hermes works (Input → Reason → Act → Learn)** – `AIAgent.run_conversation`: message or cron input → model with context → tools/skills/subagents → memory + skill nudges.
* **Use cases & workflow ideas** – bundled skills: research briefs, inbox triage, lead qualification, scheduled reports, browser research, file operations, customer support, multi-agent projects, content briefs, reminders.
* **Level-up path** – Install & explore → add tools & memory → create reusable skills → connect channels & MCP → automate & deploy.
