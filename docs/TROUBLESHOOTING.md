# Troubleshooting

Start with:

```bash
hermes doctor          # config, keys, binaries, tools, skills, db, platforms, cron, MCP
hermes doctor --ping   # also makes one real model call
hermes doctor --fix    # create missing dirs, fix .env permissions
hermes logs --errors   # last 50 lines of ~/.hermes/logs/errors.log (secrets redacted)
```

| Symptom | Fix |
|---------|-----|
| `No API key for provider 'openrouter'` | `hermes setup` or `hermes config env OPENROUTER_API_KEY sk-or-...` |
| `The anthropic provider needs the official SDK` | `pip install 'hermes-agent-replica[anthropic]'` |
| Model error 404 / unknown model | `hermes model --list` and pick an exact id; check provider prefix (`openrouter` uses `vendor/model`) |
| Every call fails / rate limited | add `fallback_providers` and/or `credential_pools` in config.yaml |
| "Command blocked … user did not approve" | expected for risky commands; approve in the prompt, use `/approve` in chat, or allowlist the exact command |
| Tool missing from the agent | `hermes tools` shows availability (missing key/binary) and whether its toolset is enabled for that platform |
| Browser pages empty | the lite engine has no JavaScript: `pip install playwright && playwright install chromium` |
| Bot doesn't answer in a group | mention it, or set `gateway.group_require_mention: false` |
| Bot replies with a pairing code | approve yourself: `hermes pairing approve telegram <CODE>` or set `TELEGRAM_ALLOWED_USERS` |
| Cron jobs never run | jobs fire while `hermes gateway run` is up, or add `* * * * * hermes cron tick` to your system crontab |
| Context too long | `/compress`, lower `compression.threshold`, or start `/new` |
| Wrong edit made by the agent | `/rollback` (or `hermes checkpoints list` / `rollback <sha>`) |
| MCP server tools missing | `hermes mcp test <name>`; check the command exists and env vars referenced as `${VAR}` are set |
| Need a clean slate | `hermes backup` then remove `~/.hermes` and run `hermes setup` |
| Vercel build: `PYTHON_ENTRYPOINT_NOT_FOUND` | Hermes isn't a serverless app; deploy the container instead (Render/Fly/Docker - see `docs/DEPLOYMENT.md`) and disconnect the Vercel project |
| Dashboard says "unexpected Host header" (421) behind a custom domain | add the domain to `HERMES_ALLOWED_HOSTS` |
| Container loses memory/sessions after a deploy | mount a persistent volume at `/data` |
