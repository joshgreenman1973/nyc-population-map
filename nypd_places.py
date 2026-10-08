"""Place NYPD records (arrests, complaints, shootings) in 2020 census tracts.

NYPD puts a record it cannot geocode, and every rape complaint (to protect the
victim), at the station house of the precinct where it happened. Those records have
no known street location. They are found here (any record within STATION_RADIUS_M of
its own precinct's station house) and LEFT OUT of the tract counts, and counted in
the report so the map can say how many are missing. No record is ever assigned to a
place by its precinct alone.

Every other record sits at an intersection or the middle of a street segment, as NYPD
geocodes them. When that point is on a tract boundary, the record is shared equally
among the tracts that meet there (see SHARE_M).

Station houses come from data/stations.json: each precinct's address from its NYPD
web page, geocoded with NYC Planning's GeoSearch, plus the 40th Precinct's former
building, where NYPD still places its records.
"""
import json
import math
from collections import defaultdict
from pathlib import Path

from shapely.geometry import Point, shape
from shapely.ops import transform
from shapely.strtree import STRtree

ROOT = Path(__file__).parent
# NYPD places every record at an intersection or the middle of a street segment
# (arrest data footnote 4), and most tract and neighborhood boundaries run down street
# centerlines, so about half of street-located arrests sit on a tract boundary. A record
# within SHARE_M of a boundary is shared equally among the tracts that meet there,
# rather than assigned to whichever side a hairline difference in linework favors.
SHARE_M = 10
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


# Local planar coordinates in meters (degree lengths at 40.7 N, WGS84), accurate to
# well under 1% across the city -- plenty for a 10-meter test.
M_PER_DEG_LAT = 111_048.0
M_PER_DEG_LON = 84_512.0
LON0, LAT0 = -74.0, 40.7


def to_m(lon, lat, z=None):
    return (lon - LON0) * M_PER_DEG_LON, (lat - LAT0) * M_PER_DEG_LAT


class TractIndex:
    def __init__(self, path=None):
        gj = json.load(open(path or ROOT / "nyc2020_tracts.geojson"))
        self.geoids = [f["properties"]["geoid"] for f in gj["features"]]
        self.geoms = [shape(f["geometry"]) for f in gj["features"]]
        self.tree = STRtree(self.geoms)
        self.geoms_m = [transform(to_m, g) for g in self.geoms]
        self.tree_m = STRtree(self.geoms_m)

    def tracts_of(self, lat, lon, share_m=SHARE_M, snap_m=150):
        """[(geoid, weight)] for a street-geocoded record: every tract within share_m
        gets an equal share (one tract, weight 1, for a point well inside a tract).
        Points outside every tract (piers, the shoreline clip) snap to the nearest
        tract within snap_m; farther than that, []."""
        pt = Point(*to_m(lon, lat))
        near = [i for i in self.tree_m.query(pt.buffer(share_m)) if self.geoms_m[i].distance(pt) <= share_m]
        if near:
            return [(self.geoids[i], 1.0 / len(near)) for i in near]
        i = self.tree_m.nearest(pt)
        if i is not None and self.geoms_m[i].distance(pt) <= snap_m:
            return [(self.geoids[i], 1.0)]
        return []

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
    """Assign street-located records to tracts; leave out station-house records.

    records: dicts with float 'lat', 'lon', a precinct under precinct_key and a
    category (e.g. age group) under category_key.
    Returns (counts, report): counts[geoid][category] = float count (fractional where
    a record sits on a boundary and is shared).
    """
    counts = defaultdict(lambda: defaultdict(float))
    at_station = defaultdict(int)        # category -> records left out
    at_station_pct = defaultdict(int)    # precinct -> records left out
    located_pct = defaultdict(int)       # precinct -> records placed
    rec_by_cat = defaultdict(int)
    unplaced = shared = no_precinct = located = 0
    for r in records:
        lat, lon = r["lat"], r["lon"]
        cat = r[category_key]
        rec_by_cat[cat] += 1
        try:
            pct = int(r.get(precinct_key))
        except (TypeError, ValueError):
            pct = None
        if pct is None or pct not in stations:
            no_precinct += 1
            pct = None
        if pct is not None and any(meters(lat, lon, a, b) <= radius for a, b in stations[pct]):
            at_station[cat] += 1
            at_station_pct[pct] += 1
            continue
        shares = index.tracts_of(lat, lon)
        if not shares:
            unplaced += 1
            continue
        if len(shares) > 1:
            shared += 1
        for g, w in shares:
            counts[g][cat] += w
        located += 1
        if pct is not None:
            located_pct[pct] += 1
    share_by_pct = {p: at_station_pct[p] / (at_station_pct[p] + located_pct[p])
                    for p in set(at_station_pct) | set(located_pct) if at_station_pct[p] + located_pct[p] >= 50}
    report = {
        "records": len(records),
        "records_by_category": dict(rec_by_cat),
        "left_out_at_station_house": int(sum(at_station.values())),
        "left_out_by_category": dict(at_station),
        "located": located,
        "shared_on_boundary": shared,
        "unplaced": unplaced,
        "no_precinct": no_precinct,
        "station_share_by_precinct_range": [round(min(share_by_pct.values()), 3), round(max(share_by_pct.values()), 3)]
                                           if share_by_pct else None,
    }
    placed = sum(sum(c.values()) for c in counts.values())
    if abs(placed - located) > 1e-6 * max(1, located):
        raise SystemExit(f"placement lost records: {placed} vs {located}")
    return counts, report
