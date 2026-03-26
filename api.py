"""
MLB Stats API client.

Handles all HTTP requests, schedule fetching, box score parsing, and
affiliate ID auto-discovery.
"""

import logging
import requests
from config import API_BASE, AFFILIATES, PARENT_ORG_ID, SPORT_IDS, SPORT_ID_TO_LEVEL

log = logging.getLogger("nats_milb.api")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  LOW-LEVEL
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def api_get(endpoint: str, params: dict | None = None,
            timeout: int = 30) -> dict:
    """GET from MLB Stats API with error handling."""
    url = f"{API_BASE}{endpoint}"
    try:
        resp = requests.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        log.error("API error: %s – %s", url, exc)
        return {}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AFFILIATE DISCOVERY
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def build_team_name_cache() -> dict:
    """
    Fetch all MiLB teams and return a dict of {team_id: {"name": ..., "short_name": ...}}.
    Called once per run so opponent names resolve correctly.
    """
    log.info("Building team name cache…")
    data = api_get("/api/v1/teams", params={"sportIds": SPORT_IDS})
    cache = {}
    for t in data.get("teams", []):
        sport_id = t.get("sport", {}).get("id")
        cache[t["id"]] = {
            "name": t.get("name", "Unknown"),
            "short_name": t.get("shortName", t.get("teamName", "Unknown")),
            "sport_id": sport_id,
            "level": SPORT_ID_TO_LEVEL.get(sport_id, "Unknown"),
        }
    log.info("  Cached %d MiLB teams.", len(cache))
    return cache


def discover_affiliates() -> dict:
    """
    Query the API for all MiLB teams, filter to Nationals affiliates.
    Returns dict keyed by sport_id → {team_id, name}.
    Falls back to config.py on failure.
    """
    log.info("Discovering affiliate IDs…")
    data = api_get("/api/v1/teams", params={"sportIds": SPORT_IDS})
    if not data or "teams" not in data:
        log.warning("Discovery failed; using config fallbacks.")
        return {}
    found = {}
    for t in data["teams"]:
        if t.get("parentOrgId") == PARENT_ORG_ID:
            sid = t["sport"]["id"]
            found[sid] = {
                "team_id": t["id"],
                "name": t.get("name", ""),
                "short_name": t.get("shortName", t.get("teamName", "")),
            }
            log.info("  %s (ID %d, sport %d)", t["name"], t["id"], sid)
    return found


def resolve_affiliates() -> dict:
    """Merge discovered IDs with config fallbacks. Returns AFFILIATES dict."""
    discovered = discover_affiliates()
    affiliates = {k: dict(v) for k, v in AFFILIATES.items()}
    for level, info in affiliates.items():
        sid = info["sport_id"]
        if sid in discovered:
            info["team_id"] = discovered[sid]["team_id"]
            info["name"] = discovered[sid]["name"]
    return affiliates


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SCHEDULE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fetch_schedule(team_id: int, date_str: str,
                   sport_id: int | None = None) -> list[dict]:
    """Games for a team on a date."""
    params = {
        "teamId": team_id,
        "date": date_str,
        "hydrate": "linescore",
    }
    if sport_id is not None:
        params["sportId"] = sport_id
    data = api_get("/api/v1/schedule", params=params)
    games = []
    for d in data.get("dates", []):
        games.extend(d.get("games", []))
    return games


