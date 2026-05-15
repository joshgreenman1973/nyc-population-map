#!/usr/bin/env python3
"""Fetch 2020 Decennial Census tract-level population for NYC (P1_001N).

Requires a Census API key — free at https://api.census.gov/data/key_signup.html.
Provide it via:
  export CENSUS_API_KEY=your_key_here

Output: docs/decennial_2020_by_tract.json  → { GEOID: pop_2020 }

After this runs, re-run build.py to merge the 2020 population into tracts.geojson as
the `pop_2020` property. The UI's denominator-toggle will then pick it up.
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

DOCS = Path(__file__).parent / "docs"
DOCS.mkdir(exist_ok=True)

KEY = os.environ.get("CENSUS_API_KEY")
if not KEY:
    sys.stderr.write("Set CENSUS_API_KEY env var first. Register at https://api.census.gov/data/key_signup.html\n")
    sys.exit(1)

NYC_COUNTIES = ["005", "047", "061", "081", "085"]
out = {}
for county in NYC_COUNTIES:
    url = (
        "https://api.census.gov/data/2020/dec/pl"
        f"?get=P1_001N&for=tract:*&in=state:36+county:{county}&key={KEY}"
    )
    with urllib.request.urlopen(url, timeout=60) as r:
        rows = json.loads(r.read())
    for row in rows[1:]:  # skip header
        pop, state, cnty, tract = row
        geoid = state + cnty + tract
        try:
            out[geoid] = int(pop) if pop is not None else None
        except ValueError:
            out[geoid] = None
    print(f"  {county}: {len(rows)-1} tracts")

json.dump(out, open(DOCS / "decennial_2020_by_tract.json", "w"))
print(f"Wrote {DOCS/'decennial_2020_by_tract.json'} ({len(out)} tracts)")
