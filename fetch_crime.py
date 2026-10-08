#!/usr/bin/env python3
"""Fetch NYC major-felony complaints for a rolling 12 months and aggregate to tracts.

Major felonies (NYPD's standard "Index 7" classification):
  Violent:  101 murder, 104 rape, 105 robbery, 106 felony assault
  Property: 107 burglary, 109 grand larceny, 110 grand larceny of motor vehicle

Window: last 12 months for which the Historic + Current YTD datasets combined have data.
Dates use RPT_DT (date the complaint was reported to NYPD), never CMPLT_FR_DT.

NYPD places un-geocodable complaints, and nearly every rape complaint, at the
precinct station house. Those have no known street location and are left out of the
tract counts (nypd_places.place); the count left out is recorded in meta. Complaints on
a tract boundary are shared equally among the tracts that meet there, so counts can be
fractional.

Output: docs/crime_by_tract.json  →  { GEOID: {violent: n, property: n, total: n} }
"""
import json
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

from nypd_places import TractIndex, load_stations, place

ROOT = Path(__file__).parent
DOCS = ROOT / "docs"

VIOLENT = {"101", "104", "105", "106"}
PROPERTY = {"107", "109", "110"}
ALL_CODES = VIOLENT | PROPERTY

# Datasets
HISTORIC = "qgea-i56i"      # 2006 → end of last completed year
CURRENT  = "5uac-w243"      # current YTD


def fetch_socrata(resource, where, fields, page=50000):
    """Paginate a Socrata query, return all rows."""
    base = f"https://data.cityofnewyork.us/resource/{resource}.json"
    out = []
    offset = 0
    while True:
        qs = urllib.parse.urlencode({
            "$select": ",".join(fields),
            "$where": where,
            "$limit": page,
            "$offset": offset,
            "$order": "cmplnt_num",
        })
        url = f"{base}?{qs}"
        req = urllib.request.Request(url, headers={"User-Agent": "nyc-population-map/1.0"})
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    rows = json.loads(r.read())
                break
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(2 * (attempt + 1))
        if not rows:
            break
        out.extend(rows)
        if len(rows) < page:
            break
        offset += page
    return out


def max_rpt_dt(resource):
    url = f"https://data.cityofnewyork.us/resource/{resource}.json?$select=max(rpt_dt)&$limit=1"
    req = urllib.request.Request(url, headers={"User-Agent": "nyc-population-map/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read())
    return datetime.strptime(data[0]["max_rpt_dt"][:10], "%Y-%m-%d").date()


def daterange_iso(d):
    return d.strftime("%Y-%m-%dT00:00:00")


def main():
    # Determine the rolling 12-month window: end = most recent date in CURRENT YTD
    # (or Historic if Current is empty), start = end - 365 days.
    end_current = max_rpt_dt(CURRENT)
    end_historic = max_rpt_dt(HISTORIC)
    end = max(end_current, end_historic)
    start = end - timedelta(days=364)  # inclusive 365-day window
    print(f"Window: {start} → {end}  ({(end - start).days + 1} days)")

    codes_clause = "ky_cd in(" + ",".join(f"'{c}'" for c in sorted(ALL_CODES)) + ")"
    fields = ["cmplnt_num", "rpt_dt", "ky_cd", "addr_pct_cd", "latitude", "longitude"]

    rows = []
    # If start falls in Historic's range, query Historic for [start, end_historic]
    if start <= end_historic:
        hi = min(end, end_historic)
        where = (
            f"rpt_dt between '{daterange_iso(start)}' and '{daterange_iso(hi)}' "
            f"AND {codes_clause} AND latitude IS NOT NULL"
        )
        print(f"Querying Historic ({HISTORIC}) for {start} → {hi} ...")
        h = fetch_socrata(HISTORIC, where, fields)
        print(f"  {len(h):,} rows")
        rows.extend(h)

    # If end extends into Current YTD's range, query Current for [max(start, jan1_current), end]
    if end > end_historic:
        lo = max(start, end_historic + timedelta(days=1))
        where = (
            f"rpt_dt between '{daterange_iso(lo)}' and '{daterange_iso(end)}' "
            f"AND {codes_clause} AND latitude IS NOT NULL"
        )
        print(f"Querying Current YTD ({CURRENT}) for {lo} → {end} ...")
        c = fetch_socrata(CURRENT, where, fields)
        print(f"  {len(c):,} rows")
        rows.extend(c)

    print(f"Total complaint rows: {len(rows):,}")

    # ---- place in tracts, spreading station-house complaints across the precinct ----
    recs = []
    for row in rows:
        try:
            lat, lon = float(row["latitude"]), float(row["longitude"])
        except (TypeError, ValueError, KeyError):
            continue
        if not (40.4 <= lat <= 41.0 and -74.3 <= lon <= -73.6):
            continue  # outside NYC bbox
        cat = "violent" if row["ky_cd"] in VIOLENT else "property"
        recs.append({"lat": lat, "lon": lon, "pct": row.get("addr_pct_cd"), "cat": cat})
    index = TractIndex()
    placed, rep = place(recs, load_stations(), index, "pct", "cat")
    counts = {gid: {"violent": 0, "property": 0, "total": 0} for gid in index.geoids}  # zeros are data
    for gid, c in placed.items():
        v, pr = c.get("violent", 0.0), c.get("property", 0.0)
        counts[gid] = {"violent": round(v, 3), "property": round(pr, 3), "total": round(v + pr, 3)}
    print(f"Placed {rep['located']:,} complaints by location ({rep['shared_on_boundary']:,} shared on a boundary); "
          f"left out {rep['left_out_at_station_house']:,} station-house complaints; {rep['unplaced']:,} fell outside every tract.")

    meta = {
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "window_days": (end - start).days + 1,
        "total_complaints": len(rows),
        "matched_complaints": round(sum(c["total"] for c in counts.values())),
        "station_house_complaints_left_out": rep["left_out_at_station_house"],
        "station_house_left_out_by_type": rep["left_out_by_category"],
        "shared_on_boundary": rep["shared_on_boundary"],
        "source_historic": HISTORIC,
        "source_current": CURRENT,
        "categories": {
            "violent": ["101 murder", "104 rape", "105 robbery", "106 felony assault"],
            "property": ["107 burglary", "109 grand larceny", "110 grand larceny of motor vehicle"],
        },
    }
    out = {"meta": meta, "counts": counts}
    json.dump(out, open(DOCS / "crime_by_tract.json", "w"))
    print(f"Wrote {DOCS/'crime_by_tract.json'}")
    print("Window:", meta["window_start"], "→", meta["window_end"])
    print("Total matched:", meta["matched_complaints"])


if __name__ == "__main__":
    main()
