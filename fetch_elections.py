#!/usr/bin/env python3
"""Fetch NYC general-election results and aggregate by census tract.

Elections covered (all citywide ED-level CSVs from NYC BoE + Josh's nyc-election-archive):
  - 2020 General — President / Vice President (Biden vs Trump)
  - 2024 General — President / Vice President (Harris vs Trump)
  - 2021 General — Mayor (Adams vs Sliwa)
  - 2025 General — Mayor (Mamdani vs Cuomo vs Sliwa)

Method:
  1. For each election, build a map { ED key → { candidate: votes } }.
     Presidential CSVs come from vote.nyc directly; mayoral results come from
     the already-prepared geojsons in ~/Experiments/nyc-election-archive/docs.
  2. ED geometry: use Josh's published ED polygons. We use the 2021-vintage geometry
     for 2020/2021 elections and the 2025-vintage geometry for 2024/2025 elections,
     since NYC redrew ED lines after the 2020 redistricting.
  3. For each ED polygon, compute centroid and find the containing 2020 census tract.
  4. Sum candidate votes from all EDs falling in each tract.

Output: docs/elections_by_tract.json
  { "tracts": { GEOID: { pres_2020_d, pres_2020_r, ... } }, "meta": { ... } }

Two-party Democratic share is computed as (D / (D + R)) so it's directly comparable
across years even when third-party candidates differ.
"""
import csv
import io
import json
import os
import time
import urllib.request
from pathlib import Path

from shapely.geometry import shape, Point

ROOT = Path(__file__).parent
DOCS = ROOT / "docs"
DOCS.mkdir(exist_ok=True)

ARCHIVE = Path("/Users/joshgreenman/Experiments/nyc-election-archive/docs")

# ---------- Presidential CSVs from NYC BoE ----------
PRES_URLS = {
    "2020": "https://vote.nyc/sites/default/files/pdf/election_results/2020/20201103General%20Election/00000100000Citywide%20President%20Vice%20President%20Citywide%20EDLevel.csv",
    "2024": "https://vote.nyc/sites/default/files/pdf/election_results/2024/20241105General%20Election/00000100000Citywide%20President%20Vice%20President%20Citywide%20EDLevel.csv",
}

# Substrings that identify each candidate. Each year's candidate appears on multiple
# ballot lines (Democratic + Working Families, Republican + Conservative, etc.). We
# sum all lines for the same candidate.
PRES_CANDIDATES = {
    "2020": {
        "d": ["Joseph R. Biden"],
        "r": ["Donald J. Trump"],
    },
    "2024": {
        "d": ["Kamala D. Harris"],
        "r": ["Donald J. Trump"],
    },
}

# ---------- Mayoral ED geojsons (already prepped) ----------
# Map each election to the geojson file and candidate -> vote field name
MAYOR_FILES = {
    "2021": {
        "geojson": ARCHIVE / "results_2021_general_mayor.geojson",
        "candidates": {
            "adams":   "v_adams",
            "sliwa":   "v_sliwa",
        },
    },
    "2025": {
        "geojson": ARCHIVE / "results_2025_general_mayor.geojson",
        "candidates": {
            "mamdani": "v_mamdani",
            "cuomo":   "v_cuomo",
            "sliwa":   "v_sliwa",
        },
    },
}

# Which ED-geometry vintage to use for each election
ED_GEOM_VINTAGE = {
    "2020": "2021",  # pre-redistricting
    "2021": "2021",
    "2024": "2025",  # post-redistricting
    "2025": "2025",
}


