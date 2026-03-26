#!/usr/bin/env python3
"""
Bulk-lookup MLB player IDs for all prospects in prospects.json.

Strategy (in order):
  1. Pull full-season rosters from all Nats affiliates + parent org.
     This covers every player who spent time in the Nats system.
  2. For anyone still missing, try the people/search endpoint with
     last name only (works better than full name for MiLB players).
  3. Flag anything still unresolved for manual review.

Writes prospects_with_ids.json — review it, then cp over prospects.json.
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import api
from config import AFFILIATES, PARENT_ORG_ID

PROSPECTS_FILE = Path(__file__).parent / "prospects.json"
OUTPUT_FILE    = Path(__file__).parent / "prospects_with_ids.json"

# Nats parent org MLB team ID + all affiliate team IDs
NATS_TEAM_IDS = [PARENT_ORG_ID] + [v["team_id"] for v in AFFILIATES.values()]


# ── Strategy 1: roster scrape ────────────────────────────────────────

def fetch_org_name_map(season: int) -> dict[str, int]:
    """
    Pull full-season rosters from the Nats parent org and all affiliates.
    Returns {lowercase_fullname: player_id}.
    """
    name_map: dict[str, int] = {}

    for team_id in NATS_TEAM_IDS:
        for roster_type in ("fullSeason", "40Man", "nonRosterInvitees"):
            data = api.api_get(
                f"/api/v1/teams/{team_id}/roster",
                params={
                    "rosterType": roster_type,
                    "season": season,
                    "hydrate": "person",
                },
            )
            for entry in data.get("roster", []):
                person = entry.get("person", {})
                pid  = person.get("id")
                name = person.get("fullName", "")
                if pid and name:
                    name_map[name.lower()] = pid

        time.sleep(0.1)

    return name_map


# ── Strategy 2: last-name search fallback ───────────────────────────

def search_by_last_name(full_name: str) -> list[dict]:
    """Search using only last name — much better hit rate for MiLB players."""
    last = full_name.split()[-1]
    # Strip trailing punctuation like "Jr." → "Jr" won't trip up the API
    last = last.rstrip(".")
    results = api.lookup_player_id(last)
    return results


def pick_best(full_name: str, results: list[dict]) -> tuple[int | None, str]:
    """Try to find the best match from a list of search results."""
    if not results:
        return None, "no_match"

    # Exact full-name match
    exact = [r for r in results if r["full_name"].lower() == full_name.lower()]
    if len(exact) == 1:
        return exact[0]["id"], "ok"
    if len(exact) > 1:
        return exact[0]["id"], "ambiguous"

    # Single result total → likely right
    if len(results) == 1:
        return results[0]["id"], "ok"

    return None, "ambiguous"


# ── Main ─────────────────────────────────────────────────────────────

def main():
    data = json.loads(PROSPECTS_FILE.read_text())
    prospects = data["prospects"]

    # Prompt for season — default to most recent completed season
    from datetime import datetime
    default_season = datetime.now().year - 1
    season_input = input(f"Season to pull rosters for [{default_season}]: ").strip()
    season = int(season_input) if season_input.isdigit() else default_season

    print(f"\nStep 1: Fetching org rosters for {season}…")
    name_map = fetch_org_name_map(season)
    print(f"  Found {len(name_map)} players across Nats org rosters.")

    ok_count       = 0
    already_set    = 0
    no_match_names = []
    ambiguous      = []

    print(f"\nMatching {len(prospects)} prospects…\n")
    print(f"  {'#':>3}  {'Name':<28}  {'Status':<16}  ID")
    print("  " + "-" * 65)

    for i, p in enumerate(prospects, 1):
        name = p["name"]

        if p.get("mlb_player_id"):
            already_set += 1
            print(f"  {i:>3}  {name:<28}  {'already set':<16}  {p['mlb_player_id']}")
            continue

        # Strategy 1: roster match
        pid = name_map.get(name.lower())
        if pid:
            p["mlb_player_id"] = pid
            ok_count += 1
            print(f"  {i:>3}  {name:<28}  {'✓ roster':<16}  {pid}")
            continue

        # Strategy 2: last-name search
        results = search_by_last_name(name)
        pid, status = pick_best(name, results)
        time.sleep(0.2)

        if status == "ok" and pid:
            p["mlb_player_id"] = pid
            ok_count += 1
            print(f"  {i:>3}  {name:<28}  {'✓ search':<16}  {pid}")
        elif status == "no_match":
            no_match_names.append(name)
            print(f"  {i:>3}  {name:<28}  {'✗ no match':<16}  —")
        else:
            ambiguous.append((name, results))
            print(f"  {i:>3}  {name:<28}  {'? ambiguous':<16}  {len(results)} candidates")

    # Write output
    data["prospects"] = prospects
    OUTPUT_FILE.write_text(json.dumps(data, indent=2))

    print(f"\n{'='*65}")
    print(f"  Matched:      {ok_count}")
    print(f"  Already set:  {already_set}")
    print(f"  No match:     {len(no_match_names)}")
    print(f"  Ambiguous:    {len(ambiguous)}")
    print(f"  Output:       {OUTPUT_FILE.name}")
    print(f"{'='*65}")

    if no_match_names:
        print("\n⚠  No match found — these may be in a different org or use a")
        print("   different name format in the API. Try searching by last name:")
        for n in no_match_names:
            last = n.split()[-1].rstrip(".")
            print(f"    python3 nats_tracker.py lookup-player \"{last}\"")

    if ambiguous:
        print("\n⚠  Multiple candidates — set the correct mlb_player_id manually")
        print("   in prospects_with_ids.json:\n")
        for name, results in ambiguous:
            print(f"  {name}:")
            for r in results[:6]:
                print(f"    {r['id']:>8}  {r['full_name']:<30}  "
                      f"{r['primary_position']:>4}  {r['current_team']}")
            print()

    total_resolved = ok_count + already_set
    if total_resolved > 0:
        print(f"✓ {total_resolved}/{len(prospects)} resolved.")
        print(f"  Review {OUTPUT_FILE.name}, then:")
        print(f"  cp prospects_with_ids.json prospects.json")


if __name__ == "__main__":
    main()
