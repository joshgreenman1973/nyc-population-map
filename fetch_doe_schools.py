#!/usr/bin/env python3
"""Fetch NYC DOE K-12 public school enrollment by school location, aggregate to tracts.

Sources:
  - Demographic Snapshot c7ru-d68s (DBN + total + per-grade enrollment; latest year 2021-22)
  - 2019-2020 School Locations wg9x-4ke6 (DBN + lat/lon; latest available DOE location set)

Output: docs/doe_k12_by_tract.json
  { GEOID: { schools: n, k12_enrolled: n, year: "2021-22" } }

Method: latest year per school from c7ru-d68s, join to wg9x-4ke6 by DBN to get lat/lon,
point-in-polygon to NYC DCP 2020 tract geometry. K-12 enrollment = sum of grade_k through
grade_12 cells (excludes 3-K and Pre-K, which are not strictly K-12).
"""
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

from shapely.geometry import shape, Point
from shapely.strtree import STRtree

ROOT = Path(__file__).parent
DOCS = ROOT / "docs"
DOCS.mkdir(exist_ok=True)

ENROLL_URL = "https://data.cityofnewyork.us/resource/c7ru-d68s.json"
LOC_URL    = "https://data.cityofnewyork.us/resource/wg9x-4ke6.json"


def fetch_all(url, where=None, limit=50000):
    out = []
    offset = 0
    while True:
        q = {"$limit": limit, "$offset": offset, "$order": "dbn" if "c7ru" in url else "system_code"}
        if where: q["$where"] = where
        full = f"{url}?{urllib.parse.urlencode(q)}"
        req = urllib.request.Request(full, headers={"User-Agent": "nyc-population-map/1.0"})
        with urllib.request.urlopen(req, timeout=120) as r:
            rows = json.loads(r.read())
        if not rows:
            break
        out.extend(rows)
        if len(rows) < limit:
            break
        offset += limit
        time.sleep(0.1)
    return out


def num(v):
    if v in (None, "", "NULL"): return 0
    try: return int(float(v))
    except (ValueError, TypeError): return 0


print("Fetching DOE Demographic Snapshot enrollment (c7ru-d68s)...")
all_enroll = fetch_all(ENROLL_URL)
print(f"  {len(all_enroll):,} school-year rows")

# Use only the most-recent year present for each DBN
latest_by_dbn = {}
for r in all_enroll:
    dbn = r.get("dbn")
    year = r.get("year", "")
    if not dbn: continue
    if dbn not in latest_by_dbn or year > latest_by_dbn[dbn]["year"]:
        latest_by_dbn[dbn] = r

print(f"  {len(latest_by_dbn):,} unique schools (most-recent year per school)")
years_present = sorted({r['year'] for r in latest_by_dbn.values()})
print(f"  years used: {years_present}")

print("Fetching DOE school locations (wg9x-4ke6)...")
locs = fetch_all(LOC_URL)
loc_by_dbn = {}
for r in locs:
    dbn = r.get("system_code")
    lat = r.get("latitude"); lon = r.get("longitude")
    if dbn and lat and lon:
        try:
            loc_by_dbn[dbn] = (float(lon), float(lat))
        except ValueError:
            pass
print(f"  {len(loc_by_dbn):,} schools with lat/lon")

# Join + aggregate K-12 enrollment per school
print("Loading tract geometry...")
tracts = json.load(open(ROOT / "nyc2020_tracts.geojson"))
geoms = [shape(f["geometry"]) for f in tracts["features"]]
geoids = [f["properties"]["geoid"] for f in tracts["features"]]
tree = STRtree(geoms)

by_tract = {g: {"schools": 0, "k12_enrolled": 0} for g in geoids}
GRADES = ["grade_k", "grade_1", "grade_2", "grade_3", "grade_4", "grade_5",
          "grade_6", "grade_7", "grade_8", "grade_9", "grade_10", "grade_11", "grade_12"]

unmatched_loc = 0
unmatched_geom = 0
total_enrolled = 0
schools_used = 0
for dbn, rec in latest_by_dbn.items():
    coords = loc_by_dbn.get(dbn)
    if not coords:
        unmatched_loc += 1
        continue
    k12 = sum(num(rec.get(g)) for g in GRADES)
    if k12 == 0:
        continue  # not a K-12 school
    lon, lat = coords
    if not (40.4 <= lat <= 41.0 and -74.3 <= lon <= -73.6):
        continue
    pt = Point(lon, lat)
    hit = None
    for idx in tree.query(pt):
        if geoms[idx].contains(pt):
            hit = geoids[idx]
            break
    if not hit:
        unmatched_geom += 1
        continue
    by_tract[hit]["schools"] += 1
    by_tract[hit]["k12_enrolled"] += k12
    total_enrolled += k12
    schools_used += 1

print(f"Schools matched: {schools_used} (no-location: {unmatched_loc}, off-grid: {unmatched_geom})")
print(f"Total K-12 enrolled across NYC: {total_enrolled:,}")
year_label = max(years_present) if years_present else "unknown"

out = {
    "meta": {
        "year": year_label,
        "schools_used": schools_used,
        "schools_missing_location": unmatched_loc,
        "total_k12_enrolled": total_enrolled,
        "source_enrollment": "NYC Open Data c7ru-d68s (NYC DOE Demographic Snapshot)",
        "source_locations": "NYC Open Data wg9x-4ke6 (DOE School Locations)",
        "note": "By school location (where the school is), not by student residence. Only NYC public schools; charters operating under DOE included. Private/parochial NOT included (NYSED non-public data is published only as MS Access files; integration TODO).",
    },
    "by_tract": by_tract,
}
json.dump(out, open(DOCS / "doe_k12_by_tract.json", "w"))
print(f"Wrote {DOCS/'doe_k12_by_tract.json'}")
