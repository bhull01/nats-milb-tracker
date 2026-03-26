"""
Database layer for storing and querying MiLB game data.

Supports PostgreSQL (via DATABASE_URL) for production/Railway
and falls back to SQLite for local development.

Schema:
  games          – one row per game per affiliate
  hitting_lines  – one row per batter per game
  pitching_lines – one row per pitcher per game

All stat aggregation (season totals, rolling averages, trends) is done
via SQL queries rather than in-memory Python, so the DB is the single
source of truth even across runs.
"""

import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

from config import DATABASE_URL, DB_PATH, DATA_DIR

log = logging.getLogger("nats_milb.db")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ENGINE DETECTION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

USE_PG = bool(DATABASE_URL)


def _ph(n: int = 1) -> str:
    """Return parameter placeholder(s): %s for Postgres, ? for SQLite."""
    return ", ".join(["%s"] * n) if USE_PG else ", ".join(["?"] * n)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SCHEMA
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

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

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
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
);

CREATE TABLE IF NOT EXISTS hitting_lines (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
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
);

CREATE TABLE IF NOT EXISTS pitching_lines (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
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
);

CREATE INDEX IF NOT EXISTS idx_games_date ON games(date);
CREATE INDEX IF NOT EXISTS idx_games_level ON games(level);
CREATE INDEX IF NOT EXISTS idx_hitting_player ON hitting_lines(player_name);
CREATE INDEX IF NOT EXISTS idx_hitting_date ON hitting_lines(date);
CREATE INDEX IF NOT EXISTS idx_pitching_player ON pitching_lines(player_name);
CREATE INDEX IF NOT EXISTS idx_pitching_date ON pitching_lines(date);
"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CONNECTION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _native(val):
    """Coerce psycopg2 Decimal/numeric types to native Python int/float."""
    from decimal import Decimal
    if isinstance(val, Decimal):
        return int(val) if val == int(val) else float(val)
    return val


class _DictCursor:
    """Wraps a psycopg2 cursor so fetchone/fetchall return dicts."""

    def __init__(self, cursor):
        self._cur = cursor

    def execute(self, sql, params=None):
        self._cur.execute(sql, params)
        return self

    def fetchone(self):
        if not self._cur.description:
            return None
        row = self._cur.fetchone()
        if row is None:
            return None
        cols = [desc[0] for desc in self._cur.description]
        return {c: _native(v) for c, v in zip(cols, row)}

    def fetchall(self):
        if not self._cur.description:
            return []
        rows = self._cur.fetchall()
        if not rows:
            return []
        cols = [desc[0] for desc in self._cur.description]
        return [{c: _native(v) for c, v in zip(cols, row)} for row in rows]


class Connection:
    """Unified connection wrapper for PostgreSQL and SQLite.

    Exposes: execute(), commit(), close() with dict-row results.
    """

    def __init__(self, pg_conn=None, sqlite_conn=None):
        self._pg = pg_conn
        self._sqlite = sqlite_conn

    def execute(self, sql, params=None):
        if self._pg:
            cur = self._pg.cursor()
            cur.execute(sql, params or ())
            return _DictCursor(cur)
        else:
            if params is None:
                return self._sqlite.execute(sql)
            return self._sqlite.execute(sql, params)

    def commit(self):
        if self._pg:
            self._pg.commit()
        else:
            self._sqlite.commit()

    def close(self):
        if self._pg:
            self._pg.close()
        else:
            self._sqlite.close()


