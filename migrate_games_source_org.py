#!/usr/bin/env python3
"""
One-time migration: backfill games.source_org for existing external-org rows.

Run once after updating to the version that adds source_org to the games table:
    python3 migrate_games_source_org.py

Safe to run multiple times — it only touches rows that still have source_org IS NULL
and have no affiliate box-score stat lines attached.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import db
from db import _q

conn = db.get_connection()

# Any game row where source_org IS NULL but has NO stat lines with source_org IS NULL
# is an external-org skeleton row that needs to be tagged.
rows = conn.execute("""
    SELECT game_pk FROM games WHERE source_org IS NULL
    AND game_pk NOT IN (
        SELECT DISTINCT game_pk FROM hitting_lines  WHERE source_org IS NULL
        UNION
        SELECT DISTINCT game_pk FROM pitching_lines WHERE source_org IS NULL
    )
""").fetchall()

game_pks = [r["game_pk"] for r in rows]
print(f"Found {len(game_pks)} external-org game rows with missing source_org")

if game_pks:
    updated = 0
    for gpk in game_pks:
        row = conn.execute(_q("""
            SELECT source_org FROM hitting_lines
            WHERE game_pk = ? AND source_org IS NOT NULL
            UNION
            SELECT source_org FROM pitching_lines
            WHERE game_pk = ? AND source_org IS NOT NULL
            LIMIT 1
        """), (gpk, gpk)).fetchone()
        if row and row["source_org"]:
            conn.execute(_q("UPDATE games SET source_org = ? WHERE game_pk = ?"),
                         (row["source_org"], gpk))
            updated += 1
    conn.commit()
    print(f"Updated {updated} game rows.")
else:
    print("Nothing to migrate — already up to date.")

conn.close()
