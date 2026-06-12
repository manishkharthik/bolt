# Deploying bolt (wcbot) to Railway

The bot runs as a single always-on container: FastAPI hosts a `/health` endpoint while aiogram
long-polls Telegram and APScheduler runs the cron jobs — all in one process. No public traffic
is required; the health endpoint just lets Railway confirm the container is alive.

**Database:** we use the existing **Supabase** Postgres via `DATABASE_URL`. Do **not** add a
Railway Postgres plugin.

## Prerequisites

- A Railway account.
- Railway CLI (no Git repo needed — we upload the directory directly):
  ```bash
  npm i -g @railway/cli      # or: brew install railway
  railway login
  ```

## One-time setup

1. **Rotate secrets first** (they were exposed during development):
   - API-Football: regenerate the key in your API-SPORTS dashboard.
   - Supabase: reset the database password.

2. **Create the project** from this directory:
   ```bash
   cd /Users/shru/Desktop/wcbot
   railway init            # create a new project (give it a name)
   ```

3. **Set environment variables** (in the dashboard → Variables, or via CLI):
   ```bash
   railway variables --set "BOT_TOKEN=<from @BotFather>"
   railway variables --set "DATABASE_URL=postgresql+asyncpg://postgres.<...>:<password>@aws-1-ap-northeast-1.pooler.supabase.com:6543/postgres"
   railway variables --set "API_FOOTBALL_KEY=<new api-sports key>"
   railway variables --set "API_FOOTBALL_HOST=https://v3.football.api-sports.io"
   railway variables --set "LEAGUE_ID=1"
   railway variables --set "SEASON=2026"
   railway variables --set "TZ=Asia/Singapore"
   ```
   Keep the `+asyncpg` driver and the `:6543` pooler port — `config/db.py` is already configured
   for Supabase's transaction pooler.

## Deploy

```bash
railway up
```

This uploads the directory, builds the `Dockerfile`, and starts one replica (per `railway.toml`).
`.dockerignore` keeps `.env`, `.venv`, and `.git` out of the image.

## Verify

- Railway dashboard → Deployments → logs should show:
  `Scheduler started` → `Polling started`.
- The deploy goes healthy once `/health` responds (Railway hits it on the assigned `$PORT`).
- In Telegram: DM the bot `/start`, then `/matchday` / `/status`.

## Critical: keep it to ONE replica

Telegram allows only one poller per bot token. `railway.toml` sets `numReplicas = 1`; do not
raise it and do not enable autoscaling, or you'll get `409 Conflict` errors.

## Schema

The Supabase DB already has the tables (predictions/sync have been writing to it). For a fresh
database, load `database/schema.sql` once and ensure `world_cup_players` is populated.

## Updating

Re-run `railway up` after code changes. (Optional: connect a GitHub repo in the Railway
dashboard for auto-deploys on push instead of manual `railway up`.)
