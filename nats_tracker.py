#!/usr/bin/env python3
"""
Washington Nationals Minor League Performance Tracker
=====================================================

A CLI tool that fetches daily MiLB box scores, stores them in a local
SQLite database, and generates an HTML dashboard with season trends.

Subcommands:
    fetch             Fetch game data and store in DB
    dashboard         Generate HTML dashboard from stored data
    season            Show season-to-date prospect summary
    update-prospects  Guided prospect-list update helper
    backfill          Fetch a range of dates at once (Nats affiliates)
    lookup-player     Find a player's MLB Stats API ID by name
    backfill-player   Fetch a prospect's full game log from any org
    info              Show DB stats and affiliate info

Usage:
    python3 nats_tracker.py fetch                    # yesterday
    python3 nats_tracker.py fetch 2026-04-15         # specific date
    python3 nats_tracker.py fetch --today             # today
    python3 nats_tracker.py dashboard                 # latest date in DB
    python3 nats_tracker.py dashboard 2026-04-15      # specific date
    python3 nats_tracker.py season                    # full season overview
    python3 nats_tracker.py backfill 2026-04-01 2026-04-15
    python3 nats_tracker.py update-prospects
    python3 nats_tracker.py info
"""

import argparse
import csv
import io
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests

# Add script directory to path so imports work from anywhere
sys.path.insert(0, str(Path(__file__).parent))

