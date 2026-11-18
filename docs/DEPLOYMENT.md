# Deploying Hermes

Hermes is a **long-running agent**: it keeps memory, skills, sessions and scheduled jobs on disk,
holds live chat runs in memory, fires cron jobs on its own and listens to chat platforms. It needs
an always-on process with a persistent disk.

One container runs everything on a single port (`$PORT`, default 8080):

```
hermes serve --gateway --host 0.0.0.0
  /            web dashboard        (sign in: /?token=<HERMES_WEB_TOKEN>)
  /v1/...      OpenAI-compatible API (Authorization: Bearer <API_SERVER_KEY>)
  /health      health check
  /whatsapp, /teams/..., /google_chat/..., /webhook/...   messaging webhooks
  + scheduled jobs and Telegram/Discord/Slack/Signal/email listeners in the same process
```

State lives under `/data` (`/data/hermes` = HERMES_HOME, `/data/workspace` = the agent's working
directory). **Mount a persistent volume at `/data`** or everything resets on each deploy.

## Required environment variables

| Variable | Purpose |
|----------|---------|
| `HERMES_WEB_TOKEN` | dashboard sign-in secret (required when listening publicly) |
| `API_SERVER_KEY` | bearer key for `/v1` (required when listening publicly) |
| one model key | e.g. `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`, `NOUS_API_KEY`, `OPENAI_API_KEY` |
| `HERMES_PROVIDER`, `HERMES_MODEL` | optional defaults, e.g. `openrouter` + `anthropic/claude-opus-5-5`, or `anthropic` + `claude-opus-5-5` |
| `HERMES_ALLOWED_HOSTS` | extra public hostnames (custom domains). Render, Fly and Railway hostnames are detected automatically |
| platform tokens | `TELEGRAM_BOT_TOKEN`, `SLACK_BOT_TOKEN`, ... (see `examples/.env.example`) |

Settings you change in the dashboard are saved to `/data/hermes/config.yaml` and take precedence
over `HERMES_PROVIDER` / `HERMES_MODEL`.

## Render (recommended: deploys from GitHub)

1. In Render: **New → Blueprint**, pick this repository. Render reads `render.yaml`.
2. Enter your model key (e.g. `OPENROUTER_API_KEY`) when prompted. `HERMES_WEB_TOKEN` and
   `API_SERVER_KEY` are generated for you.
3. Deploy. The service gets a 5 GB disk at `/data` (persistent disks need a paid instance type).
4. Open `https://<service>.onrender.com/?token=<HERMES_WEB_TOKEN>` (copy the token from the
   service's Environment tab). Every push to `main` redeploys automatically.

## Fly.io

```bash
fly launch --no-deploy --copy-config        # choose a unique app name
fly volumes create hermes_data --size 5
fly secrets set HERMES_WEB_TOKEN=$(openssl rand -hex 24) API_SERVER_KEY=$(openssl rand -hex 24) OPENROUTER_API_KEY=sk-or-...
fly deploy
```

Open `https://<app>.fly.dev/?token=<HERMES_WEB_TOKEN>`. `fly.toml` keeps one machine running
(scheduled jobs and chat bots need it awake).

## Railway

Create a service from this repo (it builds the Dockerfile), add a volume mounted at `/data`, set the
variables above, and generate a public domain. `RAILWAY_PUBLIC_DOMAIN` is picked up automatically.

## Docker / docker compose (your own machine or VPS)

```bash
cat > .env <<'ENV'
HERMES_WEB_TOKEN=change-me-to-a-long-random-string
API_SERVER_KEY=change-me-too
OPENROUTER_API_KEY=sk-or-...
ENV
docker compose up -d
open "http://127.0.0.1:8642/?token=change-me-to-a-long-random-string"
```

For a public VPS put HTTPS in front (`deploy/Caddyfile`), add your domain to
`HERMES_ALLOWED_HOSTS`, or run without Docker using `hermes gateway install` / `deploy/hermes-gateway.service`.

## Why not Vercel (or other serverless platforms)?

Serverless functions are short-lived and stateless: in-memory state isn't shared between requests,
work stops when the response is sent, only `/tmp` is writable (and it is wiped), and nothing runs
between requests. Hermes would lose memory, skills and sessions, scheduled jobs and chat bots
would never run, and long agent runs would hit request timeouts. Use a container host instead.
If your repository is connected to Vercel, disconnect it (Project → Settings → Git) or remove the
project so pushes stop triggering failing builds.

## Security notes

- The agent runs commands **inside the container**; keep `approvals.mode: manual` (the default) so
  risky commands wait for your approval in the dashboard.
- Keep `HERMES_WEB_TOKEN` and `API_SERVER_KEY` long and secret; rotate by changing the variable and redeploying.
- The container drops root before starting (`deploy/docker-entrypoint.sh`); the dashboard rejects
  unknown Host headers, sets `Secure` cookies behind HTTPS and enforces a strict CSP.
