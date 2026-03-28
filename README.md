# Washington Nationals Minor League Tracker

A Flask web app that pulls daily box scores, season stats, and trends for
all Nationals minor league affiliates from the MLB Stats API, stores
everything in PostgreSQL, and highlights top 30 prospects. Hosted on
Railway with a cron job that checks for new game data every 10 minutes.

**Live site:** Hosted on Railway (Flask + Gunicorn)

## Architecture

```
app.py              Flask web app (routes, caching, template filters)
nats_tracker.py     CLI entry point (fetch / backfill / season / info)
api.py              MLB Stats API client (schedules, box scores, game logs)
db.py               Database layer (PostgreSQL + SQLite fallback, schema, queries)
config.py           Affiliate config, DB paths, prospect loader
dashboard.py        Static HTML dashboard generator (legacy)
prospects.json      Editable prospect list with MLB / FanGraphs / BA rankings
templates/          Jinja2 templates (day, season, players, player profile)
```

## How It Works

1. **Data ingestion** — A Railway cron job runs `python nats_tracker.py fetch`
   every 10 minutes, pulling schedules and box scores from the free MLB Stats
   API for each Nationals affiliate and upserting results into PostgreSQL.
2. **Web dashboard** — Flask serves the data with five main views:
   - `/day/<date>` — game results, full box scores, key performers
   - `/season` — season-to-date prospect stats (OPS, WHIP, K/9)
   - `/players` — all hitters and pitchers aggregated by year
   - `/player/<name>` — individual game log with 7/15-day rolling trends
3. **Caching** — Responses are cached in-memory with a 5-minute TTL to keep
   page loads fast.

## Deployment (Railway)

The app runs on Railway with two services:

- **Web** — `gunicorn app:app --bind 0.0.0.0:$PORT` (see `Procfile`)
- **Cron** — Runs `python nats_tracker.py fetch --today` every 10 minutes to
  pick up completed games throughout the evening

**Key environment variables:**

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string (provided by Railway Postgres plugin) |
| `PORT` | Dynamic port for the web service (set by Railway) |
| `NATS_DATA_DIR` | Override data directory (default: `./data`) |

**Config files:**

- `Procfile` — Gunicorn web process
- `railway.toml` — Nixpacks builder
- `runtime.txt` — Python 3.12.x

## Database

PostgreSQL in production (Railway plugin), with automatic SQLite fallback for
local development. Three main tables:

- **games** — game results per affiliate per date (W/L, score, level)
- **hitting_lines** — one row per batter per game (AB, H, HR, RBI, BB, K, SB, …)
- **pitching_lines** — one row per pitcher per game (IP, H, ER, BB, K, decision, …)

Season aggregates, rolling averages, and bulk queries are computed in SQL.
Composite indexes on `(player_name, date)` keep queries fast.

## Local Development

```bash
pip install -r requirements.txt

# Uses SQLite by default when DATABASE_URL is not set
python nats_tracker.py fetch            # Fetch yesterday's games
python nats_tracker.py fetch 2026-04-15 # Fetch a specific date
python nats_tracker.py backfill 2026-04-01 2026-04-15  # Backfill a range

# Run the web app locally
python app.py   # → http://localhost:5000
```

### CLI Commands

| Command | Description |
|---|---|
| `fetch [date]` | Fetch box scores for a date (default: yesterday) |
| `fetch --today` | Fetch today's games (used by cron) |
| `backfill <start> <end>` | Fetch a range of dates |
| `season` | Export season-to-date prospect stats as CSV |
| `info` | Show DB stats and affiliate information |
| `update-prospects` | Interactive helper to edit prospect list |
| `lookup-player <name>` | Find a player's MLB ID |
| `backfill-player <name>` | Fetch full game log for a prospect |

## Prospect Rankings

Rankings from three sources are stored in `prospects.json` and shown as
color-coded badges in the UI:

- **Red** — MLB Pipeline
- **Green** — FanGraphs
- **Navy** — Baseball America

Edit `prospects.json` directly, or use `python nats_tracker.py update-prospects`
for a guided CSV round-trip workflow.

## Data Source

Free MLB Stats API (no auth required):
- Base: `https://statsapi.mlb.com/api/v1/`
- Schedule, box scores, game logs, team lookups
