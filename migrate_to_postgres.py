#!/usr/bin/env python3
"""
Migrate data from local SQLite database to PostgreSQL.

Usage:
    DATABASE_URL="postgresql://user:pass@host:5432/dbname" python3 migrate_to_postgres.py

Reads from the local SQLite DB at data/nats_milb.db and inserts all rows
into the PostgreSQL database specified by DATABASE_URL.
"""

import os
import sys
import sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    print("ERROR: Set DATABASE_URL environment variable first.")
    print('  export DATABASE_URL="postgresql://user:pass@host:5432/dbname"')
    sys.exit(1)

import psycopg2
from config import DB_PATH

# ── Connect to both databases ───────────────────────────────────────

sqlite_conn = sqlite3.connect(str(DB_PATH))
sqlite_conn.row_factory = sqlite3.Row

pg_conn = psycopg2.connect(DATABASE_URL)
pg_cur = pg_conn.cursor()

# ── Create Postgres schema ──────────────────────────────────────────

PG_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS games (
        game_pk     INTEGER PRIMARY KEY,
        date        TEXT NOT NULL,
        level       TEXT NOT NULL,
        team_id     INTEGER NOT NULL,
        team_name   TEXT NOT NULL,
        opponent    TEXT NOT NULL,
        is_home     INTEGER NOT NULL,
        our_score   INTEGER,
        opp_score   INTEGER,
        result      TEXT,
        status      TEXT NOT NULL,
        source_org  TEXT DEFAULT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS hitting_lines (
        id          SERIAL PRIMARY KEY,
        game_pk     INTEGER NOT NULL REFERENCES games(game_pk),
        date        TEXT NOT NULL,
        level       TEXT NOT NULL,
        player_name TEXT NOT NULL,
        position    TEXT,
        ab          INTEGER DEFAULT 0,
        r           INTEGER DEFAULT 0,
        h           INTEGER DEFAULT 0,
        doubles     INTEGER DEFAULT 0,
        triples     INTEGER DEFAULT 0,
        hr          INTEGER DEFAULT 0,
        rbi         INTEGER DEFAULT 0,
        bb          INTEGER DEFAULT 0,
        k           INTEGER DEFAULT 0,
        sb          INTEGER DEFAULT 0,
        e           INTEGER DEFAULT 0,
        source_org  TEXT DEFAULT NULL,
        UNIQUE(game_pk, player_name)
    )""",
    """CREATE TABLE IF NOT EXISTS pitching_lines (
        id          SERIAL PRIMARY KEY,
        game_pk     INTEGER NOT NULL REFERENCES games(game_pk),
        date        TEXT NOT NULL,
        level       TEXT NOT NULL,
        player_name TEXT NOT NULL,
        position    TEXT,
        ip          REAL DEFAULT 0,
        h           INTEGER DEFAULT 0,
        r           INTEGER DEFAULT 0,
        er          INTEGER DEFAULT 0,
        bb          INTEGER DEFAULT 0,
        k           INTEGER DEFAULT 0,
        hr          INTEGER DEFAULT 0,
        decision    TEXT DEFAULT '',
        source_org  TEXT DEFAULT NULL,
        UNIQUE(game_pk, player_name)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_games_date ON games(date)",
    "CREATE INDEX IF NOT EXISTS idx_games_level ON games(level)",
    "CREATE INDEX IF NOT EXISTS idx_hitting_player ON hitting_lines(player_name)",
    "CREATE INDEX IF NOT EXISTS idx_hitting_date ON hitting_lines(date)",
    "CREATE INDEX IF NOT EXISTS idx_pitching_player ON pitching_lines(player_name)",
    "CREATE INDEX IF NOT EXISTS idx_pitching_date ON pitching_lines(date)",
]

print("Creating Postgres schema...")
for stmt in PG_SCHEMA:
    pg_cur.execute(stmt)
pg_conn.commit()

# ── Migrate games ───────────────────────────────────────────────────

print("Migrating games...")
games = sqlite_conn.execute("SELECT * FROM games").fetchall()
count = 0
for g in games:
    pg_cur.execute("""
        INSERT INTO games
            (game_pk, date, level, team_id, team_name, opponent,
             is_home, our_score, opp_score, result, status, source_org)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (game_pk) DO NOTHING
    """, (
        g["game_pk"], g["date"], g["level"], g["team_id"],
        g["team_name"], g["opponent"], g["is_home"],
        g["our_score"], g["opp_score"], g["result"], g["status"],
        g["source_org"],
    ))
    count += 1
pg_conn.commit()
print(f"  {count} game rows processed.")

# ── Migrate hitting_lines ──────────────────────────────────────────

print("Migrating hitting_lines...")
rows = sqlite_conn.execute("SELECT * FROM hitting_lines").fetchall()
count = 0
for r in rows:
    pg_cur.execute("""
        INSERT INTO hitting_lines
            (game_pk, date, level, player_name, position,
             ab, r, h, doubles, triples, hr, rbi, bb, k, sb, e, source_org)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (game_pk, player_name) DO NOTHING
    """, (
        r["game_pk"], r["date"], r["level"], r["player_name"], r["position"],
        r["ab"], r["r"], r["h"], r["doubles"], r["triples"], r["hr"],
        r["rbi"], r["bb"], r["k"], r["sb"], r["e"] if "e" in r.keys() else 0,
        r["source_org"] if "source_org" in r.keys() else None,
    ))
    count += 1
pg_conn.commit()
print(f"  {count} hitting rows processed.")

# ── Migrate pitching_lines ─────────────────────────────────────────

print("Migrating pitching_lines...")
rows = sqlite_conn.execute("SELECT * FROM pitching_lines").fetchall()
count = 0
for r in rows:
    pg_cur.execute("""
        INSERT INTO pitching_lines
            (game_pk, date, level, player_name, position,
             ip, h, r, er, bb, k, hr, decision, source_org)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (game_pk, player_name) DO NOTHING
    """, (
        r["game_pk"], r["date"], r["level"], r["player_name"], r["position"],
        r["ip"], r["h"], r["r"], r["er"], r["bb"], r["k"],
        r["hr"] if "hr" in r.keys() else 0,
        r["decision"],
        r["source_org"] if "source_org" in r.keys() else None,
    ))
    count += 1
pg_conn.commit()
print(f"  {count} pitching rows processed.")

# ── Done ────────────────────────────────────────────────────────────

sqlite_conn.close()
pg_cur.close()
pg_conn.close()

print("\nMigration complete!")
print("Set DATABASE_URL on Railway and deploy.")
