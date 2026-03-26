# Washington Nationals Minor League Tracker

Track daily box scores, season stats, and trends for all Nationals minor
league affiliates with special focus on top 30 prospects.

## Architecture

```
nats_tracker.py     CLI entry point (fetch / dashboard / season / backfill / info)
config.py           Affiliate config, DB paths, prospect loader
api.py              MLB Stats API client
db.py               SQLite schema, inserts, and aggregation queries
dashboard.py        HTML dashboard generator
prospects.json      Editable prospect list with MLB / FanGraphs / BA rankings
data/
  nats_milb.db      SQLite database (auto-created)
  nats_dashboard.html   Latest dashboard (single file, always overwritten)
```

## Quick Start

```bash
pip install -r requirements.txt

# Fetch yesterday's data (auto-generates dashboard)
python3 nats_tracker.py fetch

# Fetch a specific date
python3 nats_tracker.py fetch 2026-04-15

# Backfill a date range (e.g., opening week)
python3 nats_tracker.py backfill 2026-04-01 2026-04-15

# Re-generate dashboard for any stored date
python3 nats_tracker.py dashboard 2026-04-10

# Export season CSV stats
python3 nats_tracker.py season

# Show DB info and records
python3 nats_tracker.py info
```

## How It Works

**`fetch`** queries the MLB Stats API for each affiliate's schedule on the
target date, pulls full box scores for completed games, and stores
everything in `data/nats_milb.db`. It auto-generates a dashboard
afterwards (skip with `--no-dashboard`).

**`dashboard`** reads from the SQLite DB — not from the API — so it's
instant and works offline. The HTML includes:

- Game results per affiliate with W-L records
- Full hitter and pitcher stat lines (prospects highlighted in gold)
- Key Performers section ranked by a weighted performance score
- Season-to-date prospect tracker with OPS, WHIP, K/9
- 7-day and 15-day rolling trends with hot/cold arrows
- Date navigation (prev / next)

**`backfill`** is just `fetch` in a loop — great for catching up or
starting fresh mid-season.

**`season`** exports a CSV of season-to-date prospect stats that opens
cleanly in Excel.

## Prospect Rankings (MLB / FanGraphs / BA)

Rankings from all three sources are stored in `prospects.json`. In the
dashboard, each prospect shows three color-coded rank pips:

- Red = MLB Pipeline
- Green = FanGraphs
- Navy = Baseball America

### Updating the Prospect List

Option A — **Interactive helper:**
```bash
python3 nats_tracker.py update-prospects
```
This lets you export to CSV, edit in a spreadsheet, and re-import.

Option B — **Edit `prospects.json` directly.** It's plain JSON.

Option C — **CSV round-trip:** Export with option 1, edit in Excel/Sheets,
import with option 2.

## Data Storage

All data lives in `data/nats_milb.db` (SQLite). No files pile up — the
dashboard is always a single HTML file that gets regenerated.

Override the data directory with the `NATS_DATA_DIR` env var:
```bash
export NATS_DATA_DIR=~/nats-data
python3 nats_tracker.py fetch
```

## Automation (Optional)

Add a cron job to fetch daily:
```bash
# Every morning at 8am, fetch yesterday's games
0 8 * * * cd /path/to/nats-milb-tracker && python3 nats_tracker.py fetch
```

## Data Source

Free MLB Stats API (no auth required):
- Base: `https://statsapi.mlb.com/api/v1/`
- Schedule: `/schedule?teamId=XXX&date=YYYY-MM-DD`
- Box scores: `/game/GAMEPK/boxscore`
- Teams: `/teams?sportIds=11,12,13,14`
