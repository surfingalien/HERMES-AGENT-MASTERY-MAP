# Hermes Agent Mastery Map — a working replica

A from-scratch, self-improving AI agent that implements **all 100 items** of
*The Complete Hermes Agent Mastery Map* — from first setup to an autonomous,
multi-platform agent system: tools & toolsets, persistent memory, skills that
improve from experience, a messaging gateway for 8+ platforms, cron automation,
subagents, browser automation, MCP, five terminal backends, safety controls and
power-user features (profiles, kanban, API server, ACP, voice, batch, insights).

> Independent educational reimplementation inspired by Nous Research's open-source
> Hermes Agent. Not affiliated with or endorsed by Nous Research.

* **Item-by-item coverage:** [`docs/MASTERY_MAP.md`](docs/MASTERY_MAP.md)
* **Architecture:** [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
* **Troubleshooting:** [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md)

## How it works

```
INPUT (message or schedule) → REASON (model + context) → ACT (tools, skills, subagents) → LEARN (memory + skill improvement)
```

The agent loop (`hermes/agent.py`) builds a stable system prompt from `SOUL.md`,
a frozen snapshot of `MEMORY.md`/`USER.md`, the skills index and project context
files, then lets the model call tools until the task is done. Along the way it
checkpoints files before edits, asks before risky commands, redacts secrets,
compresses long contexts, records usage/cost, and nudges itself to save memories
and turn complex procedures into skills.

## Quick start

```bash
git clone <this repo> && cd HERMES-AGENT-MASTERY-MAP
./scripts/install.sh            # venv + `hermes` on your PATH (Python 3.10+; Windows: use WSL)
hermes setup                    # pick a provider & model, terminal backend, approvals
# or: hermes setup --portal     # Nous Portal quick path
hermes doctor                   # verify everything
hermes                          # start chatting
```

Only one runtime dependency is required (PyYAML). Optional extras:
`pip install -e '.[anthropic]'` (native Claude via the official SDK),
`'.[browser]'` (Playwright), `'.[discord]'`, `'.[slack]'`, `'.[all]'`.

Try it fully offline with the mock provider: `hermes model mock:mock-1` then
`hermes -q 'call:terminal {"command": "uname -a"}'`.

## Useful commands

| Command | What it does |
|---------|--------------|
| `hermes` / `hermes -q "..."` | interactive chat / one-shot (`-c` continue, `--resume <id>`, `-t web,file`, `--yolo`) |
| `hermes setup [--portal]` | setup wizard |
| `hermes model [provider:model]` | choose provider & model (`--list` to browse) |
| `hermes tools [enable\|disable] <toolset> --platform telegram` | review and scope tools per platform |
| `hermes plugins [list\|create\|enable\|install]` | extend with plugins |
| `hermes send telegram "Deploy finished ✅"` | deliver a message to any platform |
| `hermes doctor [--fix] [--ping]` | diagnose problems |
| `hermes gateway setup\|run\|install` | messaging gateway (systemd/launchd service) |
| `hermes pairing approve telegram ABC123` | let a new user talk to your bot |
| `hermes sessions list\|search\|show\|export` | browse and search every past conversation |
| `hermes memory show\|add\|edit` | inspect persistent memory |
| `hermes skills list\|install\|check\|create` | skills & Skills Hub |
| `hermes cron create "weekdays at 8am" "Summarise top AI news" --deliver telegram` | scheduled tasks |
| `hermes mcp add fs -- npx -y @modelcontextprotocol/server-filesystem ~/docs` | MCP servers |
| `hermes profile create researcher` / `hermes -p researcher` | specialist bots |
| `hermes kanban add "..." --assignee writer` / `hermes kanban work` | multi-agent board |
| `hermes serve` | OpenAI-compatible API server |
| `hermes acp` | editor integration (Agent Client Protocol) |
| `hermes voice` | push-to-talk voice mode |
| `hermes batch prompts.jsonl --workers 8` | parallel batch runs + trajectories |
| `hermes insights` | tokens, tools and cost report |
| `hermes backup` / `hermes restore` | back up everything in `~/.hermes` |
| `hermes checkpoints list\|rollback <sha>` | undo file changes |

Inside chat: `/help`, `/new`, `/model`, `/skills`, `/<skill-name>`, `/memory`,
`/usage`, `/compress`, `/undo`, `/retry`, `/rollback`, `/personality`, `/voice`.
Reference files with `@path/to/file`, `@folder:src`, `@diff`, `@url:https://…`.

## Core building blocks

| Block | Definition | Code |
|-------|------------|------|
| **Agent** | model + tools + context | `hermes/agent.py` |
| **Tool** | callable capability | `hermes/tools/*.py` (32 built-in) |
| **Toolset** | grouped tools, enabled per platform | `hermes/tools/registry.py` |
| **Skill** | reusable procedure (`SKILL.md`) | `hermes/skills/`, 12 bundled in `hermes/bundled_skills/` |
| **Memory** | persistent context | `hermes/memory/`, `hermes/sessions/` |
| **Gateway** | messaging bridge | `hermes/gateway/` |

## Messaging channels

Telegram · Discord · Slack · WhatsApp (Cloud API) · Signal · Email · Microsoft Teams ·
Google Chat · generic webhook. Each chat keeps its own persistent session; unknown
users get a pairing code; risky commands are approved from the chat with `/approve`;
voice notes are transcribed; scheduled jobs deliver to home channels.

```bash
hermes gateway setup     # walks through tokens for each platform
hermes gateway run       # or: hermes gateway install (systemd / launchd)
```

## Safety checklist (built in)

- **Protect API keys** – `.env` with mode 600, redaction of tool output/logs/memory, secrets stripped from child processes
- **Least privilege** – per-platform toolsets, restricted subagents, protected paths
- **Approve sensitive actions** – dangerous-command detection with once/session/always/deny, manual/smart/off modes
- **Isolate execution** – Docker (cap-drop, no-new-privileges, limits), SSH, Daytona or Modal backends
- **Review logs** – `hermes logs`, `hermes insights`, approval log, hooks
- **Checkpoint file changes** – shadow-git snapshots before edits, `/rollback`

## Deploy

* **Local:** `./scripts/install.sh`
* **Docker:** `docker compose up -d` (data in `./hermes-data`, see `docker-compose.yml`)
* **VPS:** `hermes gateway install` (systemd user unit) or `deploy/hermes-gateway.service`; HTTPS via `deploy/Caddyfile`
* **Sandboxed execution:** `hermes config set terminal.backend docker|ssh|daytona|modal`

## Tests

```bash
python -m unittest discover -s tests -t .     # 74 tests, offline (mock provider)
```

## Known limitations

- Teams and Google Chat inbound requests are authenticated with a secret URL path plus
  user allowlists, not full JWT signature validation (the stdlib has no RS256); keep them behind HTTPS.
- Daytona and Modal backends follow their SDKs' documented APIs but were not exercised in CI.
- Discord and Slack receiving need their optional libraries; sending works with REST only.
- The Nous Portal path opens the portal and stores the API key you paste; it does not implement an OAuth device flow.
- Cost figures are estimates from a price table (override with `pricing:` in config).

## License

MIT — see [LICENSE](LICENSE).
