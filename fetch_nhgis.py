#!/usr/bin/env python3
"""Parse the NHGIS DHC extract (P9 + P12) and emit per-tract 2020 Decennial-count metrics.

Input:  nhgis/nhgis0003_csv/nhgis0003_ds258_2020_tract.csv  (Statewide NY DHC P9 + P12)
Output: docs/nhgis_dhc_by_tract.json
  { GEOID: { pct_white_nh_2020, pct_black_nh_2020, pct_asian_nh_2020,
             pct_hispanic_2020, pct_under18_2020, pct_over65_2020 } }

These are 100% Decennial counts (no MOE), filtered to NYC's five counties.
"""
import csv
import json
from pathlib import Path

ROOT = Path(__file__).parent
DOCS = ROOT / "docs"
CSV_PATH = ROOT / "nhgis/nhgis0003_csv/nhgis0003_ds258_2020_tract.csv"

NYC_COUNTY_FIPS = {"36005", "36047", "36061", "36081", "36085"}

# P9 (NHGIS code U7P): Hispanic or Latino × Race 70
# U7P001 = Total, U7P002 = Hispanic any race, U7P005-U7P010 = NH single-race breakdowns.
P9_TOTAL    = "U7P001"
P9_HISPANIC = "U7P002"
P9_WHITE_NH = "U7P005"
P9_BLACK_NH = "U7P006"
P9_ASIAN_NH = "U7P008"

# P12 (NHGIS code U7S): Sex × Age, 23-bucket version.
# Male: U7S002 total, U7S003-U7S025 age buckets (under 5 → 85+).
# Female: U7S026 total, U7S027-U7S049 age buckets.
P12_TOTAL = "U7S001"
# Under 18 buckets: Under 5 / 5-9 / 10-14 / 15-17 — same indices both sexes
P12_M_U18  = ["U7S003", "U7S004", "U7S005", "U7S006"]
P12_F_U18  = ["U7S027", "U7S028", "U7S029", "U7S030"]
# 65+ buckets: 65-66 / 67-69 / 70-74 / 75-79 / 80-84 / 85+
P12_M_65P  = ["U7S020", "U7S021", "U7S022", "U7S023", "U7S024", "U7S025"]
P12_F_65P  = ["U7S044", "U7S045", "U7S046", "U7S047", "U7S048", "U7S049"]


def intval(s):
    try: return int(s)
    except (ValueError, TypeError): return 0


def pct(num, denom):
    if denom is None or denom == 0: return None
    return 100.0 * num / denom


out = {}
nyc_count = 0
with open(CSV_PATH) as f:
    reader = csv.DictReader(f)
    for row in reader:
        geoid = row.get("GEOCODE") or ""  # 11-char state+county+tract
        if len(geoid) != 11 or geoid[:5] not in NYC_COUNTY_FIPS:
            continue
        total = intval(row.get(P9_TOTAL))
        if total == 0:
            continue
        under18 = sum(intval(row.get(c)) for c in P12_M_U18 + P12_F_U18)
        over65  = sum(intval(row.get(c)) for c in P12_M_65P + P12_F_65P)
        out[geoid] = {
            "pop_2020_dhc":          total,
            "_white_nh_2020_n":      intval(row.get(P9_WHITE_NH)),
            "_black_nh_2020_n":      intval(row.get(P9_BLACK_NH)),
            "_asian_nh_2020_n":      intval(row.get(P9_ASIAN_NH)),
            "_hispanic_2020_n":      intval(row.get(P9_HISPANIC)),
            "_under18_2020_n":       under18,
            "_over65_2020_n":        over65,
            "pct_white_nh_2020":     pct(intval(row.get(P9_WHITE_NH)),    total),
            "pct_black_nh_2020":     pct(intval(row.get(P9_BLACK_NH)),    total),
            "pct_asian_nh_2020":     pct(intval(row.get(P9_ASIAN_NH)),    total),
            "pct_hispanic_2020":     pct(intval(row.get(P9_HISPANIC)),    total),
            "pct_under18_2020":      pct(under18, total),
            "pct_over65_2020":       pct(over65,  total),
        }
        nyc_count += 1

print(f"Parsed {nyc_count} NYC tracts from NHGIS DHC extract.")
json.dump(out, open(DOCS / "nhgis_dhc_by_tract.json", "w"))
print(f"Wrote {DOCS/'nhgis_dhc_by_tract.json'}")
