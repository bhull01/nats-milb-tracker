#!/usr/bin/env python3
"""
Flask web app for the Washington Nationals MiLB Tracker.

Pages:
    /                       → redirects to latest day in DB
    /day/<YYYY-MM-DD>       → daily box scores + key performers
    /season[?year=YYYY]     → season-to-date prospect stats
    /player/<name>          → player profile + game log

Run with:
    python3 app.py --port 5001
Then open http://localhost:5001
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta

from flask import Flask, render_template, redirect, url_for, abort, request
from flask_caching import Cache

sys.path.insert(0, str(Path(__file__).parent))

import db as database
from db import _q
from config import LEVEL_ORDER, prospect_lookup

app = Flask(__name__)
cache = Cache(app, config={
    "CACHE_TYPE": "SimpleCache",
    "CACHE_DEFAULT_TIMEOUT": 300,  # 5 minutes
})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  TEMPLATE FILTERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.template_filter("fmt_avg")
def fmt_avg(val):
    if not val:
        return ".000"
    return f"{float(val):.3f}"


@app.template_filter("fmt_era")
def fmt_era(val):
    return f"{float(val):.2f}" if val else "—"


@app.template_filter("fmt_ip")
def fmt_ip(val):
    return f"{float(val):.1f}" if val else "0.0"


@app.template_filter("date_display")
def date_display(date_str):
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%A, %B %-d, %Y")


@app.template_filter("trend_arrow_hit")
def trend_arrow_hit(args):
    """Pass (recent_avg, season_avg) as a tuple."""
    recent, season = args
    if recent is None or season is None:
        return ""
    diff = recent - season
    if diff > 0.030:
        return '<span class="trend-up" title="Hot">▲</span>'
    elif diff < -0.030:
        return '<span class="trend-down" title="Cold">▼</span>'
    return '<span class="trend-flat" title="Steady">▶</span>'


@app.template_filter("trend_arrow_era")
def trend_arrow_era(args):
    """Pass (recent_era, season_era) as a tuple."""
    recent, season = args
    if recent is None or season is None:
        return ""
    diff = recent - season
    if diff < -0.50:
        return '<span class="trend-up" title="Improving">▲</span>'
    elif diff > 0.50:
        return '<span class="trend-down" title="Struggling">▼</span>'
    return '<span class="trend-flat" title="Steady">▶</span>'


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ROUTES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.route("/")
def index():
    conn = database.get_connection()
    dates = database.dates_with_data(conn)
    conn.close()
    if not dates:
        return render_template("no_data.html")
    return redirect(url_for("day_view", date=dates[0]))


@app.route("/day/<date>")
@cache.cached(timeout=300)
def day_view(date):
    conn = database.get_connection()
    all_dates = database.dates_with_data(conn)

    if not all_dates:
        conn.close()
        return render_template("no_data.html")
    if date not in all_dates:
        conn.close()
        abort(404)

    idx = all_dates.index(date)
    prev_date = all_dates[idx + 1] if idx < len(all_dates) - 1 else None
    next_date = all_dates[idx - 1] if idx > 0 else None

    prospects = prospect_lookup()
    prospect_names = set(prospects.keys())

    games = database.games_on_date(conn, date, affiliate_only=True)
    games_by_level = {}
    for g in games:
        games_by_level.setdefault(g["level"], []).append(g)

    levels = []
    for level in LEVEL_ORDER:
        record = database.team_record(conn, level)
        level_games = games_by_level.get(level, [])
        game_data = []
        for g in level_games:
            hitters = database.hitting_for_game(conn, g["game_pk"], affiliate_only=True)
            pitchers = database.pitching_for_game(conn, g["game_pk"], affiliate_only=True)
            for h in hitters:
                h["is_prospect"] = h["player_name"].lower() in prospect_names
                h["prospect_info"] = prospects.get(h["player_name"].lower(), {})
                h["avg"] = round(h["h"] / h["ab"], 3) if h["ab"] else 0
            for p in pitchers:
                p["is_prospect"] = p["player_name"].lower() in prospect_names
                p["prospect_info"] = prospects.get(p["player_name"].lower(), {})
            game_data.append({"game": dict(g), "hitters": hitters, "pitchers": pitchers})
        levels.append({"level": level, "record": record, "games": game_data})

    performers = _build_performers(conn, date, prospects, prospect_names)
    conn.close()

    return render_template("day.html",
        date=date,
        all_dates=all_dates,
        prev_date=prev_date,
        next_date=next_date,
        levels=levels,
        performers=performers,
    )


@app.route("/season")
@cache.cached(timeout=300, query_string=True)
def season_view():
    year = request.args.get("year", type=int)
    conn = database.get_connection()

    # Auto-detect year from DB if not specified
    if not year:
        row = conn.execute("SELECT MAX(date) as d FROM games").fetchone()
        year = int(row["d"][:4]) if row and row["d"] else datetime.now().year

    prospects = prospect_lookup()
    sorted_prospects = sorted(prospects.values(), key=lambda p: p.get("composite_rank", 99))
    all_names = [p["name"] for p in sorted_prospects]

    # Bulk queries: 6 queries total instead of ~90+
    season_hit_map = database.bulk_season_hitting(conn, all_names, year)
    season_pitch_map = database.bulk_season_pitching(conn, all_names, year)
    last15_hit_map = database.bulk_last_n_hitting(conn, all_names, 15, year)
    last30_hit_map = database.bulk_last_n_hitting(conn, all_names, 30, year)
    last5_pitch_map = database.bulk_last_n_pitching(conn, all_names, 5, year)
    last15_pitch_map = database.bulk_last_n_pitching(conn, all_names, 15, year)

    hitters, pitchers = [], []
    for p in sorted_prospects:
        name = p["name"]
        season = season_hit_map.get(name)
        if season:
            hitters.append({
                "prospect": p, "season": season,
                "last15": last15_hit_map.get(name),
                "last30": last30_hit_map.get(name),
            })
        pseason = season_pitch_map.get(name)
        if pseason:
            pitchers.append({
                "prospect": p, "season": pseason,
                "last5": last5_pitch_map.get(name),
                "last15": last15_pitch_map.get(name),
            })

    # Available years
    years = conn.execute(
        "SELECT DISTINCT substring(date FROM 1 FOR 4) as y FROM games ORDER BY y DESC"
        if database.USE_PG else
        "SELECT DISTINCT substr(date,1,4) as y FROM games ORDER BY y DESC"
    ).fetchall()
    available_years = [int(r["y"]) for r in years]

    conn.close()
    return render_template("season.html",
        hitters=hitters, pitchers=pitchers,
        year=year, available_years=available_years,
    )


@app.route("/players")
@cache.cached(timeout=300, query_string=True)
def players_view():
    year = request.args.get("year", type=int)
    conn = database.get_connection()

    # Auto-detect year from DB if not specified
    if not year:
        row = conn.execute("SELECT MAX(date) as d FROM games").fetchone()
        year = int(row["d"][:4]) if row and row["d"] else datetime.now().year

    prospects = prospect_lookup()
    prospect_map = {p["name"].lower(): p for p in prospects.values()}

    # All hitters in DB for this year
    rows = conn.execute(_q("""
        SELECT player_name, COUNT(DISTINCT game_pk) as games,
               SUM(ab) as ab, SUM(h) as h, SUM(hr) as hr,
               SUM(rbi) as rbi, SUM(bb) as bb, SUM(k) as k, SUM(sb) as sb,
               MAX(level) as level
        FROM hitting_lines
        WHERE date LIKE ?
        GROUP BY player_name
        HAVING SUM(ab) > 0
        ORDER BY SUM(ab) DESC
    """), (f"{year}-%",)).fetchall()

    # All pitchers in DB for this year
    pitch_rows = conn.execute(_q("""
        SELECT player_name, COUNT(DISTINCT game_pk) as games,
               SUM(ip) as ip, SUM(er) as er, SUM(k) as k,
               SUM(bb) as bb, SUM(h) as h,
               MAX(level) as level
        FROM pitching_lines
        WHERE date LIKE ?
        GROUP BY player_name
        HAVING SUM(ip) > 0
        ORDER BY SUM(ip) DESC
    """), (f"{year}-%",)).fetchall()

    def enrich(r):
        d = dict(r)
        d["is_prospect"] = d["player_name"].lower() in prospect_map
        d["prospect_info"] = prospect_map.get(d["player_name"].lower(), {})
        return d

    hitters = [enrich(r) for r in rows]
    pitchers = [enrich(r) for r in pitch_rows]

    # Dedupe: remove pitchers who also appear as hitters (two-way is fine, but avoid exact dups)
    hitter_names = {h["player_name"] for h in hitters}
    pitchers_only = [p for p in pitchers if p["player_name"] not in hitter_names]

    # Available years
    years = conn.execute(
        "SELECT DISTINCT substring(date FROM 1 FOR 4) as y FROM games ORDER BY y DESC"
        if database.USE_PG else
        "SELECT DISTINCT substr(date,1,4) as y FROM games ORDER BY y DESC"
    ).fetchall()
    available_years = [int(r["y"]) for r in years]

    conn.close()

    return render_template("players.html",
        hitters=hitters, pitchers=pitchers_only,
        year=year, available_years=available_years,
    )


@app.route("/player/<path:name>")
@cache.cached(timeout=300)
def player_view(name):
    conn = database.get_connection()
    prospects = prospect_lookup()
    prospect_info = prospects.get(name.lower())

    # Detect year from DB
    row = conn.execute("SELECT MAX(date) as d FROM games").fetchone()
    year = int(row["d"][:4]) if row and row["d"] else datetime.now().year
    as_of = row["d"] if row and row["d"] else f"{year}-09-30"

    season_hit = database.season_hitting_totals(conn, name, year=year)
    season_pitch = database.season_pitching_totals(conn, name, year=year)
    hit_log = database.player_game_log(conn, name, "hitting", limit=20)
    pitch_log = database.player_game_log(conn, name, "pitching", limit=20)
    last7_hit = database.rolling_hitting(conn, name, 7, as_of=as_of)
    last15_hit = database.rolling_hitting(conn, name, 15, as_of=as_of)
    last7_pitch = database.rolling_pitching(conn, name, 7, as_of=as_of)
    last15_pitch = database.rolling_pitching(conn, name, 15, as_of=as_of)
    conn.close()

    if not season_hit and not season_pitch and not prospect_info:
        abort(404)

    return render_template("player.html",
        name=name,
        prospect_info=prospect_info,
        season_hit=season_hit,
        season_pitch=season_pitch,
        hit_log=hit_log,
        pitch_log=pitch_log,
        last7_hit=last7_hit,
        last15_hit=last15_hit,
        last7_pitch=last7_pitch,
        last15_pitch=last15_pitch,
        year=year,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HELPERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _build_performers(conn, date, prospects, prospect_names):
    games = database.games_on_date(conn, date, affiliate_only=True)
    performers = []
    for g in games:
        for h in database.hitting_for_game(conn, g["game_pk"], affiliate_only=True):
            if h["player_name"].lower() not in prospect_names:
                continue
            p = prospects[h["player_name"].lower()]
            score = (h["h"] + h["doubles"] * 0.5 + h["triples"] + h["hr"] * 2
                     + h["rbi"] * 0.5 + h["bb"] * 0.3 + h["sb"] * 0.5 + h["r"] * 0.3)
            if score > 0 or h["ab"] >= 3:
                extras = []
                for stat, label in [("hr","HR"),("rbi","RBI"),("doubles","2B"),
                                    ("triples","3B"),("bb","BB"),("sb","SB"),("r","R")]:
                    if h[stat]:
                        extras.append(label if h[stat] == 1 else f'{h[stat]} {label}')
                last_name = h["player_name"].split()[-1]
                performers.append({
                    "name": last_name,
                    "full_name": h["player_name"],
                    "prospect": p,
                    "level": g["level"],
                    "line": f'{h["h"]}/{h["ab"]}' + (f', {", ".join(extras)}' if extras else ""),
                    "type": "hitting",
                    "score": score,
                })
        for pt in database.pitching_for_game(conn, g["game_pk"], affiliate_only=True):
            if pt["player_name"].lower() not in prospect_names or pt["ip"] < 1.0:
                continue
            p = prospects[pt["player_name"].lower()]
            score = pt["ip"] + pt["k"] * 0.5 - pt["er"]
            line = f'{pt["ip"]:.1f} IP, {pt["k"]} K, {pt["er"]} ER'
            if pt["decision"]: line += f' ({pt["decision"]})'
            last_name = pt["player_name"].split()[-1]
            performers.append({
                "name": last_name,
                "full_name": pt["player_name"],
                "prospect": p,
                "level": g["level"],
                "line": line,
                "type": "pitching",
                "score": score,
            })
    performers.sort(key=lambda x: (-x["score"], x["prospect"].get("composite_rank", 99)))
    return performers[:10]


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5001)
