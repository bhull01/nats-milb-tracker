"""
Configuration for Washington Nationals Minor League Performance Tracker.

Affiliate structure and constants. Prospect data lives in prospects.json
(edit that file directly or use `nats_tracker.py update-prospects`).
"""

import json
import os
from pathlib import Path

# ── Nationals org ────────────────────────────────────────────────────
PARENT_ORG_ID = 120

# ── Affiliates: level → metadata ────────────────────────────────────
# sport_id:  11 = AAA, 12 = AA, 13 = High-A, 14 = Single-A
# team_ids are auto-discovered at runtime; these are fallbacks.
# Verify with:
#   python3 -c "import requests,json; r=requests.get(
#     'https://statsapi.mlb.com/api/v1/teams?sportIds=11,12,13,14');
#     [print(t['id'],t['name']) for t in r.json()['teams']
#      if t.get('parentOrgId')==120]"
AFFILIATES = {
    "AAA": {
        "name": "Rochester Red Wings",
        "team_id": 534,
        "sport_id": 11,
        "short_name": "Rochester",
    },
    "AA": {
        "name": "Harrisburg Senators",
        "team_id": 546,
        "sport_id": 12,
        "short_name": "Harrisburg",
    },
    "High-A": {
        "name": "Wilmington Blue Rocks",
        "team_id": 1865,
        "sport_id": 13,
        "short_name": "Wilmington",
    },
    "Single-A": {
        "name": "Fredericksburg Nationals",
        "team_id": 547,
        "sport_id": 14,
        "short_name": "Fredericksburg",
    },
}

LEVEL_ORDER = ["AAA", "AA", "High-A", "Single-A"]

# Maps MLB Stats API sport IDs to human-readable level labels
SPORT_ID_TO_LEVEL = {
    11: "AAA",
    12: "AA",
    13: "High-A",
    14: "Single-A",
    16: "Rookie",
    17: "Rookie",   # DSL/FCL both report as rookie-level
}

# ── MLB Stats API ────────────────────────────────────────────────────
API_BASE = "https://statsapi.mlb.com"
SPORT_IDS = "11,12,13,14"

# ── Database ─────────────────────────────────────────────────────────
# PostgreSQL: set DATABASE_URL env var (e.g. on Railway).
# Falls back to local SQLite when DATABASE_URL is not set.
DATABASE_URL = os.environ.get("DATABASE_URL")

# DB and dashboard output go into a 'data' subdirectory.
# Override with NATS_DATA_DIR env var if you want them elsewhere.
DATA_DIR = Path(os.environ.get("NATS_DATA_DIR", str(Path(__file__).parent / "data")))
DB_PATH = DATA_DIR / "nats_milb.db"  # only used when DATABASE_URL is not set

# ── Prospect loading ─────────────────────────────────────────────────

def load_prospects(path: str | None = None) -> list[dict]:
    """Load prospects from JSON file."""
    if path is None:
        path = Path(__file__).parent / "prospects.json"
    with open(path, "r") as f:
        data = json.load(f)
    return data["prospects"]


def prospect_lookup(prospects: list[dict] | None = None) -> dict:
    """
    Build a lowercase-name → prospect dict for fast matching.
    Includes a 'display_rank' string like "1/2/1" for MLB/FG/BA.
    """
    if prospects is None:
        prospects = load_prospects()
    lookup = {}
    for p in prospects:
        key = p["name"].lower()
        ranks = []
        for src in ("mlb_rank", "fg_rank", "ba_rank"):
            r = p.get(src)
            ranks.append(str(r) if r else "—")
        p["display_rank"] = "/".join(ranks)
        # "composite" rank = average of available ranks (for sorting)
        avail = [p.get(s) for s in ("mlb_rank", "fg_rank", "ba_rank") if p.get(s)]
        p["composite_rank"] = sum(avail) / len(avail) if avail else 99
        lookup[key] = p
    return lookup
