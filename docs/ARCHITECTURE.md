# Architecture

```
           ┌──────────── inputs ─────────────┐
 CLI REPL ─┤ Telegram Discord Slack WhatsApp │  cron ticker   API server   ACP (editors)   batch
           │ Signal Email Teams Google Chat  │      │              │            │            │
           └───────────────┬─────────────────┘      │              │            │            │
                    GatewayRunner                   │              │            │            │
         (sessions, pairing, slash cmds,            │              │            │            │
          approvals over chat, voice, media)        │              │            │            │
                           └──────────────┬─────────┴──────────────┴────────────┴────────────┘
                                          ▼
                                   AIAgent (agent.py)
   system prompt = SOUL.md + rules + frozen MEMORY/USER snapshot + skills index + AGENTS.md + env
   loop:  provider.chat(messages, tools) ─► tool calls ─► registry.run_tool ─► results ─► …
          • approvals (security/approval.py)   • checkpoints before edits
          • secret redaction & output limits    • hooks (tool:pre veto, tool:post, agent:*)
          • compression near context limit      • usage/cost → state.db
          • memory & skill nudges (LEARN)        • optional verify pass
                                          │
              ┌───────────────┬───────────┴────────────┬─────────────────┐
              ▼               ▼                        ▼                 ▼
         Providers        Tools/Toolsets           Memory/Skills       Sessions DB
  OpenAI-compatible,   web, terminal (5 backends),  MEMORY.md USER.md   SQLite + FTS5
  Anthropic SDK, mock, files, browser, vision,     SKILL.md library    (search, insights)
  fallback chain,      image, tts, memory, skills,  Skills Hub
  credential pools     delegate, cron, code, MCP…
```

## Message format

Internally messages are OpenAI-style dicts. Providers translate:

* `OpenAICompatProvider` – Chat Completions over stdlib HTTP (OpenRouter routing, credential pools, retries).
* `AnthropicProvider` – the official `anthropic` SDK: streaming + `get_final_message()`, adaptive thinking with
  `output_config.effort`, automatic prompt caching, server-side refusal fallbacks on supported models, all tool results
  for a turn in one user message, and native assistant blocks (including thinking) replayed unchanged via `_raw`.
  After context compression rewrites history, native blocks are dropped and only text/tool calls are replayed.

## Extension points

| Extension | How |
|-----------|-----|
| Tool | `@tool(...)` in a module, or a plugin's `ctx.register_tool` |
| Toolset | the `toolset` field of a tool; composites in `registry.COMPOSITES` |
| Platform | subclass `PlatformAdapter`, add to `gateway/platforms/__init__.py` |
| Provider | subclass `Provider`, wire in `providers/__init__.py:build_single` |
| Memory provider | subclass `MemoryProvider`, `ctx.register_memory_provider` |
| Hook | `~/.hermes/hooks/<name>/HOOK.yaml` + `handler.py` |
| Slash command | `ctx.register_command(name, fn)` |
| Skill | `SKILL.md` in `~/.hermes/skills/` |
| MCP | `mcp_servers:` in config.yaml |

## Files under `~/.hermes`

```
config.yaml  .env  SOUL.md  state.db  kanban.db (base home)
memories/MEMORY.md  memories/USER.md
skills/<category>/<name>/SKILL.md   skills/.hub/lock.json
cron/jobs.json  cron/output/<job>/<timestamp>.md
gateway/sessions.json  gateway/pairing.json
checkpoints/<hash>/ (shadow git)   logs/  cache/  plugins/  hooks/
profiles/<name>/  (each a full home)
```
