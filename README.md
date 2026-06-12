# bolt (wcbot)

A Telegram World Cup prediction game. Users predict match results and player wagers in private
DMs; points are scored after each match and leaderboards are published to group chats.

See [CLAUDE.md](CLAUDE.md) for the full product spec, scoring rules, and architecture rules.

## Architecture

Monolith. One FastAPI process that, on startup, launches:

- the **aiogram** bot in long-polling mode (no public HTTPS / webhook needed), and
- the **APScheduler** cron jobs (Daily Blast, Slacker Warning, Lineup Lockdown, Post-Match
  Analysis, Daily Reveal).

```
config/      env settings + async DB engine/session
database/    schema.sql (the canonical DDL) + SQLAlchemy 2.0 models
services/    ALL business logic (sports API, scoring, predictions, leaderboards, cron)
bot/         FastAPI app + thin aiogram handlers (validate -> call service -> reply)
```

Handlers never run SQL, never score, never call external APIs — that all lives in `services/`.

## Setup

1. **Install deps** (Python 3.12):
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure env**:
   ```bash
   cp .env.example .env
   # then fill in BOT_TOKEN, DATABASE_URL, API_FOOTBALL_KEY
   ```

3. **Create the schema** (skip if the database already has the tables from CLAUDE.md §4):
   ```bash
   psql "postgresql://user:password@localhost:5432/wcbot" -f database/schema.sql
   ```
   Note: `schema.sql` uses a plain `postgresql://` URL; `DATABASE_URL` in `.env` uses the
   `postgresql+asyncpg://` form for the app.

## Run

```bash
uvicorn bot.main:app --reload
```

- `GET /health` → `{"status": "ok"}`
- Bot logs "Polling started" once aiogram is up.

## Run with Docker

```bash
docker build -t wcbot .
docker run --env-file .env -p 8000:8000 wcbot
```

Postgres is expected to be provided externally via `DATABASE_URL`.

## Key conventions

- **Locking** is time-derived: a match locks 60 minutes before kickoff. There is no stored
  `LOCKED` status (the `matches.status` enum is `SCHEDULED / IN_PROGRESS / FINISHED`).
- **matchday_id** is the Singapore calendar date of kickoff encoded as `YYYYMMDD`
  (e.g. `20260611`).
- **Scoring is idempotent**: re-running it produces identical results.