def fetch_url(url):
    req = urllib.request.Request(url, headers={"User-Agent": "nyc-population-map/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


# ---------- Presidential parsing ----------
def parse_pres_csv(text, candidates_map):
    """Return dict[ed_key] -> { 'd': votes, 'r': votes, 'total_major': votes }."""
    out = {}
    reader = csv.reader(io.StringIO(text))
    for row in reader:
        # Each row has 22 columns: 11 header labels + 11 data values.
        # Data columns are at indices 11..21.
        ad = row[11].lstrip("0") or "0"
        ed = row[12].lstrip("0") or "0"
        unit = row[20]
        tally_str = (row[21] or "0").replace(",", "")
        try:
            tally = int(tally_str)
        except ValueError:
            continue
        if tally <= 0:
            continue
        ed_key = int(ad) * 1000 + int(ed)
        # Identify candidate by substring match
        bucket = None
        for key, matchers in candidates_map.items():
            if any(m in unit for m in matchers):
                bucket = key
                break
        if bucket is None:
            continue
        rec = out.setdefault(ed_key, {})
        rec[bucket] = rec.get(bucket, 0) + tally
    return out


def fetch_presidential(year):
    print(f"  Fetching {year} presidential CSV...")
    raw = fetch_url(PRES_URLS[year]).decode("utf-8", errors="replace")
    cands = PRES_CANDIDATES[year]
    votes = parse_pres_csv(raw, cands)
    print(f"    parsed {len(votes):,} EDs with votes")
    total_d = sum(v.get("d", 0) for v in votes.values())
    total_r = sum(v.get("r", 0) for v in votes.values())
    print(f"    citywide D={total_d:,}  R={total_r:,}")
    return votes


# ---------- Mayoral parsing ----------
def fetch_mayoral(year):
    cfg = MAYOR_FILES[year]
    print(f"  Loading {year} mayoral geojson...")
    d = json.load(open(cfg["geojson"]))
    out = {}
    for f in d["features"]:
        p = f["properties"]
        ed_key = int(p["ed"])
        rec = {}
        for cand, field in cfg["candidates"].items():
            v = p.get(field) or 0
            try:
                rec[cand] = int(v)
            except (ValueError, TypeError):
                rec[cand] = 0
        if any(v > 0 for v in rec.values()):
            out[ed_key] = rec
    print(f"    {len(out):,} EDs with votes")
    return out


# ---------- ED centroids and tract assignment ----------
def build_ed_centroids(vintage):
    """Return dict[ed_key] -> (lon, lat). Uses 2021 or 2025 mayoral geojson as ED source."""
    fn = ARCHIVE / f"results_{vintage}_general_mayor.geojson"
    d = json.load(open(fn))
    out = {}
    for f in d["features"]:
        if not f.get("geometry"):
            continue
        ed_key = int(f["properties"]["ed"])
        c = shape(f["geometry"]).centroid
        out[ed_key] = (c.x, c.y)
    return out


def build_tract_lookup():
    tracts = json.load(open(ROOT / "nyc2020_tracts.geojson"))
    polys = [shape(f["geometry"]) for f in tracts["features"]]
    ids = [f["properties"]["geoid"] for f in tracts["features"]]
    return polys, ids


def aggregate_to_tracts(votes_by_ed, ed_centroids, tract_polys, tract_ids):
    """For each ED, find its containing tract and sum its votes there."""
    out = {gid: {} for gid in tract_ids}
    missing = 0
    for ed_key, cands in votes_by_ed.items():
        c = ed_centroids.get(ed_key)
        if not c:
            missing += 1
            continue
        pt = Point(c[0], c[1])
        hit = None
        for i, poly in enumerate(tract_polys):
            if poly.contains(pt):
                hit = tract_ids[i]
                break
        if not hit:
            continue
        rec = out[hit]
        for cand, n in cands.items():
            rec[cand] = rec.get(cand, 0) + n
    return out, missing


def main():
    print("=" * 60)
    print("Building tract polygons + centroid lookups...")
    tract_polys, tract_ids = build_tract_lookup()
    print(f"  {len(tract_polys):,} tracts loaded")

    ed_centroids_2021 = build_ed_centroids("2021")
    ed_centroids_2025 = build_ed_centroids("2025")
    print(f"  ED centroids: {len(ed_centroids_2021):,} (2021), {len(ed_centroids_2025):,} (2025)")

    # ---- Fetch each election ----
    print()
    print("Fetching presidential results...")
    pres_2020 = fetch_presidential("2020")
    pres_2024 = fetch_presidential("2024")

    print()
    print("Loading mayoral results...")
    mayor_2021 = fetch_mayoral("2021")
    mayor_2025 = fetch_mayoral("2025")

    # ---- Aggregate each to tracts ----
    print()
    print("Aggregating to tracts...")
    agg = {gid: {} for gid in tract_ids}

    for year, votes, vintage in [
        ("2020", pres_2020, "2021"),
        ("2024", pres_2024, "2025"),
    ]:
        centroids = ed_centroids_2021 if vintage == "2021" else ed_centroids_2025
        by_tract, missing = aggregate_to_tracts(votes, centroids, tract_polys, tract_ids)
        for gid, rec in by_tract.items():
            for cand, n in rec.items():
                agg[gid][f"pres_{year}_{cand}"] = agg[gid].get(f"pres_{year}_{cand}", 0) + n
        print(f"  pres {year}: {missing} EDs missing geometry")

    for year, votes, vintage in [
        ("2021", mayor_2021, "2021"),
        ("2025", mayor_2025, "2025"),
    ]:
        centroids = ed_centroids_2021 if vintage == "2021" else ed_centroids_2025
        by_tract, missing = aggregate_to_tracts(votes, centroids, tract_polys, tract_ids)
        for gid, rec in by_tract.items():
            for cand, n in rec.items():
                agg[gid][f"mayor_{year}_{cand}"] = agg[gid].get(f"mayor_{year}_{cand}", 0) + n
        print(f"  mayor {year}: {missing} EDs missing geometry")

    # ---- Compute derived percentages ----
    for gid, rec in agg.items():
        # Presidential — two-party share (D / (D+R))
        for year in ["2020", "2024"]:
            d = rec.get(f"pres_{year}_d") or 0
            r = rec.get(f"pres_{year}_r") or 0
            tot = d + r
            if tot >= 25:  # suppress tracts with very low vote counts
                rec[f"pres_{year}_d_pct"] = 100.0 * d / tot
                rec[f"pres_{year}_r_pct"] = 100.0 * r / tot
                rec[f"pres_{year}_total"] = tot

        # 2021 Mayoral — Adams vs Sliwa (de facto two-party)
        cfg21 = MAYOR_FILES["2021"]["candidates"]
        total21 = sum(rec.get(f"mayor_2021_{c}") or 0 for c in cfg21)
        if total21 >= 25:
            for c in cfg21:
                v = rec.get(f"mayor_2021_{c}") or 0
                rec[f"mayor_2021_{c}_pct"] = 100.0 * v / total21
            rec["mayor_2021_total"] = total21

        # 2025 Mayoral — Mamdani vs Cuomo vs Sliwa (three-way)
        cfg25 = MAYOR_FILES["2025"]["candidates"]
        total25 = sum(rec.get(f"mayor_2025_{c}") or 0 for c in cfg25)
        if total25 >= 25:
            for c in cfg25:
                v = rec.get(f"mayor_2025_{c}") or 0
                rec[f"mayor_2025_{c}_pct"] = 100.0 * v / total25
            rec["mayor_2025_total"] = total25

    # ---- Stats ----
    pres_2020_covered = sum(1 for r in agg.values() if "pres_2020_d_pct" in r)
    pres_2024_covered = sum(1 for r in agg.values() if "pres_2024_d_pct" in r)
    mayor_2021_covered = sum(1 for r in agg.values() if "mayor_2021_adams_pct" in r)
    mayor_2025_covered = sum(1 for r in agg.values() if "mayor_2025_mamdani_pct" in r)
    print()
    print("Tract coverage:")
    print(f"  pres 2020:  {pres_2020_covered:,}")
    print(f"  pres 2024:  {pres_2024_covered:,}")
    print(f"  mayor 2021: {mayor_2021_covered:,}")
    print(f"  mayor 2025: {mayor_2025_covered:,}")

    out = {
        "tracts": agg,
        "meta": {
            "elections": ["pres_2020", "pres_2024", "mayor_2021", "mayor_2025"],
            "source_pres": "NYC BoE citywide ED-level CSVs (vote.nyc)",
            "source_mayor": "nyc-election-archive ED-level geojsons",
            "method": "ED centroid → tract via point-in-polygon; ED → tract is 1-to-1 (EDs much smaller than tracts).",
            "ed_geometry_vintages": ED_GEOM_VINTAGE,
            "suppression_threshold": "Tracts with fewer than 25 total votes are suppressed for that election.",
        },
    }
    json.dump(out, open(DOCS / "elections_by_tract.json", "w"))
    print(f"\nWrote {DOCS/'elections_by_tract.json'}")


if __name__ == "__main__":
    main()