import api
import db as database
from config import (
    AFFILIATES, DATA_DIR, DATABASE_URL, DB_PATH, LEVEL_ORDER,
    load_prospects, prospect_lookup,
)
from dashboard import generate_dashboard

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("nats_milb")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  FETCH: pull data from API → store in SQLite
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def cmd_fetch(args):
    """Fetch game data for a date and store in DB."""
    date_str = _resolve_date(args)
    log.info("=" * 50)
    log.info("  FETCH: %s", date_str)
    log.info("=" * 50)

    affiliates = api.resolve_affiliates()
    team_cache = api.build_team_name_cache()
    conn = database.get_connection()

    total_games = 0
    total_hitters = 0
    total_pitchers = 0

    for level in LEVEL_ORDER:
        info = affiliates.get(level)
        if not info:
            continue
        team_id = info["team_id"]
        team_name = info["name"]
        sport_id = info.get("sport_id")
        log.info("Fetching %s – %s (ID %d)…", level, team_name, team_id)

        games = api.fetch_schedule(team_id, date_str, sport_id=sport_id)
        if not games:
            log.info("  No games scheduled.")
            continue

        for game in games:
            gi = api.parse_game_info(game, team_id, team_cache=team_cache)
            game_pk = gi["game_pk"]
            if not game_pk:
                continue

            # Store game record
            database.upsert_game(conn, {
                "game_pk": game_pk,
                "date": date_str,
                "level": level,
                "team_id": team_id,
                "team_name": team_name,
                "opponent": gi["opponent"],
                "is_home": gi["is_home"],
                "our_score": gi["our_score"],
                "opp_score": gi["opp_score"],
                "result": gi["result"],
                "status": gi["state"],
            })
            total_games += 1

            if gi["state"] != "Final":
                log.info("  Game %d: %s (not final)", game_pk, gi["detail"])
                continue

            log.info("  Game %d: %s %s vs %s",
                     game_pk, gi["result"], gi["score_display"],
                     gi["opponent_short"])

            # Fetch box score
            boxscore = api.fetch_boxscore(game_pk)
            if not boxscore:
                continue

            hitters, pitchers = api.extract_player_stats(boxscore, team_id)

            for h in hitters:
                h["game_pk"] = game_pk
                h["date"] = date_str
                h["level"] = level
                database.upsert_hitting_line(conn, h)
                total_hitters += 1

            for p in pitchers:
                p["game_pk"] = game_pk
                p["date"] = date_str
                p["level"] = level
                database.upsert_pitching_line(conn, p)
                total_pitchers += 1

    # ── Prospects on non-Nats teams ──────────────────────────────────
    # For every top-30 prospect that has an mlb_player_id set, fetch
    # their game log filtered to this date.  Skip any game played for
    # a Nats affiliate (already captured above); store the rest with
    # source_org so the dashboard can label them correctly.
    log.info("-" * 50)
    log.info("  Checking top-30 prospects on non-Nats teams…")

    prospects_list = load_prospects()
    nats_ids = _nats_team_ids()
    season = int(date_str[:4])
    ext_hitters = 0
    ext_pitchers = 0

    for prospect in prospects_list:
        player_id = prospect.get("mlb_player_id")
        if not player_id:
            continue

        name = prospect["name"]
        position = prospect.get("position", "")
        is_pitcher = any(x in position for x in ("RHP", "LHP", "SP", "RP"))
        groups = [] if is_pitcher else ["hitting"]
        groups.append("pitching")  # harmless for position players; skipped if no IP

        for group in groups:
            splits = api.fetch_player_game_log(
                player_id, season, group=group,
                team_cache=team_cache,
                start_date=date_str, end_date=date_str,
            )
            for split in splits:
                if split["date"] != date_str:
                    continue  # extra safety guard in case API ignores date params
                team_id = split["team_id"]
                if team_id in nats_ids:
                    continue  # already captured by the affiliate loop above

                game_pk = split["game_pk"]
                if not game_pk:
                    continue

                source_org = split["team_short"]

                # Store game row (INSERT ignore — we never want to overwrite
                # a proper Nats-affiliate game record with this skeleton row).
                # source_org is set so the dashboard can filter these out of
                # the daily affiliate view.
                database.insert_game_ignore(conn, {
                    "game_pk": game_pk,
                    "date": split["date"],
                    "level": split["level"],
                    "team_id": team_id,
                    "team_name": split["team_name"],
                    "opponent": split["opponent_name"],
                    "is_home": split["is_home"],
                    "our_score": None,
                    "opp_score": None,
                    "result": None,
                    "status": "Final",
                    "source_org": source_org,
                })

                base = {
                    "game_pk": game_pk,
                    "date": split["date"],
                    "level": split["level"],
                    "player_name": name,
                    "source_org": source_org,
                }

                if group == "hitting":
                    line = {**base, **api.parse_hitting_split(split)}
                    line["player_name"] = name
                    if line["ab"] > 0:
                        database.upsert_hitting_line(conn, line)
                        ext_hitters += 1
                        log.info("  %s  %s  (via %s)", name, date_str, source_org)
                else:
                    line = {**base, **api.parse_pitching_split(split)}
                    line["player_name"] = name
                    if line["ip"] > 0:
                        database.upsert_pitching_line(conn, line)
                        ext_pitchers += 1
                        log.info("  %s  %s  pitching (via %s)", name, date_str, source_org)

    log.info("External prospects: %d hitting lines, %d pitching lines",
             ext_hitters, ext_pitchers)

    conn.commit()
    conn.close()
    log.info("Stored: %d games, %d hitting lines, %d pitching lines",
             total_games, total_hitters + ext_hitters,
             total_pitchers + ext_pitchers)

    # Auto-generate static HTML dashboard after fetch (local dev only)
    if not args.no_dashboard and not DATABASE_URL:
        log.info("Generating dashboard…")
        conn = database.get_connection()
        out_path = str(DATA_DIR / "nats_dashboard.html")
        generate_dashboard(conn, date_str, out_path)
        conn.close()
        log.info("Open: %s", out_path)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DASHBOARD: generate HTML from DB
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def cmd_dashboard(args):
    """Generate HTML dashboard for a date (default: latest in DB)."""
    conn = database.get_connection()

    if args.date:
        date_str = args.date
    else:
        dates = database.dates_with_data(conn)
        if not dates:
            log.error("No data in DB. Run 'fetch' first.")
            conn.close()
            return
        date_str = dates[0]  # most recent

    out_path = str(DATA_DIR / "nats_dashboard.html")
    generate_dashboard(conn, date_str, out_path)
    conn.close()
    log.info("Dashboard for %s → %s", date_str, out_path)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SEASON: CSV export of season stats
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def cmd_season(args):
    """Export season-to-date prospect stats as CSV."""
    conn = database.get_connection()
    prospects = prospect_lookup()
    sorted_p = sorted(prospects.values(), key=lambda p: p.get("composite_rank", 99))

    out = io.StringIO()
    w = csv.writer(out)

    # Hitting
    w.writerow(["=== PROSPECT HITTING – SEASON TO DATE ==="])
    w.writerow(["Rank (MLB/FG/BA)", "Name", "Pos", "Level", "G", "AB",
                "AVG", "OBP", "SLG", "OPS", "H", "2B", "3B", "HR",
                "RBI", "BB", "K", "SB"])
    for p in sorted_p:
        s = database.season_hitting_totals(conn, p["name"])
        if not s:
            continue
        w.writerow([
            p["display_rank"], p["name"], p["position"], p.get("level", ""),
            s["games"], s["ab"], f'{s["avg"]:.3f}', f'{s["obp"]:.3f}',
            f'{s["slg"]:.3f}', f'{s["ops"]:.3f}',
            s["h"], s["doubles"], s["triples"], s["hr"],
            s["rbi"], s["bb"], s["k"], s["sb"],
        ])
    w.writerow([])

    # Pitching
    w.writerow(["=== PROSPECT PITCHING – SEASON TO DATE ==="])
    w.writerow(["Rank (MLB/FG/BA)", "Name", "Pos", "Level", "G", "IP",
                "ERA", "WHIP", "K/9", "K", "BB", "H", "ER"])
    for p in sorted_p:
        s = database.season_pitching_totals(conn, p["name"])
        if not s:
            continue
        w.writerow([
            p["display_rank"], p["name"], p["position"], p.get("level", ""),
            s["games"], f'{s["ip"]:.1f}', f'{s["era"]:.2f}',
            f'{s["whip"]:.2f}', f'{s["k_per_9"]:.1f}',
            s["k"], s["bb"], s["h"], s["er"],
        ])

    conn.close()
    csv_path = str(DATA_DIR / "nats_season_stats.csv")
    with open(csv_path, "w") as f:
        f.write(out.getvalue())
    log.info("Season stats → %s", csv_path)
    print(out.getvalue())


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  BACKFILL: fetch a range of dates
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def cmd_backfill(args):
    """Fetch data for a range of dates."""
    start = datetime.strptime(args.start_date, "%Y-%m-%d")
    end = datetime.strptime(args.end_date, "%Y-%m-%d")
    if end < start:
        log.error("End date must be >= start date.")
        return

    current = start
    total_days = (end - start).days + 1
    day_num = 0
    while current <= end:
        day_num += 1
        date_str = current.strftime("%Y-%m-%d")
        log.info("Backfill [%d/%d]: %s", day_num, total_days, date_str)

        # Reuse fetch logic with no_dashboard=True
        class FakeArgs:
            date = date_str
            today = False
            no_dashboard = True
        cmd_fetch(FakeArgs())
        current += timedelta(days=1)

    # Generate dashboard for the end date
    log.info("Generating dashboard for %s…", args.end_date)
    conn = database.get_connection()
    out_path = str(DATA_DIR / "nats_dashboard.html")
    generate_dashboard(conn, args.end_date, out_path)
    conn.close()
    log.info("Done! Dashboard → %s", out_path)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  UPDATE-PROSPECTS: guided update helper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def cmd_update_prospects(args):
    """Interactive helper to update prospect rankings."""
    json_path = Path(__file__).parent / "prospects.json"

    print("\n" + "=" * 60)
    print("  PROSPECT LIST UPDATE HELPER")
    print("=" * 60)
    print(f"\nEditing: {json_path}")
    print("\nOptions:")
    print("  1. Export current list as CSV (for spreadsheet editing)")
    print("  2. Import updated rankings from CSV")
    print("  3. Add a new prospect")
    print("  4. Remove a prospect")
    print("  5. Generate blank CSV template")
    print()

    choice = input("Choice [1-5]: ").strip()

    prospects_data = json.loads(json_path.read_text())
    prospects = prospects_data["prospects"]

    if choice == "1":
        csv_path = Path(__file__).parent / "prospects_export.csv"
        with open(csv_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["name", "position", "level", "mlb_rank", "fg_rank", "ba_rank"])
            for p in prospects:
                w.writerow([p["name"], p["position"], p["level"],
                            p.get("mlb_rank", ""), p.get("fg_rank", ""),
                            p.get("ba_rank", "")])
        print(f"\nExported to: {csv_path}")
        print("Edit this CSV, then re-import with option 2.")

    elif choice == "2":
        csv_path = input("Path to CSV file: ").strip()
        if not os.path.exists(csv_path):
            print(f"File not found: {csv_path}")
            return
        new_prospects = []
        with open(csv_path, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                p = {
                    "name": row["name"].strip(),
                    "position": row["position"].strip(),
                    "level": row["level"].strip(),
                }
                for src in ("mlb_rank", "fg_rank", "ba_rank"):
                    val = row.get(src, "").strip()
                    p[src] = int(val) if val and val.isdigit() else None
                new_prospects.append(p)
        prospects_data["prospects"] = new_prospects
        prospects_data["_updated"] = datetime.now().strftime("%Y-%m-%d")
        json_path.write_text(json.dumps(prospects_data, indent=2))
        print(f"\nUpdated {len(new_prospects)} prospects in {json_path}")

    elif choice == "3":
        name = input("Player name: ").strip()
        pos = input("Position: ").strip()
        level = input("Level (AAA/AA/High-A/Single-A): ").strip()
        mlb = input("MLB Pipeline rank (or blank): ").strip()
        fg = input("FanGraphs rank (or blank): ").strip()
        ba = input("Baseball America rank (or blank): ").strip()
        prospects.append({
            "name": name, "position": pos, "level": level,
            "mlb_rank": int(mlb) if mlb.isdigit() else None,
            "fg_rank": int(fg) if fg.isdigit() else None,
            "ba_rank": int(ba) if ba.isdigit() else None,
        })
        prospects_data["_updated"] = datetime.now().strftime("%Y-%m-%d")
        json_path.write_text(json.dumps(prospects_data, indent=2))
        print(f"\nAdded {name}. Total: {len(prospects)} prospects.")

    elif choice == "4":
        print("\nCurrent prospects:")
        for i, p in enumerate(prospects):
            print(f"  {i+1}. {p['name']} ({p['position']}) – {p['level']}")
        idx = input("\nNumber to remove: ").strip()
        if idx.isdigit() and 1 <= int(idx) <= len(prospects):
            removed = prospects.pop(int(idx) - 1)
            prospects_data["_updated"] = datetime.now().strftime("%Y-%m-%d")
            json_path.write_text(json.dumps(prospects_data, indent=2))
            print(f"\nRemoved {removed['name']}.")
        else:
            print("Invalid selection.")

    elif choice == "5":
        csv_path = Path(__file__).parent / "prospects_template.csv"
        with open(csv_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["name", "position", "level", "mlb_rank", "fg_rank", "ba_rank"])
            for i in range(1, 31):
                w.writerow(["", "", "", "", "", ""])
        print(f"\nBlank template → {csv_path}")
        print("Fill it in and import with option 2.")

    else:
        print("Invalid choice.")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  LOOKUP-PLAYER: resolve MLB player ID by name
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def cmd_lookup_player(args):
    """Search MLB Stats API for a player ID by name."""
    results = api.lookup_player_id(args.name)
    if not results:
        print(f"\nNo players found for: {args.name!r}")
        print("Try a partial last name or check the spelling.")
        return

    print(f"\nResults for {args.name!r}:")
    print(f"  {'ID':>8}  {'Full Name':<30}  {'Pos':>4}  Current Team")
    print("  " + "-" * 70)
    for p in results:
        print(f"  {p['id']:>8}  {p['full_name']:<30}  {p['primary_position']:>4}  {p['current_team']}")

    print()
    print("To save an ID, edit prospects.json and set \"mlb_player_id\": <ID>")
    print("for the matching prospect entry. Then run:")
    print("  python3 nats_tracker.py backfill-player <name> --season <year>")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  BACKFILL-PLAYER: fetch a prospect's full game log from any org
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Set of Nats affiliate team IDs (loaded once at runtime)
def _nats_team_ids() -> set:
    from config import AFFILIATES
    return {info["team_id"] for info in AFFILIATES.values()}


def _backfill_one_player(prospect: dict, season: int, conn,
                         team_cache: dict, nats_ids: set,
                         dry_run: bool = False) -> dict:
    """
    Fetch and store game-log stats for a single prospect.
    Returns counts: {hitting, pitching, skipped}.
    """
    player_id = prospect.get("mlb_player_id")
    name = prospect["name"]
    counts = {"hitting": 0, "pitching": 0, "skipped": 0}

    if not player_id:
        log.warning("  %s – no mlb_player_id set, skipping.", name)
        counts["skipped"] = 1
        return counts

    log.info("  Fetching %s (ID %d) – %s…", name, player_id, season)

    # Determine whether to fetch hitting, pitching, or both based on position
    position = prospect.get("position", "")
    is_pitcher = any(x in position for x in ("RHP", "LHP", "SP", "RP"))
    groups = []
    if not is_pitcher:
        groups.append("hitting")
    groups.append("pitching")  # many position players also pitch in emergencies; harmless

    for group in groups:
        splits = api.fetch_player_game_log(player_id, season, group=group,
                                           team_cache=team_cache)
        if not splits:
            continue

        for split in splits:
            game_pk = split["game_pk"]
            if not game_pk:
                continue

            team_id = split["team_id"]
            # source_org: None if Nats affiliate, short team name otherwise
            source_org = None if team_id in nats_ids else split["team_short"]

            # Insert game row only if not already present (so Nats data is never overwritten).
            # source_org is set on external-org rows so the dashboard can filter
            # them out of the daily affiliate view.
            database.insert_game_ignore(conn, {
                "game_pk": game_pk,
                "date": split["date"],
                "level": split["level"],
                "team_id": team_id,
                "team_name": split["team_name"],
                "opponent": split["opponent_name"],
                "is_home": split["is_home"],
                "our_score": None,
                "opp_score": None,
                "result": None,
                "status": "Final",
                "source_org": source_org,
            })

            base = {
                "game_pk": game_pk,
                "date": split["date"],
                "level": split["level"],
                "player_name": name,
                "source_org": source_org,
            }

            if group == "hitting":
                line = {**base, **api.parse_hitting_split(split)}
                line["player_name"] = name
                if not dry_run and line["ab"] > 0:
                    database.upsert_hitting_line(conn, line)
                    counts["hitting"] += 1
            else:
                line = {**base, **api.parse_pitching_split(split)}
                line["player_name"] = name
                if not dry_run and line["ip"] > 0:
                    database.upsert_pitching_line(conn, line)
                    counts["pitching"] += 1

    org_note = f" ({counts['hitting']} hitting, {counts['pitching']} pitching lines)"
    log.info("    → stored%s", org_note)
    return counts


def cmd_backfill_player(args):
    """
    Fetch full-season game logs for one or all top-30 prospects.
    Uses mlb_player_id from prospects.json — run lookup-player first
    if IDs aren't populated yet.
    """
    season = args.season or datetime.now().year - 1
    prospects_data = json.loads(
        (Path(__file__).parent / "prospects.json").read_text()
    )
    all_prospects = prospects_data["prospects"]

    # Filter to the requested prospect(s)
    if args.name and args.name.lower() != "all":
        target = [p for p in all_prospects
                  if args.name.lower() in p["name"].lower()]
        if not target:
            log.error("No prospect found matching %r. Check the name in prospects.json.", args.name)
            return
    else:
        target = all_prospects

    missing_ids = [p["name"] for p in target if not p.get("mlb_player_id")]
    if missing_ids:
        log.warning("The following prospects have no mlb_player_id and will be skipped:")
        for n in missing_ids:
            log.warning("  • %s  →  run: python3 nats_tracker.py lookup-player %r", n, n.split()[1])
        if all(not p.get("mlb_player_id") for p in target):
            log.error("No IDs set — nothing to backfill. Use lookup-player first.")
            return

    log.info("=" * 55)
    log.info("  PLAYER BACKFILL: %d prospect(s) – season %s", len(target), season)
    log.info("=" * 55)

    team_cache = api.build_team_name_cache()
    nats_ids = _nats_team_ids()
    conn = database.get_connection()

    total = {"hitting": 0, "pitching": 0, "skipped": 0}
    for prospect in target:
        counts = _backfill_one_player(prospect, season, conn, team_cache,
                                      nats_ids, dry_run=args.dry_run)
        for k in total:
            total[k] += counts.get(k, 0)

    conn.commit()
    conn.close()

    log.info("-" * 55)
    log.info("Done.  Hitting lines: %d  Pitching lines: %d  Skipped: %d",
             total["hitting"], total["pitching"], total["skipped"])
    if args.dry_run:
        log.info("(dry-run: no data was written)")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  INFO: show DB stats
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def cmd_info(args):
    """Show database stats and affiliate info."""
    conn = database.get_connection()

    dates = database.dates_with_data(conn)
    game_count = conn.execute("SELECT COUNT(*) as c FROM games").fetchone()["c"]
    hit_count = conn.execute("SELECT COUNT(*) as c FROM hitting_lines").fetchone()["c"]
    pitch_count = conn.execute("SELECT COUNT(*) as c FROM pitching_lines").fetchone()["c"]

    print("\n" + "=" * 50)
    print("  NATS MiLB TRACKER – DATABASE INFO")
    print("=" * 50)
    print(f"\n  DB path:         {DB_PATH}")
    print(f"  Dates with data: {len(dates)}")
    if dates:
        print(f"  Date range:      {dates[-1]} → {dates[0]}")
    print(f"  Games stored:    {game_count}")
    print(f"  Hitting lines:   {hit_count}")
    print(f"  Pitching lines:  {pitch_count}")

    print("\n  Affiliates:")
    for level in LEVEL_ORDER:
        info = AFFILIATES[level]
        record = database.team_record(conn, level)
        rec_str = f" ({record['wins']}-{record['losses']})" if record["games"] else ""
        print(f"    {level:10s}  {info['name']:30s}  ID {info['team_id']}{rec_str}")

    prospects = load_prospects()
    print(f"\n  Prospects tracked: {len(prospects)}")
    conn.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HELPERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _resolve_date(args) -> str:
    """Determine the target date from CLI args."""
    if hasattr(args, "date") and args.date:
        return args.date
    if hasattr(args, "today") and args.today:
        return datetime.now().strftime("%Y-%m-%d")
    return (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CLI
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def main():
    parser = argparse.ArgumentParser(
        description="Washington Nationals Minor League Tracker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s fetch                     Fetch yesterday's data
  %(prog)s fetch 2026-04-15          Fetch a specific date
  %(prog)s fetch --today             Fetch today's data
  %(prog)s dashboard                 Dashboard for latest date in DB
  %(prog)s dashboard 2026-04-15      Dashboard for specific date
  %(prog)s season                    Season stats CSV export
  %(prog)s backfill 2026-04-01 2026-04-15   Fetch a date range
  %(prog)s update-prospects          Update prospect list
  %(prog)s lookup-player "Harry Ford"        Find MLB player ID
  %(prog)s backfill-player "Harry Ford" --season 2025
  %(prog)s backfill-player all --season 2025  All prospects with IDs set
  %(prog)s info                      Show DB stats
        """,
    )
    sub = parser.add_subparsers(dest="command")

    # fetch
    p_fetch = sub.add_parser("fetch", help="Fetch game data for a date")
    p_fetch.add_argument("date", nargs="?", default=None, help="YYYY-MM-DD")
    p_fetch.add_argument("--today", action="store_true")
    p_fetch.add_argument("--no-dashboard", action="store_true",
                         help="Skip auto-generating dashboard")
    p_fetch.set_defaults(func=cmd_fetch)

    # dashboard
    p_dash = sub.add_parser("dashboard", help="Generate HTML dashboard")
    p_dash.add_argument("date", nargs="?", default=None, help="YYYY-MM-DD")
    p_dash.set_defaults(func=cmd_dashboard)

    # season
    p_season = sub.add_parser("season", help="Export season prospect stats")
    p_season.set_defaults(func=cmd_season)

    # backfill
    p_back = sub.add_parser("backfill", help="Fetch a range of dates")
    p_back.add_argument("start_date", help="Start date YYYY-MM-DD")
    p_back.add_argument("end_date", help="End date YYYY-MM-DD")
    p_back.set_defaults(func=cmd_backfill)

    # update-prospects
    p_update = sub.add_parser("update-prospects", help="Update prospect list")
    p_update.set_defaults(func=cmd_update_prospects)

    # lookup-player
    p_lookup = sub.add_parser("lookup-player",
                               help="Find a player's MLB ID by name")
    p_lookup.add_argument("name", help="Player name (or partial last name)")
    p_lookup.set_defaults(func=cmd_lookup_player)

    # backfill-player
    p_bp = sub.add_parser("backfill-player",
                           help="Fetch full-season game log for a prospect (any org)")
    p_bp.add_argument("name",
                      help="Prospect name (must match prospects.json) or 'all'")
    p_bp.add_argument("--season", type=int, default=None,
                      help="Season year (default: last year)")
    p_bp.add_argument("--dry-run", action="store_true",
                      help="Fetch and log without writing to DB")
    p_bp.set_defaults(func=cmd_backfill_player)

    # info
    p_info = sub.add_parser("info", help="Show DB stats")
    p_info.set_defaults(func=cmd_info)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    args.func(args)


if __name__ == "__main__":
    main()