def get_connection() -> Connection:
    """Open (and initialize if needed) the database."""
    if USE_PG:
        import psycopg2
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        for stmt in PG_SCHEMA:
            cur.execute(stmt)
        conn.commit()
        cur.close()
        return Connection(pg_conn=conn)
    else:
        import sqlite3
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        conn.executescript(SQLITE_SCHEMA)
        # Migrations: add columns introduced after initial schema
        for migration in [
            "ALTER TABLE hitting_lines  ADD COLUMN e          INTEGER DEFAULT 0",
            "ALTER TABLE pitching_lines ADD COLUMN hr         INTEGER DEFAULT 0",
            "ALTER TABLE hitting_lines  ADD COLUMN source_org TEXT DEFAULT NULL",
            "ALTER TABLE pitching_lines ADD COLUMN source_org TEXT DEFAULT NULL",
            "ALTER TABLE games          ADD COLUMN source_org TEXT DEFAULT NULL",
        ]:
            try:
                conn.execute(migration)
                conn.commit()
            except sqlite3.OperationalError:
                pass  # column already exists
        return Connection(sqlite_conn=conn)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  INSERTS (used by the fetch command)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def upsert_game(conn: Connection, game: dict):
    """Insert or update a game record."""
    if USE_PG:
        conn.execute(f"""
            INSERT INTO games
                (game_pk, date, level, team_id, team_name, opponent,
                 is_home, our_score, opp_score, result, status, source_org)
            VALUES ({_ph(12)})
            ON CONFLICT (game_pk) DO UPDATE SET
                date = EXCLUDED.date, level = EXCLUDED.level,
                team_id = EXCLUDED.team_id, team_name = EXCLUDED.team_name,
                opponent = EXCLUDED.opponent, is_home = EXCLUDED.is_home,
                our_score = EXCLUDED.our_score, opp_score = EXCLUDED.opp_score,
                result = EXCLUDED.result, status = EXCLUDED.status,
                source_org = EXCLUDED.source_org
        """, (
            game["game_pk"], game["date"], game["level"], game["team_id"],
            game["team_name"], game["opponent"], int(game["is_home"]),
            game["our_score"], game["opp_score"], game["result"], game["status"],
            game.get("source_org"),
        ))
    else:
        conn.execute("""
            INSERT OR REPLACE INTO games
                (game_pk, date, level, team_id, team_name, opponent,
                 is_home, our_score, opp_score, result, status, source_org)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            game["game_pk"], game["date"], game["level"], game["team_id"],
            game["team_name"], game["opponent"], int(game["is_home"]),
            game["our_score"], game["opp_score"], game["result"], game["status"],
            game.get("source_org"),
        ))


def upsert_hitting_line(conn: Connection, line: dict):
    """Insert or update a hitting line."""
    if USE_PG:
        conn.execute(f"""
            INSERT INTO hitting_lines
                (game_pk, date, level, player_name, position,
                 ab, r, h, doubles, triples, hr, rbi, bb, k, sb, e, source_org)
            VALUES ({_ph(17)})
            ON CONFLICT (game_pk, player_name) DO UPDATE SET
                date = EXCLUDED.date, level = EXCLUDED.level,
                position = EXCLUDED.position, ab = EXCLUDED.ab,
                r = EXCLUDED.r, h = EXCLUDED.h, doubles = EXCLUDED.doubles,
                triples = EXCLUDED.triples, hr = EXCLUDED.hr, rbi = EXCLUDED.rbi,
                bb = EXCLUDED.bb, k = EXCLUDED.k, sb = EXCLUDED.sb,
                e = EXCLUDED.e, source_org = EXCLUDED.source_org
        """, (
            line["game_pk"], line["date"], line["level"], line["player_name"],
            line["position"], line["ab"], line["r"], line["h"],
            line["doubles"], line["triples"], line["hr"], line["rbi"],
            line["bb"], line["k"], line["sb"], line.get("e", 0),
            line.get("source_org"),
        ))
    else:
        conn.execute("""
            INSERT OR REPLACE INTO hitting_lines
                (game_pk, date, level, player_name, position,
                 ab, r, h, doubles, triples, hr, rbi, bb, k, sb, e, source_org)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            line["game_pk"], line["date"], line["level"], line["player_name"],
            line["position"], line["ab"], line["r"], line["h"],
            line["doubles"], line["triples"], line["hr"], line["rbi"],
            line["bb"], line["k"], line["sb"], line.get("e", 0),
            line.get("source_org"),
        ))


