#!/usr/bin/env python3
"""Fetch NYC rent-stabilized unit counts by tract.

Sources:
  - JustFix.nyc's annual rent-stab BBL list (derived from NYC DOF tax-bill scrapes):
    https://s3.amazonaws.com/justfix-data/rentstab_counts_from_doffer_2024.csv
  - NYC DCP PLUTO (data.cityofnewyork.us 64uk-42ks) — BBL → lat/lon.

For each rent-stabilized BBL, we look up its (lon, lat) from PLUTO and find the containing
2020 census tract. Per-tract output:

  docs/rentstab_by_tract.json
    { GEOID: { rs_buildings, rs_units_2024, rs_units_2018,
               rs_unit_change_2018_2024, rs_unit_change_pct_2018_2024 } }
"""
import csv
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

from shapely.geometry import shape, Point
from shapely.strtree import STRtree

ROOT = Path(__file__).parent
DOCS = ROOT / "docs"

JUSTFIX_URL = "https://s3.amazonaws.com/justfix-data/rentstab_counts_from_doffer_2024.csv"
PLUTO_URL   = "https://data.cityofnewyork.us/resource/64uk-42ks.json"


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "nyc-population-map/1.0"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return r.read()


def normalize_bbl(s):
    """Strip Socrata float formatting and pad to 10 chars."""
    if not s:
        return None
    s = str(s).split(".")[0]
    return s.zfill(10)


# ---- 1. JustFix rent-stab BBLs ----
print("Fetching JustFix rent-stab BBL list...")
text = fetch(JUSTFIX_URL).decode("utf-8")
reader = csv.DictReader(text.splitlines())
rs_by_bbl = {}
for row in reader:
    bbl = normalize_bbl(row.get("ucbbl"))
    if not bbl:
        continue
    try:
        u24 = int(row.get("uc2024") or 0)
    except ValueError:
        u24 = 0
    try:
        u18 = int(row.get("uc2018") or 0)
    except ValueError:
        u18 = 0
    rs_by_bbl[bbl] = {"u24": u24, "u18": u18}
print(f"  {len(rs_by_bbl):,} rent-stabilized BBLs (any year 2018-2024)")

# Filter: only BBLs that had any stabilized units in any year (default already is in the file)
need_bbls = set(rs_by_bbl.keys())
print(f"  {len(need_bbls):,} BBLs to look up in PLUTO")


# ---- 2. PLUTO lookup (paginate) ----
print("Fetching PLUTO BBL→lat/lon (paginating, this takes ~30s)...")
bbl_coords = {}
page = 50000
offset = 0
while True:
    qs = urllib.parse.urlencode({
        "$select": "bbl,latitude,longitude",
        "$limit": page,
        "$offset": offset,
        "$order": "bbl",
    })
    url = f"{PLUTO_URL}?{qs}"
    rows = json.loads(fetch(url))
    if not rows:
        break
    for row in rows:
        bbl = normalize_bbl(row.get("bbl"))
        lat = row.get("latitude"); lon = row.get("longitude")
        if not bbl or not lat or not lon:
            continue
        if bbl not in need_bbls:
            continue
        try:
            bbl_coords[bbl] = (float(lon), float(lat))
        except ValueError:
            pass
    print(f"    offset {offset:,} → {len(bbl_coords):,}/{len(need_bbls):,} matched")
    if len(rows) < page:
        break
    offset += page
    time.sleep(0.1)

print(f"  matched {len(bbl_coords):,} of {len(need_bbls):,} BBLs to PLUTO coords")


# ---- 3. Point-in-polygon to tracts ----
print("Loading tract geometry...")
tracts = json.load(open(ROOT / "nyc2020_tracts.geojson"))
geoms = [shape(f["geometry"]) for f in tracts["features"]]
gids = [f["properties"]["geoid"] for f in tracts["features"]]
tree = STRtree(geoms)

by_tract = {g: {"rs_buildings": 0, "rs_units_2024": 0, "rs_units_2018": 0} for g in gids}
unmatched = 0
for bbl, (lon, lat) in bbl_coords.items():
    if not (40.4 <= lat <= 41.0 and -74.3 <= lon <= -73.6):
        continue
    pt = Point(lon, lat)
    hit = None
    for idx in tree.query(pt):
        if geoms[idx].contains(pt):
            hit = gids[idx]
            break
    if not hit:
        unmatched += 1
        continue
    rec = by_tract[hit]
    counts = rs_by_bbl[bbl]
    # Count this building only if it has any stabilized units in 2024
    if counts["u24"] > 0:
        rec["rs_buildings"] += 1
    rec["rs_units_2024"] += counts["u24"]
    rec["rs_units_2018"] += counts["u18"]

# Derive change since 2018
for g, rec in by_tract.items():
    u18 = rec["rs_units_2018"]
    u24 = rec["rs_units_2024"]
    rec["rs_unit_change_2018_2024"] = u24 - u18
    if u18 > 0:
        rec["rs_unit_change_pct_2018_2024"] = 100.0 * (u24 - u18) / u18
    else:
        rec["rs_unit_change_pct_2018_2024"] = None

total_b = sum(r["rs_buildings"] for r in by_tract.values())
total_u = sum(r["rs_units_2024"] for r in by_tract.values())
print(f"  {total_b:,} rent-stab buildings; {total_u:,} units in 2024 (citywide)")
print(f"  {unmatched:,} BBLs failed point-in-polygon")

out = {
    "meta": {
        "source_units":   "JustFix.nyc rentstab_counts_from_doffer_2024 (DOF tax-bill scrapes)",
        "source_coords":  "NYC DCP PLUTO (data.cityofnewyork.us 64uk-42ks)",
        "year_label_2024": "Registered units as of mid-2025 (most recent file dated June 2025)",
        "year_label_2018": "Registered units as of mid-2019",
        "method":         "BBL → PLUTO lat/lon → point-in-polygon in 2020 census tracts",
        "tracts_with_units": sum(1 for r in by_tract.values() if r['rs_units_2024'] > 0),
        "citywide_buildings": total_b,
        "citywide_units":     total_u,
    },
    "by_tract": by_tract,
}
json.dump(out, open(DOCS / "rentstab_by_tract.json", "w"))
print(f"Wrote {DOCS/'rentstab_by_tract.json'}")