def parse_game_info(game: dict, our_team_id: int,
                    team_cache: dict | None = None) -> dict:
    """Extract result/score from a schedule game entry.

    team_cache: optional dict of {team_id: {"name": ..., "short_name": ...}}
    built by build_team_name_cache(). Used to resolve opponent names that
    the schedule endpoint often omits for MiLB games.
    """
    status = game.get("status", {})
    state = status.get("abstractGameState", "Preview")
    detail = status.get("detailedState", "Scheduled")

    away = game.get("teams", {}).get("away", {})
    home = game.get("teams", {}).get("home", {})
    is_home = home.get("team", {}).get("id") == our_team_id

    our_score = (home if is_home else away).get("score", 0)
    opp_score = (away if is_home else home).get("score", 0)
    opp_team = (away if is_home else home).get("team", {})
    opp_id = opp_team.get("id")

    # Prefer inline name; fall back to cache; fall back to "Unknown"
    if team_cache and opp_id and opp_id in team_cache:
        opp_name = team_cache[opp_id]["name"]
        opp_short = team_cache[opp_id]["short_name"]
    else:
        opp_name = opp_team.get("name") or "Unknown"
        opp_short = opp_team.get("teamName") or opp_name

    result = None
    if state == "Final":
        if our_score > opp_score:
            result = "W"
        elif our_score < opp_score:
            result = "L"
        else:
            result = "T"

    return {
        "game_pk": game.get("gamePk"),
        "state": state,
        "detail": detail,
        "is_home": is_home,
        "opponent": opp_name,
        "opponent_short": opp_short,
        "our_score": our_score,
        "opp_score": opp_score,
        "result": result,
        "score_display": f"{our_score}-{opp_score}" if state == "Final" else detail,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  BOX SCORE PARSING
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fetch_boxscore(game_pk: int) -> dict:
    return api_get(f"/api/v1/game/{game_pk}/boxscore")


def _parse_ip(ip_str: str) -> float:
    """
    Convert MLB innings-pitched string to float.
    '5.2' means 5⅔ innings → 5.667; '5.1' means 5⅓ → 5.333.
    """
    try:
        parts = str(ip_str).split(".")
        full = int(parts[0])
        thirds = int(parts[1]) if len(parts) > 1 else 0
        return round(full + thirds / 3, 3)
    except (ValueError, IndexError):
        return 0.0


def extract_player_stats(boxscore: dict, team_id: int) -> tuple[list, list]:
    """
    From a boxscore, pull hitter and pitcher lines for our team.
    Returns (hitters: list[dict], pitchers: list[dict]).
    """
    hitters, pitchers = [], []
    teams = boxscore.get("teams", {})

    for side in ("home", "away"):
        side_data = teams.get(side, {})
        if side_data.get("team", {}).get("id") != team_id:
            continue

        for pid, pdata in side_data.get("players", {}).items():
            name = pdata.get("person", {}).get("fullName", "Unknown")
            pos = pdata.get("position", {}).get("abbreviation", "")

            # Hitting
            bstats = pdata.get("stats", {}).get("batting", {})
            fstats = pdata.get("stats", {}).get("fielding", {})
            if bstats and bstats.get("atBats", 0) > 0:
                hitters.append({
                    "player_name": name,
                    "position": pos,
                    "ab": bstats.get("atBats", 0),
                    "r": bstats.get("runs", 0),
                    "h": bstats.get("hits", 0),
                    "doubles": bstats.get("doubles", 0),
                    "triples": bstats.get("triples", 0),
                    "hr": bstats.get("homeRuns", 0),
                    "rbi": bstats.get("rbi", 0),
                    "bb": bstats.get("baseOnBalls", 0),
                    "k": bstats.get("strikeOuts", 0),
                    "sb": bstats.get("stolenBases", 0),
                    "e": fstats.get("errors", 0),
                })

            # Pitching
            pstats = pdata.get("stats", {}).get("pitching", {})
            ip_str = pstats.get("inningsPitched", "0")
            if pstats and ip_str != "0":
                pitchers.append({
                    "player_name": name,
                    "position": pos,
                    "ip": _parse_ip(ip_str),
                    "ip_display": ip_str,
                    "h": pstats.get("hits", 0),
                    "r": pstats.get("runs", 0),
                    "er": pstats.get("earnedRuns", 0),
                    "bb": pstats.get("baseOnBalls", 0),
                    "k": pstats.get("strikeOuts", 0),
                    "hr": pstats.get("homeRuns", 0),
                    "decision": pstats.get("note", ""),
                })

    return hitters, pitchers


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PLAYER-CENTRIC LOOKUP (for traded / non-Nats prospects)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def lookup_player_id(name: str) -> list[dict]:
    """
    Search for a player by name via the MLB people/search endpoint.
    Returns list of {id, full_name, current_team} dicts.
    """
    data = api_get("/api/v1/people/search", params={"names": name})
    results = []
    for p in data.get("people", []):
        results.append({
            "id": p.get("id"),
            "full_name": p.get("fullName", ""),
            "current_team": p.get("currentTeam", {}).get("name", ""),
            "primary_position": p.get("primaryPosition", {}).get("abbreviation", ""),
        })
    return results


def fetch_player_game_log(player_id: int, season: int,
                          group: str = "hitting",
                          team_cache: dict | None = None,
                          start_date: str | None = None,
                          end_date: str | None = None) -> list[dict]:
    """
    Fetch per-game stats for a player across all MiLB levels for a season.

    group: 'hitting' or 'pitching'
    team_cache: optional cache from build_team_name_cache() for level resolution.
    start_date / end_date: optional YYYY-MM-DD strings to narrow the result
        window (passed to the API as startDate/endDate).  Useful for daily
        fetches so we only pull one day's worth of data instead of the full
        season log.

    Returns list of game-split dicts:
      date, game_pk, team_id, team_name, team_short, level,
      opponent_name, opponent_short, is_home, stat (raw API dict)
    """
    # Query each MiLB sport level separately — the endpoint rejects
    # comma-separated sportId values and returns only MLB data with no sportId.
    all_raw_splits = []
    for sport_id in [11, 12, 13, 14]:  # AAA, AA, High-A, Single-A
        params = {
            "stats": "gameLog",
            "season": season,
            "group": group,
            "sportId": sport_id,
        }
        if start_date:
            params["startDate"] = start_date
        if end_date:
            params["endDate"] = end_date
        data = api_get(
            f"/api/v1/people/{player_id}/stats",
            params=params,
        )
        for stat_obj in data.get("stats", []):
            all_raw_splits.extend(stat_obj.get("splits", []))

    splits = []
    seen_game_pks = set()
    for split in all_raw_splits:
            game = split.get("game", {})
            game_date = split.get("date", "")  # "YYYY-MM-DD" lives at split level
            if not game_date:
                continue
            team = split.get("team", {})
            opponent = split.get("opponent", {})
            team_id = team.get("id")

            # Resolve level from team cache if available, else fall back
            if team_cache and team_id in team_cache:
                level = team_cache[team_id]["level"]
                team_short = team_cache[team_id]["short_name"]
                team_name = team_cache[team_id]["name"]
            else:
                level = "Unknown"
                team_short = team.get("teamName") or team.get("name", "Unknown")
                team_name = team.get("name", "Unknown")

            opp_id = opponent.get("id")
            if team_cache and opp_id and opp_id in team_cache:
                opp_short = team_cache[opp_id]["short_name"]
                opp_name = team_cache[opp_id]["name"]
            else:
                opp_short = opponent.get("teamName") or opponent.get("name", "Unknown")
                opp_name = opponent.get("name", "Unknown")

            splits.append({
                "date": game_date,
                "game_pk": game.get("gamePk"),
                "team_id": team_id,
                "team_name": team_name,
                "team_short": team_short,
                "level": level,
                "opponent_name": opp_name,
                "opponent_short": opp_short,
                "is_home": split.get("isHome", False),
                "stat": split.get("stat", {}),
            })
    return splits


def parse_hitting_split(split: dict) -> dict:
    """Convert a game-log hitting split into the same shape as extract_player_stats."""
    s = split["stat"]
    return {
        "player_name": None,  # caller sets this
        "position": "",
        "ab": s.get("atBats", 0),
        "r": s.get("runs", 0),
        "h": s.get("hits", 0),
        "doubles": s.get("doubles", 0),
        "triples": s.get("triples", 0),
        "hr": s.get("homeRuns", 0),
        "rbi": s.get("rbi", 0),
        "bb": s.get("baseOnBalls", 0),
        "k": s.get("strikeOuts", 0),
        "sb": s.get("stolenBases", 0),
        "e": s.get("errors", 0),
    }


def parse_pitching_split(split: dict) -> dict:
    """Convert a game-log pitching split into the same shape as extract_player_stats."""
    s = split["stat"]
    ip_str = s.get("inningsPitched", "0") or "0"
    return {
        "player_name": None,  # caller sets this
        "position": "P",
        "ip": _parse_ip(ip_str),
        "ip_display": ip_str,
        "h": s.get("hits", 0),
        "r": s.get("runs", 0),
        "er": s.get("earnedRuns", 0),
        "bb": s.get("baseOnBalls", 0),
        "k": s.get("strikeOuts", 0),
        "hr": s.get("homeRuns", 0),
        "decision": s.get("note", ""),
    }
