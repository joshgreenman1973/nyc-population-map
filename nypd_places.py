"""Place NYPD records (arrests, complaints, shootings) in 2020 census tracts.

NYPD puts a record it cannot geocode, and every rape complaint (to protect the
victim), at the station house of the precinct where it happened. Left alone,
those records pile up in whichever tract holds the station house. In 2024, about a quarter of
arrests of people under 25 sat within 130 meters of their own precinct's station
house; in the first half of 2026, 94% of rape complaints did.

This module finds those station-house records and spreads them across the
precinct in proportion to where the precinct's other records of the same kind
were located. Totals are preserved; only the location of the station-house share
is estimated. Everything is documented in methodology.html.

Station houses come from data/stations.json: each precinct's address from its NYPD
web page, geocoded with NYC Planning's GeoSearch, plus the 40th Precinct's former
building, where NYPD still places its records. It is built by fetch_youth.py in the
summer jobs factor map (github.com/Vital-City-NYC/syep-factor-map) and copied here.
"""
import json
import math
from collections import defaultdict
from pathlib import Path

from shapely.geometry import Point, shape
from shapely.strtree import STRtree

ROOT = Path(__file__).parent
STATION_RADIUS_M = 140   # see methodology: in every precinct but one (the 14th, where
                         # Macy's Herald Square tops the list) the busiest arrest point
                         # falls within this distance of the geocoded station house


def meters(lat1, lon1, lat2, lon2):
    """Great-circle (haversine) distance in meters, mean Earth radius."""
    R = 6_371_008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def load_stations(path=None):
    """precinct number -> list of (lat, lon) station-house points."""
    rows = json.load(open(path or ROOT / "data" / "stations.json"))
    out = defaultdict(list)
    for r in rows:
        out[int(r["precinct"])].append((float(r["lat"]), float(r["lon"])))
    if len(out) != 78:
        raise SystemExit(f"stations.json covers {len(out)} precincts, expected 78")
    return out


class TractIndex:
    def __init__(self, path=None):
        gj = json.load(open(path or ROOT / "nyc2020_tracts.geojson"))
        self.geoids = [f["properties"]["geoid"] for f in gj["features"]]
        self.geoms = [shape(f["geometry"]) for f in gj["features"]]
        self.tree = STRtree(self.geoms)

    def tract_of(self, lat, lon, snap_m=150):
        """Tract containing the point. Points that fall just outside every polygon
        (piers, the shoreline clip) snap to the nearest tract within snap_m."""
        pt = Point(lon, lat)
        for i in self.tree.query(pt):
            if self.geoms[i].contains(pt):
                return self.geoids[i]
        i = self.tree.nearest(pt)
        if i is not None:
            # distance in degrees -> rough meters
            d = self.geoms[i].distance(pt) * 111_320 * math.cos(math.radians(lat))
            if d <= snap_m:
                return self.geoids[i]
        return None


def place(records, stations, index, precinct_key, category_key, radius=STATION_RADIUS_M):
    """Assign records to tracts, reallocating station-house records.

    records: dicts with float 'lat', 'lon', a precinct under precinct_key and a
    category (e.g. age group) under category_key.
    Returns (counts, report): counts[geoid][category] = float count.
    """
    located = defaultdict(lambda: defaultdict(float))   # (precinct, cat) -> tract -> n
    at_station = defaultdict(float)                      # (precinct, cat) -> n
    unplaced = 0
    no_precinct = 0
    for r in records:
        lat, lon = r["lat"], r["lon"]
        try:
            pct = int(r.get(precinct_key))
        except (TypeError, ValueError):
            pct = None
        cat = r[category_key]
        if pct is None or pct not in stations:
            no_precinct += 1
            pct = None
        if pct is not None and any(meters(lat, lon, a, b) <= radius for a, b in stations[pct]):
            at_station[(pct, cat)] += 1
            continue
        g = index.tract_of(lat, lon)
        if g is None:
            unplaced += 1
            continue
        located[(pct, cat)][g] += 1

    counts = defaultdict(lambda: defaultdict(float))
    for (pct, cat), tracts in located.items():
        for g, n in tracts.items():
            counts[g][cat] += n

    # Spread each precinct's station-house records like its located ones. If a
    # precinct has no located records in that category, fall back to all of its
    # located records; if it has none at all, keep them at the station tract.
    fallback_used = []
    by_pct_all = defaultdict(lambda: defaultdict(float))
    for (pct, cat), tracts in located.items():
        for g, n in tracts.items():
            by_pct_all[pct][g] += n
    for (pct, cat), n in at_station.items():
        dist = located.get((pct, cat))
        if not dist or sum(dist.values()) == 0:
            dist = by_pct_all.get(pct)
            fallback_used.append((pct, cat, n))
        if not dist or sum(dist.values()) == 0:
            a, b = stations[pct][0]
            g = index.tract_of(a, b)
            counts[g][cat] += n
            continue
        tot = sum(dist.values())
        for g, m in dist.items():
            counts[g][cat] += n * m / tot

    by_cat = defaultdict(int)
    for (pct, cat), n in at_station.items():
        by_cat[cat] += int(n)
    rec_by_cat = defaultdict(int)
    for r in records:
        rec_by_cat[r[category_key]] += 1
    report = {
        "records": len(records),
        "records_by_category": dict(rec_by_cat),
        "at_station_house_by_category": dict(by_cat),
        "at_station_house": int(sum(at_station.values())),
        "located": int(sum(sum(t.values()) for t in located.values())),
        "unplaced": unplaced,
        "no_precinct": no_precinct,
        "fallbacks": fallback_used,
    }
    # Conservation check: every placed record is accounted for exactly once.
    placed = sum(sum(c.values()) for c in counts.values())
    expect = report["located"] + report["at_station_house"]
    if abs(placed - expect) > 1e-6 * max(1, expect):
        raise SystemExit(f"placement lost records: {placed} vs {expect}")
    return counts, report