def upsert_pitching_line(conn: Connection, line: dict):
    """Insert or update a pitching line."""
    if USE_PG:
        conn.execute(f"""
            INSERT INTO pitching_lines
                (game_pk, date, level, player_name, position,
                 ip, h, r, er, bb, k, hr, decision, source_org)
            VALUES ({_ph(14)})
            ON CONFLICT (game_pk, player_name) DO UPDATE SET
                date = EXCLUDED.date, level = EXCLUDED.level,
                position = EXCLUDED.position, ip = EXCLUDED.ip,
                h = EXCLUDED.h, r = EXCLUDED.r, er = EXCLUDED.er,
                bb = EXCLUDED.bb, k = EXCLUDED.k, hr = EXCLUDED.hr,
                decision = EXCLUDED.decision, source_org = EXCLUDED.source_org
        """, (
            line["game_pk"], line["date"], line["level"], line["player_name"],
            line["position"], line["ip"], line["h"], line["r"],
            line["er"], line["bb"], line["k"], line.get("hr", 0), line["decision"],
            line.get("source_org"),
        ))
    else:
        conn.execute("""
            INSERT OR REPLACE INTO pitching_lines
                (game_pk, date, level, player_name, position,
                 ip, h, r, er, bb, k, hr, decision, source_org)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            line["game_pk"], line["date"], line["level"], line["player_name"],
            line["position"], line["ip"], line["h"], line["r"],
            line["er"], line["bb"], line["k"], line.get("hr", 0), line["decision"],
            line.get("source_org"),
        ))


def insert_game_ignore(conn: Connection, game: dict):
    """Insert a game row only if it doesn't already exist (for external-org skeletons)."""
    if USE_PG:
        conn.execute(f"""
            INSERT INTO games
                (game_pk, date, level, team_id, team_name, opponent,
                 is_home, our_score, opp_score, result, status, source_org)
            VALUES ({_ph(12)})
            ON CONFLICT (game_pk) DO NOTHING
        """, (
            game["game_pk"], game["date"], game["level"], game["team_id"],
            game["team_name"], game["opponent"], int(game["is_home"]),
            game["our_score"], game["opp_score"], game["result"], game["status"],
            game.get("source_org"),
        ))
    else:
        conn.execute("""
            INSERT OR IGNORE INTO games
                (game_pk, date, level, team_id, team_name, opponent,
                 is_home, our_score, opp_score, result, status, source_org)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            game["game_pk"], game["date"], game["level"], game["team_id"],
            game["team_name"], game["opponent"], int(game["is_home"]),
            game["our_score"], game["opp_score"], game["result"], game["status"],
            game.get("source_org"),
        ))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  QUERIES – daily view
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _q(sql: str) -> str:
    """Replace ? placeholders with %s when using Postgres."""
    return sql.replace("?", "%s") if USE_PG else sql


def games_on_date(conn: Connection, date_str: str,
                  affiliate_only: bool = False) -> list[dict]:
    if affiliate_only:
        rows = conn.execute(
            _q("SELECT * FROM games WHERE date = ? AND source_org IS NULL ORDER BY level"),
            (date_str,),
        ).fetchall()
    else:
        rows = conn.execute(
            _q("SELECT * FROM games WHERE date = ? ORDER BY level"), (date_str,)
        ).fetchall()
    return [dict(r) for r in rows]


def hitting_for_game(conn: Connection, game_pk: int,
                     affiliate_only: bool = False) -> list[dict]:
    if affiliate_only:
        rows = conn.execute(
            _q("SELECT * FROM hitting_lines WHERE game_pk = ? AND source_org IS NULL ORDER BY ab DESC, h DESC"),
            (game_pk,),
        ).fetchall()
    else:
        rows = conn.execute(
            _q("SELECT * FROM hitting_lines WHERE game_pk = ? ORDER BY ab DESC, h DESC"),
            (game_pk,),
        ).fetchall()
    return [dict(r) for r in rows]


def pitching_for_game(conn: Connection, game_pk: int,
                      affiliate_only: bool = False) -> list[dict]:
    if affiliate_only:
        rows = conn.execute(
            _q("SELECT * FROM pitching_lines WHERE game_pk = ? AND source_org IS NULL ORDER BY ip DESC"),
            (game_pk,),
        ).fetchall()
    else:
        rows = conn.execute(
            _q("SELECT * FROM pitching_lines WHERE game_pk = ? ORDER BY ip DESC"),
            (game_pk,),
        ).fetchall()
    return [dict(r) for r in rows]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  QUERIES – season / trend aggregations
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def season_hitting_totals(conn: Connection, player_name: str,
                          year: int | None = None) -> dict | None:
    if year is None:
        year = datetime.now().year
    row = conn.execute(_q("""
        SELECT
            player_name,
            COUNT(DISTINCT game_pk) as games,
            SUM(ab) as ab, SUM(r) as r, SUM(h) as h,
            SUM(doubles) as doubles, SUM(triples) as triples,
            SUM(hr) as hr, SUM(rbi) as rbi, SUM(bb) as bb,
            SUM(k) as k, SUM(sb) as sb
        FROM hitting_lines
        WHERE player_name = ? AND date LIKE ?
        GROUP BY player_name
    """), (player_name, f"{year}-%")).fetchone()
    if not row or row["ab"] == 0:
        return None
    d = dict(row)
    d["avg"] = round(d["h"] / d["ab"], 3) if d["ab"] else 0
    d["obp"] = round((d["h"] + d["bb"]) / (d["ab"] + d["bb"]) , 3) if (d["ab"] + d["bb"]) else 0
    d["slg"] = round(
        (d["h"] - d["doubles"] - d["triples"] - d["hr"]
         + d["doubles"] * 2 + d["triples"] * 3 + d["hr"] * 4) / d["ab"], 3
    ) if d["ab"] else 0
    d["ops"] = round(d["obp"] + d["slg"], 3)
    return d


def season_pitching_totals(conn: Connection, player_name: str,
                            year: int | None = None) -> dict | None:
    if year is None:
        year = datetime.now().year
    row = conn.execute(_q("""
        SELECT
            player_name,
            COUNT(DISTINCT game_pk) as games,
            SUM(ip) as ip, SUM(h) as h, SUM(r) as r,
            SUM(er) as er, SUM(bb) as bb, SUM(k) as k, SUM(hr) as hr
        FROM pitching_lines
        WHERE player_name = ? AND date LIKE ?
        GROUP BY player_name
    """), (player_name, f"{year}-%")).fetchone()
    if not row or not row["ip"]:
        return None
    d = dict(row)
    d["era"] = round(d["er"] * 9 / d["ip"], 2) if d["ip"] else 0
    d["whip"] = round((d["bb"] + d["h"]) / d["ip"], 2) if d["ip"] else 0
    d["k_per_9"] = round(d["k"] * 9 / d["ip"], 1) if d["ip"] else 0
    d["fip"] = round((13 * (d["hr"] or 0) + 3 * d["bb"] - 2 * d["k"]) / d["ip"] + 3.10, 2) if d["ip"] else None
    return d


def rolling_hitting(conn: Connection, player_name: str,
                    days: int = 7, as_of: str | None = None) -> dict | None:
    ref = datetime.strptime(as_of, "%Y-%m-%d") if as_of else datetime.now()
    cutoff = (ref - timedelta(days=days)).strftime("%Y-%m-%d")
    row = conn.execute(_q("""
        SELECT
            COUNT(DISTINCT game_pk) as games,
            SUM(ab) as ab, SUM(h) as h, SUM(hr) as hr,
            SUM(rbi) as rbi, SUM(bb) as bb, SUM(k) as k
        FROM hitting_lines
        WHERE player_name = ? AND date >= ?
        GROUP BY player_name
    """), (player_name, cutoff)).fetchone()
    if not row or row["ab"] == 0:
        return None
    d = dict(row)
    d["avg"] = round(d["h"] / d["ab"], 3) if d["ab"] else 0
    return d


def rolling_pitching(conn: Connection, player_name: str,
                     days: int = 7, as_of: str | None = None) -> dict | None:
    ref = datetime.strptime(as_of, "%Y-%m-%d") if as_of else datetime.now()
    cutoff = (ref - timedelta(days=days)).strftime("%Y-%m-%d")
    row = conn.execute(_q("""
        SELECT
            COUNT(DISTINCT game_pk) as games,
            SUM(ip) as ip, SUM(er) as er, SUM(k) as k,
            SUM(bb) as bb, SUM(h) as h
        FROM pitching_lines
        WHERE player_name = ? AND date >= ?
        GROUP BY player_name
    """), (player_name, cutoff)).fetchone()
    if not row or not row["ip"]:
        return None
    d = dict(row)
    d["era"] = round(d["er"] * 9 / d["ip"], 2) if d["ip"] else 0
    return d


def last_n_games_hitting(conn: Connection, player_name: str,
                         n: int = 15, year: int | None = None) -> dict | None:
    year_filter = f"AND date LIKE '{year}-%'" if year else ""
    row = conn.execute(_q(f"""
        SELECT
            COUNT(*) as games,
            SUM(ab) as ab, SUM(h) as h, SUM(hr) as hr,
            SUM(doubles) as doubles, SUM(triples) as triples,
            SUM(rbi) as rbi, SUM(bb) as bb, SUM(k) as k, SUM(sb) as sb
        FROM (
            SELECT * FROM hitting_lines
            WHERE player_name = ? {year_filter}
            ORDER BY date DESC
            LIMIT ?
        ) sub
    """), (player_name, n)).fetchone()
    if not row or not row["ab"]:
        return None
    d = dict(row)
    d["avg"] = round(d["h"] / d["ab"], 3) if d["ab"] else 0
    d["obp"] = round((d["h"] + d["bb"]) / (d["ab"] + d["bb"]), 3) if (d["ab"] + d["bb"]) else 0
    singles = d["h"] - (d["doubles"] or 0) - (d["triples"] or 0) - (d["hr"] or 0)
    d["slg"] = round((singles + 2*(d["doubles"] or 0) + 3*(d["triples"] or 0) + 4*(d["hr"] or 0)) / d["ab"], 3) if d["ab"] else 0
    d["ops"] = round(d["obp"] + d["slg"], 3)
    return d


def last_n_games_pitching(conn: Connection, player_name: str,
                           n: int = 15, year: int | None = None) -> dict | None:
    year_filter = f"AND date LIKE '{year}-%'" if year else ""
    row = conn.execute(_q(f"""
        SELECT
            COUNT(*) as games,
            SUM(ip) as ip, SUM(er) as er, SUM(k) as k,
            SUM(bb) as bb, SUM(h) as h, SUM(hr) as hr
        FROM (
            SELECT * FROM pitching_lines
            WHERE player_name = ? {year_filter}
            ORDER BY date DESC
            LIMIT ?
        ) sub
    """), (player_name, n)).fetchone()
    if not row or not row["ip"]:
        return None
    d = dict(row)
    d["era"]  = round(d["er"] * 9 / d["ip"], 2) if d["ip"] else 0
    d["whip"] = round((d["bb"] + d["h"]) / d["ip"], 2) if d["ip"] else 0
    d["k9"]   = round(d["k"] * 9 / d["ip"], 1) if d["ip"] else 0
    d["fip"]  = round((13 * (d["hr"] or 0) + 3 * d["bb"] - 2 * d["k"]) / d["ip"] + 3.10, 2) if d["ip"] else None
    return d


def player_game_log(conn: Connection, player_name: str,
                    stat_type: str = "hitting", limit: int = 10) -> list[dict]:
    table = "hitting_lines" if stat_type == "hitting" else "pitching_lines"
    rows = conn.execute(_q(f"""
        SELECT * FROM {table}
        WHERE player_name = ?
        ORDER BY date DESC
        LIMIT ?
    """), (player_name, limit)).fetchall()
    return [dict(r) for r in rows]


def dates_with_data(conn: Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT date FROM games WHERE source_org IS NULL ORDER BY date DESC"
    ).fetchall()
    return [r["date"] for r in rows]


def team_record(conn: Connection, level: str,
                year: int | None = None) -> dict:
    if year is None:
        year = datetime.now().year
    row = conn.execute(_q("""
        SELECT
            SUM(CASE WHEN result = 'W' THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN result = 'L' THEN 1 ELSE 0 END) as losses,
            COUNT(*) as games
        FROM games
        WHERE level = ? AND date LIKE ? AND status = 'Final'
    """), (level, f"{year}-%")).fetchone()
    return dict(row) if row else {"wins": 0, "losses": 0, "games": 0}
