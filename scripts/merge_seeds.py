#!/usr/bin/env python3
"""Merge confirmed game seeds into market_seeds_auto.json."""
import json
from pathlib import Path

FIXTURE = Path(__file__).resolve().parent.parent / "arbiter" / "mapping" / "fixtures" / "market_seeds_auto.json"

# Load existing
existing = json.loads(FIXTURE.read_text()) if FIXTURE.exists() else []
existing_ids = {e["canonical_id"] for e in existing}

# Load new confirmed
with open("/tmp/confirmed_game_seeds.json") as f:
    new_seeds = json.load(f)

added = 0
for seed in new_seeds:
    if seed["canonical_id"] not in existing_ids:
        existing.append(seed)
        existing_ids.add(seed["canonical_id"])
        added += 1
        print("  Added: %s (%s)" % (seed["canonical_id"], seed["description"]))
    else:
        print("  Skip (exists): %s" % seed["canonical_id"])

FIXTURE.write_text(json.dumps(existing, indent=2))
print("\nAdded %d new seeds. Total seeds in fixture: %d" % (added, len(existing)))
print("Confirmed seeds: %d" % len([s for s in existing if s.get("status") == "confirmed"]))
print("Candidate seeds: %d" % len([s for s in existing if s.get("status") != "confirmed"]))
