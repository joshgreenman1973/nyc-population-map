#!/usr/bin/env python3
"""Build NYC population map data.

Fetches latest ACS 5-year data from the Census Reporter API
(https://api.censusreporter.org) — no API key required.
Joins with NYC tract geometry and outputs:
  docs/tracts.geojson  — one feature per NYC tract with all metrics baked in
  docs/variables.json  — metric metadata for the UI
  docs/release.json    — name/year of the ACS release used
"""
import json
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
DOCS = ROOT / "docs"
DOCS.mkdir(exist_ok=True)

NYC_COUNTIES = {
    "36005": "Bronx",
    "36047": "Brooklyn",
    "36061": "Manhattan",
    "36081": "Queens",
    "36085": "Staten Island",
}

# Census Reporter caps requests at 11 tables. Batch accordingly.
TABLE_BATCHES = [
    # Demographics + income
    ["B01003", "B01002", "B01001", "B03002", "B19013", "B19001", "B17001", "B11016", "B25010", "B15003", "B05002"],
    # Housing, language, work, other
    ["C16001", "B25003", "B25064", "B25077", "B25071", "B08301", "B23025", "B21001", "B22010", "B28002"],
    # Vehicles + schools
    ["B08201", "B25044", "B14002"],
]


def fetch(url, retries=4):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; nyc-population-map/1.0; +https://github.com/vitalcity-nyc)",
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except Exception as e:
            if i == retries - 1:
                raise
            time.sleep(2 * (i + 1))


def fetch_county(county_fips):
    """Return (estimates, errors, release).
    estimates / errors: dict[tract_geoid] -> dict[column_id] -> value (or MOE).
    """
    est_out = {}
    moe_out = {}
    release = None
    for batch in TABLE_BATCHES:
        url = (
            "https://api.censusreporter.org/1.0/data/show/latest"
            f"?table_ids={','.join(batch)}"
            f"&geo_ids=140|05000US{county_fips}"
        )
        data = fetch(url)
        release = data.get("release")
        for geo, tables in data["data"].items():
            tract = geo[-11:]
            erow = est_out.setdefault(tract, {})
            mrow = moe_out.setdefault(tract, {})
            for tbl, payload in tables.items():
                for col, val in payload.get("estimate", {}).items():
                    erow[col] = val
                for col, val in payload.get("error", {}).items():
                    mrow[col] = val
    return est_out, moe_out, release


print("Fetching from Census Reporter (no API key required)...")
all_data = {}
all_moes = {}
release_meta = None
for cfips, name in NYC_COUNTIES.items():
    print(f"  {cfips} {name} ...")
    est, moe, rel = fetch_county(cfips)
    all_data.update(est)
    all_moes.update(moe)
    release_meta = rel
print(f"Got {len(all_data)} tracts. Release: {release_meta}")


# ---------- helpers ----------
def get(d, k):
    v = d.get(k)
    return v if isinstance(v, (int, float)) else None


def sum_safe(d, keys):
    total = 0.0
    seen = False
    for k in keys:
        v = get(d, k)
        if v is None:
            continue
        total += v
        seen = True
    return total if seen else None


def pct(num, denom):
    if num is None or denom is None or denom <= 0:
        return None
    return 100.0 * num / denom


# ---------- MOE propagation (ACS Handbook Appendix 3 formulas) ----------
# All MOEs from Census Reporter are at the 90% confidence level.
import math as _math

def moe_sum(moes):
    """MOE of a sum of independent cells = sqrt(Σ MOE²)."""
    vals = [m for m in moes if m is not None]
    if not vals:
        return None
    return _math.sqrt(sum(m * m for m in vals))


def moe_proportion(num, num_moe, denom, denom_moe):
    """MOE of a proportion p = x / y where x is a subset of y (ACS Handbook).

    moe(p) = sqrt(moe(x)² − p² · moe(y)²) / y
    If the radicand is negative (rare; happens when x is close to y), fall back to the ratio formula.
    Returns the MOE of the proportion in the same units as the proportion (0–1, not %).
    """
    if num is None or denom in (None, 0) or num_moe is None or denom_moe is None:
        return None
    p = num / denom
    rad = num_moe * num_moe - p * p * denom_moe * denom_moe
    if rad < 0:
        # fallback to ratio formula (independent x and y assumption)
        rad = num_moe * num_moe + p * p * denom_moe * denom_moe
    return _math.sqrt(rad) / denom


def moe_pct(num, num_moe, denom, denom_moe):
    """As moe_proportion but in percentage points."""
    m = moe_proportion(num, num_moe, denom, denom_moe)
    return m * 100 if m is not None else None


def cv(estimate, moe):
    """Coefficient of variation = MOE / (1.645 · estimate). Used for reliability flagging.
    Census Bureau guidance: CV ≤ 0.12 reliable, 0.12–0.40 use with caution, > 0.40 unreliable.
    """
    if estimate is None or moe is None or estimate == 0:
        return None
    return moe / (1.645 * abs(estimate))


# ---------- derive metrics (refactored as function so we can call it per-tract AND per-NTA aggregate) ----------
RELIABILITY_THRESHOLD = 0.30  # MOE > 30% of estimate → flag as unreliable

def derive_one(d, m):
        """Given raw ACS estimate dict d and MOE dict m, return (record, moes_dict)."""
        pop = get(d, "B01003001"); pop_moe = get(m, "B01003001")
        total_race = get(d, "B03002001")
        hh = get(d, "B11016001")
        inc_hh = get(d, "B19001001")
        edu = get(d, "B15003001")
        fb_total = get(d, "B05002001")
        lang_total = get(d, "C16001001")
        tenure = get(d, "B25003001")
        pov_total = get(d, "B17001001")
        commute = get(d, "B08301001")
        lf_total = get(d, "B23025001")       # population 16+
        lf_in = get(d, "B23025002")           # in labor force (civilian + armed forces)
        lf_civilian = get(d, "B23025003")     # civilian labor force — the BLS unemployment denominator
        vet_total = get(d, "B21001001")
        snap_total = get(d, "B22010001")
        internet_total = get(d, "B28002001")

        under18 = sum_safe(d, ["B01001003", "B01001004", "B01001005", "B01001006",
                                "B01001027", "B01001028", "B01001029", "B01001030"])
        over65 = sum_safe(d, ["B01001020", "B01001021", "B01001022", "B01001023", "B01001024", "B01001025",
                               "B01001044", "B01001045", "B01001046", "B01001047", "B01001048", "B01001049"])
        inc_u30 = sum_safe(d, [f"B19001{i:03d}" for i in range(2, 7)])
        inc_30_60 = sum_safe(d, [f"B19001{i:03d}" for i in range(7, 12)])
        inc_60_100 = sum_safe(d, ["B19001012", "B19001013"])
        inc_100_150 = sum_safe(d, ["B19001014", "B19001015"])
        inc_150_200 = sum_safe(d, ["B19001016"])
    inc_200p    = sum_safe(d, ["B19001017"])
    inc_150p    = sum_safe(d, ["B19001016", "B19001017"])
        bach_plus = sum_safe(d, ["B15003022", "B15003023", "B15003024", "B15003025"])
        hs_only = sum_safe(d, ["B15003017", "B15003018"])

        english_only = get(d, "C16001002")
        non_english = (lang_total - english_only) if (lang_total is not None and english_only is not None) else None

        # ---- Vehicles (B08201 = households × vehicles, B25044 = tenure × vehicles) ----
        veh_total = get(d, "B08201001")
        veh_0     = get(d, "B08201002")
        veh_1     = get(d, "B08201003")
        veh_2     = get(d, "B08201004")
        veh_3     = get(d, "B08201005")
        veh_4plus = get(d, "B08201006")
        veh_2plus = sum_safe(d, ["B08201004", "B08201005", "B08201006"])
        # Weighted average vehicles per household; use 4.5 as midpoint for the "4 or more" bucket.
        if veh_total and veh_total > 0 and all(v is not None for v in [veh_0, veh_1, veh_2, veh_3, veh_4plus]):
            avg_veh = (0*veh_0 + 1*veh_1 + 2*veh_2 + 3*veh_3 + 4.5*veh_4plus) / veh_total
        else:
            avg_veh = None
        # Owner-occupied no vehicle vs renter-occupied no vehicle
        own_total = get(d, "B25044002")
        own_no_v  = get(d, "B25044003")
        rent_total= get(d, "B25044009")
        rent_no_v = get(d, "B25044010")

        # ---- Schools (B14002 K-12 by public vs. private) ----
        # K-12 = kindergarten through grade 12; sum male + female.
        k12_public = sum_safe(d, [
            "B14002008", "B14002011", "B14002014", "B14002017",  # male K, 1-4, 5-8, 9-12 public
            "B14002032", "B14002035", "B14002038", "B14002041",  # female K, 1-4, 5-8, 9-12 public
        ])
        k12_private = sum_safe(d, [
            "B14002009", "B14002012", "B14002015", "B14002018",  # male K, 1-4, 5-8, 9-12 private
            "B14002033", "B14002036", "B14002039", "B14002042",  # female K, 1-4, 5-8, 9-12 private
        ])
        k12_total = None
        if k12_public is not None and k12_private is not None:
            k12_total = k12_public + k12_private

        rec = {
            "pop_total": pop,
            "median_age": get(d, "B01002001"),
            "pct_under18": pct(under18, pop),
            "pct_over65": pct(over65, pop),

            "pct_white_nh": pct(get(d, "B03002003"), total_race),
            "pct_black_nh": pct(get(d, "B03002004"), total_race),
            "pct_asian_nh": pct(get(d, "B03002006"), total_race),
            "pct_hispanic": pct(get(d, "B03002012"), total_race),

            "median_hh_income": get(d, "B19013001"),
            "pct_poverty": pct(get(d, "B17001002"), pov_total),
            "pct_hh_under30k": pct(inc_u30, inc_hh),
            "pct_hh_30_60k": pct(inc_30_60, inc_hh),
            "pct_hh_60_100k": pct(inc_60_100, inc_hh),
            "pct_hh_100_150k": pct(inc_100_150, inc_hh),
            "pct_hh_150kplus":  pct(inc_150p, inc_hh),
        "pct_hh_150_200k":  pct(inc_150_200, inc_hh),
        "pct_hh_200kplus":  pct(inc_200p, inc_hh),
            "pct_snap": pct(get(d, "B22010002"), snap_total),

            "households": hh,
            "avg_hh_size": get(d, "B25010001"),
            "pct_owner_occupied": pct(get(d, "B25003002"), tenure),

            "median_gross_rent": get(d, "B25064001"),
            "median_home_value": get(d, "B25077001"),
            "median_rent_burden": get(d, "B25071001"),

            "pct_bachelor_plus": pct(bach_plus, edu),
            "pct_hs_only": pct(hs_only, edu),

            "pct_foreign_born": pct(get(d, "B05002013"), fb_total),
            "pct_non_citizen":  pct(get(d, "B05002021"), fb_total),  # foreign-born non-citizens as a share of all residents
            "pct_non_english_home": pct(non_english, lang_total),

            "pct_in_labor_force": pct(lf_in, lf_total),
            # Unemployment rate uses the BLS-canonical denominator: civilian labor force.
            "pct_unemployed": pct(get(d, "B23025005"), lf_civilian),
            "pct_public_transit": pct(get(d, "B08301010"), commute),
            "pct_walked": pct(get(d, "B08301019"), commute),
            "pct_wfh": pct(get(d, "B08301021"), commute),

            "pct_veteran": pct(get(d, "B21001002"), vet_total),
            "pct_no_internet": pct(get(d, "B28002013"), internet_total),

            # Vehicles (B08201 / B25044)
            "pct_no_vehicle":        pct(veh_0,     veh_total),
            "pct_2plus_vehicles":    pct(veh_2plus, veh_total),
            "avg_vehicles_per_hh":   avg_veh,
            "pct_owner_no_vehicle":  pct(own_no_v,  own_total),
            "pct_renter_no_vehicle": pct(rent_no_v, rent_total),

            # Schools (B14002 K-12 by public vs. private)
            "pct_kids_public_k12":   pct(k12_public,  k12_total),
            "pct_kids_private_k12":  pct(k12_private, k12_total),
            "k12_students":          int(k12_total) if k12_total else None,
        }

        # ===== MOEs for headline variables =====
        # Sum-of-cells MOEs for derived numerators
        under18_moe = moe_sum([get(m, k) for k in [
            "B01001003","B01001004","B01001005","B01001006",
            "B01001027","B01001028","B01001029","B01001030"]])
        over65_moe = moe_sum([get(m, k) for k in [
            "B01001020","B01001021","B01001022","B01001023","B01001024","B01001025",
            "B01001044","B01001045","B01001046","B01001047","B01001048","B01001049"]])
        bach_plus_moe = moe_sum([get(m, k) for k in ["B15003022","B15003023","B15003024","B15003025"]])
        hs_only_moe   = moe_sum([get(m, k) for k in ["B15003017","B15003018"]])
        inc_u30_moe   = moe_sum([get(m, f"B19001{i:03d}") for i in range(2, 7)])
        inc_150p_moe  = moe_sum([get(m, f"B19001{i:03d}") for i in [16, 17]])
        # Non-English: total - English-only. Difference MOE ≈ sum-of-squares.
        non_eng_moe = moe_sum([get(m, "C16001001"), get(m, "C16001002")])
        # K-12 public/private/total sums
        k12_pub_moe = moe_sum([get(m, k) for k in [
            "B14002008","B14002011","B14002014","B14002017",
            "B14002032","B14002035","B14002038","B14002041"]])
        k12_pri_moe = moe_sum([get(m, k) for k in [
            "B14002009","B14002012","B14002015","B14002018",
            "B14002033","B14002036","B14002039","B14002042"]])
        k12_tot_moe = moe_sum([k12_pub_moe, k12_pri_moe])

        moes = {
            # Direct estimates / medians
            "pop_total":          pop_moe,
            "median_age":         get(m, "B01002001"),
            "median_hh_income":   get(m, "B19013001"),
            "median_gross_rent":  get(m, "B25064001"),
            "median_home_value":  get(m, "B25077001"),
            "median_rent_burden": get(m, "B25071001"),
            "avg_hh_size":        get(m, "B25010001"),
            "households":         get(m, "B11016001"),
            "k12_students":       k12_tot_moe,
            # Age shares
            "pct_under18": moe_pct(under18, under18_moe, pop, pop_moe),
            "pct_over65":  moe_pct(over65,  over65_moe,  pop, pop_moe),
            # Race / ethnicity shares — denominator is B03002_001 (= pop for these purposes)
            "pct_white_nh":  moe_pct(get(d,"B03002003"), get(m,"B03002003"), total_race, get(m,"B03002001")),
            "pct_black_nh":  moe_pct(get(d,"B03002004"), get(m,"B03002004"), total_race, get(m,"B03002001")),
            "pct_asian_nh":  moe_pct(get(d,"B03002006"), get(m,"B03002006"), total_race, get(m,"B03002001")),
            "pct_hispanic":  moe_pct(get(d,"B03002012"), get(m,"B03002012"), total_race, get(m,"B03002001")),
            # Poverty
            "pct_poverty":   moe_pct(get(d,"B17001002"), get(m,"B17001002"), pov_total, get(m,"B17001001")),
            # Income brackets — just the two tail brackets (most editorial weight)
            "pct_hh_under30k":  moe_pct(inc_u30,  inc_u30_moe,  inc_hh, get(m,"B19001001")),
            "pct_hh_150kplus":  moe_pct(inc_150p, inc_150p_moe, inc_hh, get(m,"B19001001")),
        "pct_hh_200kplus":  moe_pct(inc_200p, get(m,"B19001017"), inc_hh, get(m,"B19001001")),
            # SNAP, tenure
            "pct_snap":             moe_pct(get(d,"B22010002"), get(m,"B22010002"), snap_total, get(m,"B22010001")),
            "pct_owner_occupied":   moe_pct(get(d,"B25003002"), get(m,"B25003002"), tenure, get(m,"B25003001")),
            # Education
            "pct_bachelor_plus":    moe_pct(bach_plus, bach_plus_moe, edu, get(m,"B15003001")),
            "pct_hs_only":          moe_pct(hs_only,   hs_only_moe,   edu, get(m,"B15003001")),
            # Origin / language
            "pct_foreign_born":     moe_pct(get(d,"B05002013"), get(m,"B05002013"), fb_total, get(m,"B05002001")),
            "pct_non_citizen":      moe_pct(get(d,"B05002021"), get(m,"B05002021"), fb_total, get(m,"B05002001")),
            "pct_non_english_home": moe_pct(non_english, non_eng_moe, lang_total, get(m,"C16001001")),
            # Work
            "pct_in_labor_force":   moe_pct(lf_in, get(m,"B23025002"), lf_total, get(m,"B23025001")),
            "pct_unemployed":       moe_pct(get(d,"B23025005"), get(m,"B23025005"), lf_civilian, get(m,"B23025003")),
            "pct_public_transit":   moe_pct(get(d,"B08301010"), get(m,"B08301010"), commute, get(m,"B08301001")),
            "pct_walked":           moe_pct(get(d,"B08301019"), get(m,"B08301019"), commute, get(m,"B08301001")),
            "pct_wfh":              moe_pct(get(d,"B08301021"), get(m,"B08301021"), commute, get(m,"B08301001")),
            # Other
            "pct_veteran":     moe_pct(get(d,"B21001002"), get(m,"B21001002"), vet_total,      get(m,"B21001001")),
            "pct_no_internet": moe_pct(get(d,"B28002013"), get(m,"B28002013"), internet_total, get(m,"B28002001")),
            # Vehicles
            "pct_no_vehicle":        moe_pct(veh_0,     get(m,"B08201002"), veh_total,  get(m,"B08201001")),
            "pct_2plus_vehicles":    moe_pct(veh_2plus, moe_sum([get(m,"B08201004"),get(m,"B08201005"),get(m,"B08201006")]), veh_total, get(m,"B08201001")),
            "pct_owner_no_vehicle":  moe_pct(own_no_v,  get(m,"B25044003"), own_total,  get(m,"B25044002")),
            "pct_renter_no_vehicle": moe_pct(rent_no_v, get(m,"B25044010"), rent_total, get(m,"B25044009")),
            # Schools
            "pct_kids_public_k12":   moe_pct(k12_public,  k12_pub_moe, k12_total, k12_tot_moe),
            "pct_kids_private_k12":  moe_pct(k12_private, k12_pri_moe, k12_total, k12_tot_moe),
        }

        return rec, moes

derived = {}
all_moes_ref = all_moes  # kept for NTA aggregation later
for tract, d in all_data.items():
    m = all_moes.get(tract, {})
    rec, moes = derive_one(d, m)
    # Attach MOE companion fields with size-aware rounding.
    for key, moe_val in moes.items():
        if moe_val is None: continue
        if moe_val >= 1000: rec[key + '_moe'] = round(moe_val)
        elif moe_val >= 10: rec[key + '_moe'] = round(moe_val, 1)
        else: rec[key + '_moe'] = round(moe_val, 2)
    derived[tract] = rec

# ---------- merge NHGIS 2020 DHC race/age 100%-count metrics (optional) ----------
nhgis_path = DOCS / "nhgis_dhc_by_tract.json"
if nhgis_path.exists():
    print("Merging NHGIS 2020 DHC counts...")
    nh = json.load(open(nhgis_path))
    matched = 0
    for tract, d in derived.items():
        rec = nh.get(tract)
        if not rec: continue
        for k, v in rec.items():
            d[k] = v
        matched += 1
    print(f"  attached DHC metrics to {matched} tracts")
else:
    print(f"No {nhgis_path.name} — run fetch_nhgis.py first.")


# ---------- merge election results (optional) ----------
elec_path = DOCS / "elections_by_tract.json"
if elec_path.exists():
    print("Merging election results...")
    ej = json.load(open(elec_path))
    elec = ej.get("tracts", {})
    matched = 0
    for tract, d in derived.items():
        rec = elec.get(tract, {})
        if not rec:
            continue
        # Copy all *_pct + total fields straight in
        for k, v in rec.items():
            if k.endswith("_pct") or k.endswith("_total") or k.startswith("pres_") or k.startswith("mayor_"):
                d[k] = v
        # Derived: D-share shift from 2020 to 2024 presidential (percentage points)
        d20 = rec.get("pres_2020_d_pct")
        d24 = rec.get("pres_2024_d_pct")
        if d20 is not None and d24 is not None:
            d["pres_d_shift_2020_2024"] = d24 - d20
        matched += 1
    print(f"  attached election data to {matched} tracts")
else:
    print(f"No {elec_path.name} — run fetch_elections.py to populate.")


# ---------- merge NYC DOE public-school enrollment (optional) ----------
doe_path = DOCS / "doe_k12_by_tract.json"
doe_meta = None
if doe_path.exists():
    print("Merging NYC DOE K-12 public-school enrollment...")
    doej = json.load(open(doe_path))
    doe_meta = doej.get("meta")
    by_tract_doe = doej.get("by_tract", {})
    matched = 0
    for tract, d in derived.items():
        rec = by_tract_doe.get(tract)
        if rec:
            d["doe_public_k12_enrolled"] = rec.get("k12_enrolled") or 0
            d["doe_public_schools"]      = rec.get("schools") or 0
            matched += 1
        else:
            d["doe_public_k12_enrolled"] = 0
            d["doe_public_schools"]      = 0
    print(f"  attached counts to all {len(derived)} tracts (non-zero: {sum(1 for v in derived.values() if v.get('doe_public_k12_enrolled'))})")
else:
    print(f"No {doe_path.name} found — skipping DOE merge (run fetch_doe_schools.py first).")


# ---------- merge 2020 Decennial population (optional) ----------
dec_path = DOCS / "decennial_2020_by_tract.json"
if dec_path.exists():
    print("Merging 2020 Decennial population...")
    dec = json.load(open(dec_path))
    matched = 0
    for tract, d in derived.items():
        p = dec.get(tract)
        if p is not None:
            d["pop_2020"] = p
            matched += 1
    print(f"  matched {matched} tracts")
else:
    print(f"No {dec_path.name} found — skipping Decennial merge (set CENSUS_API_KEY and run fetch_decennial.py first).")


# ---------- merge crime counts (if available) ----------
crime_path = DOCS / "crime_by_tract.json"
crime_meta = None
if crime_path.exists():
    print("Merging tract-level crime counts...")
    cj = json.load(open(crime_path))
    crime_meta = cj.get("meta")
    counts = cj.get("counts", {})
    suppressed_low_pop = 0
    for tract, d in derived.items():
        c = counts.get(tract)
        if not c:
            d["crime_violent"] = None
            d["crime_property"] = None
            d["crime_total"] = None
            d["crime_violent_rate"] = None
            d["crime_property_rate"] = None
            d["crime_total_rate"] = None
            continue
        pop = d.get("pop_total")
        # Rates per 1,000 residents over the 12-month window.
        # Suppress for very small or commercial-heavy denominators. 200 residents is a
        # reasonable floor at the tract level — below that, rates whip around.
        suppress = (pop is None) or (pop < 200)
        d["crime_violent"] = c["violent"]
        d["crime_property"] = c["property"]
        d["crime_total"] = c["total"]
        if suppress:
            suppressed_low_pop += 1
            d["crime_violent_rate"] = None
            d["crime_property_rate"] = None
            d["crime_total_rate"] = None
        else:
            d["crime_violent_rate"]  = 1000.0 * c["violent"]  / pop
            d["crime_property_rate"] = 1000.0 * c["property"] / pop
            d["crime_total_rate"]    = 1000.0 * c["total"]    / pop
            # Commercial / tourist / industrial heuristic: any tract with a major-felony
            # rate above 100 per 1,000 residents is almost certainly daytime-population
            # driven. (Citywide ≈ 14 per 1,000; a high-crime residential tract is ~50-80.)
            # This catches Midtown, Times Square, Financial District, Penn Station, Hunts
            # Point industrial, and similar. We don't suppress — the rate is real — we
            # flag for the UI to display a caveat that the denominator is residents-only.
            if d["crime_total_rate"] > 100:
                d["crime_commercial_daytime"] = True
    print(f"  suppressed rates for {suppressed_low_pop} tracts with population < 50.")
else:
    print(f"No {crime_path.name} found — skipping crime merge. Run fetch_crime.py first.")


# ---------- join geometry ----------
print("Joining tract geometry (NYC DCP 2020 tracts, shoreline-clipped)...")
base = json.load(open(ROOT / "nyc2020_tracts.geojson"))

# Capture tract→NTA mapping BEFORE the loop below overwrites each feature's properties.
# Also build NTA code → ntatype, so tracts can be classified as non-residential via their
# parent NTA's official NYC DCP classifier (much more reliable than tract-level cdeligibil,
# which marks plenty of residential tracts as "I" for non-population reasons).
tract_to_nta = {}
nta_to_tracts = {}
for f in base["features"]:
    g = f["properties"]["geoid"]
    code = f["properties"].get("nta2020")
    if code:
        tract_to_nta[g] = code
        nta_to_tracts.setdefault(code, []).append(g)

nta_type_lookup = {}
_nta_src = json.load(open(ROOT / "ntas_2020.geojson"))
for f in _nta_src["features"]:
    p = f["properties"]
    nta_type_lookup[p.get("nta2020")] = p.get("ntatype")
del _nta_src

features = []
unmatched_geom = 0
for f in base["features"]:
    p = f["properties"]
    geoid = p["geoid"]
    d = derived.get(geoid)
    if not d:
        unmatched_geom += 1
        continue
    # shape_area is in sq ft (NAD83 NY State Plane Long Island ftUS).
    # 1 sq mi = 27,878,400 sq ft.
    try:
        sqmi = float(p.get("shape_area", 0)) / 27_878_400.0
    except (TypeError, ValueError):
        sqmi = 0
    pop = d.get("pop_total")
    d["pop_density"] = (pop / sqmi) if (pop and sqmi > 0) else None
    d["land_sqmi"] = round(sqmi, 4) if sqmi else None
    d["geoid"] = geoid
    d["borough"] = p.get("boroname")
    d["nta"] = p.get("ntaname")
    d["ct_label"] = p.get("ctlabel")
    # Non-residential flag: derived from the tract's parent NTA's official NYC DCP type.
    # ntatype "0" = residential; "5"=correctional, "6"=industrial/military, "7"=cemetery,
    # "8"=airport, "9"=park. Tracts inside non-residential NTAs get the flag and have
    # their crime rates suppressed (residents-based denominator is meaningless here).
    nta_code_ = p.get("nta2020")
    if nta_code_ and nta_type_lookup.get(nta_code_) not in (None, "0"):
        d["non_residential"] = True
        for k in ("crime_violent_rate", "crime_property_rate", "crime_total_rate"):
            d[k] = None
    f["properties"] = d
    features.append(f)

# Strip internal raw-count fields (prefixed with _) from final output to keep file size down.
for f in features:
    for k in list(f["properties"].keys()):
        if k.startswith("_"):
            del f["properties"][k]

print(f"Joined {len(features)} tracts (geometry without data: {unmatched_geom}).")


# ---------- NTA aggregation ----------
# Sum raw ACS cells (and MOEs via sum-of-squares) across all tracts in each NTA,
# then re-derive metrics on the aggregated cells. Same logic, much lower MOEs.
print("Aggregating to NTAs...")
nta_base = json.load(open(ROOT / "ntas_2020.geojson"))

print(f"  tract→NTA map: {len(tract_to_nta)} tracts into {len(nta_to_tracts)} NTAs")

# Helper: sum estimate cells across a list of tracts
def agg_cells(tract_geoids, source):
    out = {}
    for g in tract_geoids:
        cells = source.get(g, {})
        for k, v in cells.items():
            if v is None or not isinstance(v, (int, float)):
                continue
            out[k] = out.get(k, 0) + v
    return out

def agg_moe_cells(tract_geoids, source):
    """MOE of a sum across cells = sqrt(Σ MOE²) for each column."""
    out = {}
    for g in tract_geoids:
        cells = source.get(g, {})
        for k, v in cells.items():
            if v is None or not isinstance(v, (int, float)):
                continue
            out[k] = out.get(k, 0) + v * v
    return {k: _math.sqrt(v) for k, v in out.items()}

# Helper: population-weighted average of tract medians (best we can do without re-fetching).
def weighted_median_estimate(tract_geoids, median_key, weight_key="B01003001"):
    weighted_sum = 0.0
    weight_total = 0.0
    for g in tract_geoids:
        med = all_data.get(g, {}).get(median_key)
        wgt = all_data.get(g, {}).get(weight_key)
        if med is None or wgt is None or wgt <= 0:
            continue
        weighted_sum += med * wgt
        weight_total += wgt
    return (weighted_sum / weight_total) if weight_total else None


# Aggregate
nta_features = []
for nf in nta_base["features"]:
    code = nf["properties"]["nta2020"]
    member_tracts = nta_to_tracts.get(code, [])
    if not member_tracts:
        continue
    agg_d = agg_cells(member_tracts, all_data)
    agg_m = agg_moe_cells(member_tracts, all_moes)
    rec, moes = derive_one(agg_d, agg_m)

    # Replace median estimates with population-weighted tract-median averages (approximation,
    # since you can't aggregate medians from medians; we flag this in methodology).
    # Also clear their MOEs from the dict — the sum-of-squares of tract median MOEs is not a
    # defensible MOE for a population-weighted average. We will not show ± for aggregated medians.
    for median_key, raw in [
        ("median_age", "B01002001"),
        ("median_hh_income", "B19013001"),
        ("median_gross_rent", "B25064001"),
        ("median_home_value", "B25077001"),
        ("median_rent_burden", "B25071001"),
        ("avg_hh_size", "B25010001"),
    ]:
        rec[median_key] = weighted_median_estimate(member_tracts, raw)
        moes[median_key] = None  # suppress; aggregated medians don't have a clean MOE

    # Attach MOE companions
    for key, moe_val in moes.items():
        if moe_val is None: continue
        if moe_val >= 1000: rec[key + '_moe'] = round(moe_val)
        elif moe_val >= 10: rec[key + '_moe'] = round(moe_val, 1)
        else: rec[key + '_moe'] = round(moe_val, 2)

    # Sum DOE schools across member tracts
    schools = 0
    doe_k12 = 0
    pop_2020_sum = 0
    has_2020 = False
    for g in member_tracts:
        t_rec = derived.get(g, {})
        schools += t_rec.get("doe_public_schools") or 0
        doe_k12 += t_rec.get("doe_public_k12_enrolled") or 0
        p20 = t_rec.get("pop_2020")
        if p20 is not None:
            pop_2020_sum += p20
            has_2020 = True
    rec["doe_public_schools"] = schools
    rec["doe_public_k12_enrolled"] = doe_k12
    if has_2020:
        rec["pop_2020"] = pop_2020_sum

    # NHGIS DHC 2020 counts aggregation: sum raw cells, recompute percentages
    if nhgis_path.exists():
        dhc_total = 0
        sums = {k: 0 for k in ["_white_nh_2020_n","_black_nh_2020_n","_asian_nh_2020_n","_hispanic_2020_n","_under18_2020_n","_over65_2020_n"]}
        for g in member_tracts:
            t_rec = derived.get(g, {})
            dhc_total += t_rec.get("pop_2020_dhc") or 0
            for k in sums:
                sums[k] += t_rec.get(k) or 0
        if dhc_total > 0:
            rec["pop_2020_dhc"]       = dhc_total
            rec["pct_white_nh_2020"]  = 100.0 * sums["_white_nh_2020_n"]  / dhc_total
            rec["pct_black_nh_2020"]  = 100.0 * sums["_black_nh_2020_n"]  / dhc_total
            rec["pct_asian_nh_2020"]  = 100.0 * sums["_asian_nh_2020_n"]  / dhc_total
            rec["pct_hispanic_2020"]  = 100.0 * sums["_hispanic_2020_n"]  / dhc_total
            rec["pct_under18_2020"]   = 100.0 * sums["_under18_2020_n"]   / dhc_total
            rec["pct_over65_2020"]    = 100.0 * sums["_over65_2020_n"]    / dhc_total

    # Sum election votes across member tracts and recompute candidate vote shares
    if elec_path.exists():
        # Collect raw count fields from members and sum
        count_keys = set()
        for g in member_tracts:
            t_rec = derived.get(g, {})
            for k in t_rec:
                if (k.startswith("pres_") or k.startswith("mayor_")) and not k.endswith("_pct") and not k.endswith("_total") and not k.endswith("_shift_2020_2024"):
                    count_keys.add(k)
        sums = {k: 0 for k in count_keys}
        for g in member_tracts:
            t_rec = derived.get(g, {})
            for k in count_keys:
                v = t_rec.get(k)
                if isinstance(v, (int, float)):
                    sums[k] += v
        # Stash raw counts
        for k, v in sums.items():
            rec[k] = v
        # Recompute D/R shares per election
        for year in ("2020", "2024"):
            d = sums.get(f"pres_{year}_d") or 0
            r = sums.get(f"pres_{year}_r") or 0
            tot = d + r
            if tot >= 25:
                rec[f"pres_{year}_d_pct"] = 100.0 * d / tot
                rec[f"pres_{year}_r_pct"] = 100.0 * r / tot
                rec[f"pres_{year}_total"] = tot
        if rec.get("pres_2020_d_pct") is not None and rec.get("pres_2024_d_pct") is not None:
            rec["pres_d_shift_2020_2024"] = rec["pres_2024_d_pct"] - rec["pres_2020_d_pct"]
        # 2021 mayor
        a21 = sums.get("mayor_2021_adams") or 0
        s21 = sums.get("mayor_2021_sliwa") or 0
        t21 = a21 + s21
        if t21 >= 25:
            rec["mayor_2021_adams_pct"] = 100.0 * a21 / t21
            rec["mayor_2021_sliwa_pct"] = 100.0 * s21 / t21
            rec["mayor_2021_total"] = t21
        # 2025 mayor
        m25 = sums.get("mayor_2025_mamdani") or 0
        c25 = sums.get("mayor_2025_cuomo")   or 0
        s25 = sums.get("mayor_2025_sliwa")   or 0
        t25 = m25 + c25 + s25
        if t25 >= 25:
            rec["mayor_2025_mamdani_pct"] = 100.0 * m25 / t25
            rec["mayor_2025_cuomo_pct"]   = 100.0 * c25 / t25
            rec["mayor_2025_sliwa_pct"]   = 100.0 * s25 / t25
            rec["mayor_2025_total"] = t25

    # Sum crime across member tracts (numerators) and recompute rate using aggregated pop.
    # Suppress rates entirely for non-residential NTAs (parks, cemeteries, airports) — the
    # ratio of crime to *resident* population is meaningless when most users are non-residents.
    # NYC DCP NTA type codes: "0" = general / residential. Anything else = park / cemetery /
    # airport / waterway. ntype "9" is parks (e.g. BX2891 Pelham Bay Park).
    ntatype = nf["properties"].get("ntatype")
    rec["non_residential"] = ntatype != "0"
    if crime_path.exists():
        cv_count = cp_count = ct_count = 0
        for g in member_tracts:
            t_rec = derived.get(g, {})
            cv_count += t_rec.get("crime_violent")  or 0
            cp_count += t_rec.get("crime_property") or 0
            ct_count += t_rec.get("crime_total")    or 0
        rec["crime_violent"]  = cv_count
        rec["crime_property"] = cp_count
        rec["crime_total"]    = ct_count
        pop = rec.get("pop_total")
        # Suppress if non-residential OR resident population < 500 (NTA floor for stable rate)
        if pop and pop >= 500 and not rec["non_residential"]:
            rec["crime_violent_rate"]  = 1000.0 * cv_count / pop
            rec["crime_property_rate"] = 1000.0 * cp_count / pop
            rec["crime_total_rate"]    = 1000.0 * ct_count / pop
            if rec["crime_total_rate"] > 100:
                rec["crime_commercial_daytime"] = True

    # Geometry properties
    sqmi = 0
    try:
        sqmi = float(nf["properties"].get("shape_area", 0)) / 27_878_400.0
    except (TypeError, ValueError):
        pass
    rec["pop_density"] = (rec.get("pop_total") / sqmi) if (rec.get("pop_total") and sqmi > 0) else None
    rec["land_sqmi"] = round(sqmi, 4) if sqmi else None
    rec["geoid"] = code
    rec["borough"] = nf["properties"].get("boroname")
    rec["nta"] = nf["properties"].get("ntaname")
    rec["ct_label"] = None
    rec["nta_tract_count"] = len(member_tracts)

    nta_features.append({
        "type": "Feature",
        "geometry": nf["geometry"],
        "properties": rec,
    })

print(f"Wrote {len(nta_features)} NTAs.")
nta_out = {"type": "FeatureCollection", "features": nta_features}
json.dump(nta_out, open(DOCS / "ntas.geojson", "w"))
print(f"Wrote {DOCS/'ntas.geojson'}  ({(DOCS/'ntas.geojson').stat().st_size/1_000_000:.2f} MB)")

out_geo = {"type": "FeatureCollection", "features": features}
json.dump(out_geo, open(DOCS / "tracts.geojson", "w"))
print(f"Wrote {DOCS/'tracts.geojson'}  ({(DOCS/'tracts.geojson').stat().st_size/1_000_000:.2f} MB)")

# ---------- variable metadata ----------
# Variables list — ordered as a "neighborhood scouting" narrative:
#   who lives there → how they're grouped → money → housing → safety → education → work → leftovers.
VARS = [
    # --- 1. People ---
    ("People", "pop_total", "Total population (ACS, 2020–24 avg)", "int", "people",
     "Total residents in the tract — ACS 2020–24 5-year survey estimate. Has a margin of error; the 2020 Decennial count below is more precise but 5 years older."),
    ("People", "pop_2020", "Total population (2020 Decennial count)", "int", "people",
     "Total residents in the tract from the 2020 Decennial Census — a 100% count, not a survey estimate. No margin of error. Roughly 5 years stale by now."),
    ("People", "pop_density", "Population density", "num1", "people/sq mi",
     "ACS residents per square mile of land area."),
    ("People", "median_age", "Median age", "num1", "years",
     "Half of residents are older than this age, half younger."),
    ("People", "pct_under18", "Share under 18", "pct", "%",
     "Share of residents younger than 18."),
    ("People", "pct_over65", "Share 65 and over", "pct", "%",
     "Share of residents 65 years or older."),
    ("People", "pct_under18_2020", "Share under 18 (2020 count)", "pct", "%",
     "Share under 18 — 2020 Decennial 100% count, no margin of error."),
    ("People", "pct_over65_2020", "Share 65 and over (2020 count)", "pct", "%",
     "Share 65 and over — 2020 Decennial 100% count, no margin of error."),

    # --- 2. Race / ethnicity ---
    ("Race / ethnicity", "pct_white_nh", "White (non-Hispanic)", "pct", "%",
     "Share identifying as white alone and not Hispanic or Latino."),
    ("Race / ethnicity", "pct_black_nh", "Black (non-Hispanic)", "pct", "%",
     "Share identifying as Black or African American alone and not Hispanic or Latino."),
    ("Race / ethnicity", "pct_asian_nh", "Asian (non-Hispanic)", "pct", "%",
     "Share identifying as Asian alone and not Hispanic or Latino."),
    ("Race / ethnicity", "pct_hispanic", "Hispanic or Latino (any race)", "pct", "%",
     "Share identifying as Hispanic or Latino, of any race."),

    ("Race / ethnicity", "pct_white_nh_2020", "White non-Hispanic (2020 count)", "pct", "%",
     "Share identifying as White alone, non-Hispanic — 2020 Decennial Census 100% count, not a survey estimate. More precise than the ACS version above but 5 years older."),
    ("Race / ethnicity", "pct_black_nh_2020", "Black non-Hispanic (2020 count)", "pct", "%",
     "Share identifying as Black or African American alone, non-Hispanic — 2020 Decennial 100% count."),
    ("Race / ethnicity", "pct_asian_nh_2020", "Asian non-Hispanic (2020 count)", "pct", "%",
     "Share identifying as Asian alone, non-Hispanic — 2020 Decennial 100% count."),
    ("Race / ethnicity", "pct_hispanic_2020", "Hispanic or Latino (2020 count)", "pct", "%",
     "Share identifying as Hispanic or Latino, of any race — 2020 Decennial 100% count."),

    # --- 3. Origin & language ---
    ("Origin & language", "pct_foreign_born", "Foreign-born", "pct", "%",
     "Share of residents born outside the United States, not counting those born abroad to American parents (who Census classifies as native)."),
    ("Origin & language", "pct_non_citizen", "Not a U.S. citizen", "pct", "%",
     "Share of residents who are foreign-born and have not naturalized — the closest publicly available proxy for the undocumented population, but it OVER-counts that population. It includes lawful permanent residents (green-card holders), visa holders (students, H-1B, etc.), refugees, asylees, and TPS recipients. Nationally about a quarter of foreign-born residents are undocumented; in NYC, Center for Migration Studies estimates roughly a third of non-citizens are undocumented. See methodology for the full caveat."),
    ("Origin & language", "pct_non_english_home", "Non-English at home", "pct", "%",
     "Share of residents 5+ who speak a language other than English at home."),

    # --- 4. Households ---
    ("Households", "households", "Total households", "int", "households",
     "Count of occupied housing units."),
    ("Households", "avg_hh_size", "Average household size", "num2", "people",
     "Average number of people living in each household."),
    ("Households", "pct_owner_occupied", "Owner-occupied homes", "pct", "%",
     "Share of occupied units that are owner-occupied."),

    # --- 5. Income & poverty ---
    ("Income & poverty", "median_hh_income", "Median household income", "usd", "$",
     "Median household income in the past 12 months, in 2024 inflation-adjusted dollars (ACS B19013)."),
    ("Income & poverty", "pct_poverty", "Poverty rate", "pct", "%",
     "Share of residents below the federal poverty line, among those for whom poverty status was determined (excludes institutional group quarters)."),
    ("Income & poverty", "pct_hh_under30k", "Households under $30k", "pct", "%",
     "Share of households earning less than $30,000."),
    ("Income & poverty", "pct_hh_30_60k", "Households $30k–$60k", "pct", "%",
     "Share of households earning $30,000 to $59,999."),
    ("Income & poverty", "pct_hh_60_100k", "Households $60k–$100k", "pct", "%",
     "Share of households earning $60,000 to $99,999."),
    ("Income & poverty", "pct_hh_100_150k", "Households $100k–$150k", "pct", "%",
     "Share of households earning $100,000 to $149,999."),
    ("Income & poverty", "pct_hh_150kplus", "Households $150k+", "pct", "%",
     "Share of households earning $150,000 or more (the union of the next two brackets)."),
    ("Income & poverty", "pct_hh_150_200k", "Households $150k–$200k", "pct", "%",
     "Share of households earning $150,000 to $199,999."),
    ("Income & poverty", "pct_hh_200kplus", "Households $200k+", "pct", "%",
     "Share of households earning $200,000 or more. This is the highest bracket ACS publishes at tract level — Census top-codes everything above $200k here. For dollar-precise distinctions among the very wealthy ($500k+, $1M+), tract-level data is not publicly available; the IRS Statistics of Income series breaks out higher bands but only at the ZIP-code level."),
    ("Income & poverty", "pct_snap", "Receiving SNAP", "pct", "%",
     "Share of households that received SNAP / food-stamp benefits in the past year."),

    # --- 6. Housing ---
    ("Housing", "median_gross_rent", "Median gross rent", "usd", "$/mo",
     "Median monthly gross rent for renter-occupied units paying cash rent (excludes no-cash-rent units)."),
    ("Housing", "median_home_value", "Median home value", "usd", "$",
     "Median value of owner-occupied housing units, by self-report (ACS B25077)."),
    ("Housing", "median_rent_burden", "Median rent burden", "num1", "%",
     "Median gross rent as a percentage of household income in the past 12 months (ACS B25071)."),

    # --- 7. Crime ---
    ("Crime (rolling 12 mo.)", "crime_total_rate", "Major-felony rate", "num1", "per 1,000 residents",
     "All seven major felonies — murder, rape, robbery, felony assault, burglary, grand larceny, grand larceny of motor vehicle — per 1,000 residents over the most recent 12 months (NYPD)."),
    ("Crime (rolling 12 mo.)", "crime_violent_rate", "Violent-crime rate", "num1", "per 1,000 residents",
     "Murder, rape, robbery, and felony assault per 1,000 residents over the most recent 12 months (NYPD)."),
    ("Crime (rolling 12 mo.)", "crime_property_rate", "Property-crime rate", "num1", "per 1,000 residents",
     "Burglary, grand larceny, and grand larceny of motor vehicle per 1,000 residents over the most recent 12 months (NYPD)."),

    # --- 8. Education ---
    ("Education", "pct_bachelor_plus", "Bachelor's degree or higher", "pct", "%",
     "Share of residents 25+ holding at least a bachelor's degree."),
    ("Education", "pct_hs_only", "High-school diploma only", "pct", "%",
     "Share of residents 25+ whose highest credential is a high-school diploma or equivalent."),

    # --- 9. Schools (K-12) — pairs naturally with Education ---
    ("Schools (K-12)", "pct_kids_public_k12", "K-12 students in public school", "pct", "%",
     "Share of kindergarten-through-12th-grade students enrolled in public school."),
    ("Schools (K-12)", "pct_kids_private_k12", "K-12 students in private school", "pct", "%",
     "Share of kindergarten-through-12th-grade students enrolled in private school (Census combines parochial and independent private here)."),
    ("Schools (K-12)", "k12_students", "K-12 students (count, by residence)", "int", "students",
     "Total K-12 students living in the tract (ACS, by student residence)."),
    ("Schools (K-12)", "doe_public_k12_enrolled", "Public-school K-12 enrolled (by school location)", "int", "students",
     "K-12 students enrolled at public schools located in this tract, from NYC DOE Demographic Snapshot rosters. Counts by school location, not student residence — so the number is high in tracts that contain a large school and zero in tracts with no school."),
    ("Schools (K-12)", "doe_public_schools", "Public K-12 schools in tract", "int", "schools",
     "Number of NYC DOE public K-12 schools physically located in the tract (includes charters operating under DOE; excludes private/parochial)."),

    # --- 10. Work & commute ---
    ("Work & commute", "pct_in_labor_force", "In labor force (16+)", "pct", "%",
     "Share of residents 16+ in the civilian or armed-forces labor force."),
    ("Work & commute", "pct_unemployed", "Unemployment rate", "pct", "%",
     "Share of the civilian labor force (16+) that is unemployed — the standard BLS unemployment-rate definition."),
    ("Work & commute", "pct_public_transit", "Commute by public transit", "pct", "%",
     "Share of commuters using public transportation, excluding taxi."),
    ("Work & commute", "pct_walked", "Walked to work", "pct", "%",
     "Share of commuters who walked to work."),
    ("Work & commute", "pct_wfh", "Worked from home", "pct", "%",
     "Share of workers who worked primarily from home."),

    # --- 11. Vehicles — pairs naturally with commute ---
    ("Vehicles", "pct_no_vehicle", "Households with no vehicle", "pct", "%",
     "Share of households that have no vehicle available."),
    ("Vehicles", "pct_2plus_vehicles", "Households with 2+ vehicles", "pct", "%",
     "Share of households with two or more vehicles available."),
    ("Vehicles", "avg_vehicles_per_hh", "Average vehicles per household", "num2", "vehicles",
     "Average number of vehicles per household (the \"4 or more\" bucket is counted as 4.5)."),
    ("Vehicles", "pct_owner_no_vehicle", "Homeowners without a vehicle", "pct", "%",
     "Share of owner-occupied households that have no vehicle — a distinctively dense-city pattern."),
    ("Vehicles", "pct_renter_no_vehicle", "Renters without a vehicle", "pct", "%",
     "Share of renter-occupied households that have no vehicle."),

    # --- 12. Elections (NYC BoE) ---
    ("Elections", "pres_2024_d_pct", "Harris vote share, 2024 president", "pct", "%",
     "Kamala Harris share of the two-party (D+R) vote in the November 2024 general election. NYC BoE ED-level results, aggregated to tracts by ED centroid."),
    ("Elections", "pres_2024_r_pct", "Trump vote share, 2024 president", "pct", "%",
     "Donald Trump share of the two-party (D+R) vote in the November 2024 general election. NYC BoE ED-level results."),
    ("Elections", "pres_2024_total", "Major-party votes cast, 2024 president", "int", "votes",
     "Total D+R votes cast for president in 2024 in this tract."),
    ("Elections", "pres_2020_d_pct", "Biden vote share, 2020 president", "pct", "%",
     "Joe Biden share of the two-party (D+R) vote in the November 2020 general election. NYC BoE ED-level results."),
    ("Elections", "pres_2020_r_pct", "Trump vote share, 2020 president", "pct", "%",
     "Donald Trump share of the two-party (D+R) vote in the November 2020 general election."),
    ("Elections", "pres_2020_total", "Major-party votes cast, 2020 president", "int", "votes",
     "Total D+R votes cast for president in 2020 in this tract."),
    ("Elections", "pres_d_shift_2020_2024", "Democratic shift, 2020 → 2024 president", "num1", "percentage points",
     "Change in the Democratic share of the two-party presidential vote from 2020 to 2024. Negative = Republican gain; positive = Democratic gain."),
    ("Elections", "mayor_2025_mamdani_pct", "Mamdani vote share, 2025 mayor", "pct", "%",
     "Zohran Mamdani share of the three-way (Mamdani + Cuomo + Sliwa) vote in the November 2025 general mayoral election."),
    ("Elections", "mayor_2025_cuomo_pct", "Cuomo vote share, 2025 mayor", "pct", "%",
     "Andrew Cuomo share of the three-way mayoral vote in 2025 (running as an independent on his own line)."),
    ("Elections", "mayor_2025_sliwa_pct", "Sliwa vote share, 2025 mayor", "pct", "%",
     "Curtis Sliwa (Republican) share of the three-way mayoral vote in 2025."),
    ("Elections", "mayor_2025_total", "Major-candidate votes cast, 2025 mayor", "int", "votes",
     "Total Mamdani + Cuomo + Sliwa votes cast in this tract in 2025."),
    ("Elections", "mayor_2021_adams_pct", "Adams vote share, 2021 mayor", "pct", "%",
     "Eric Adams (Democrat) share of the two-major-candidate (Adams + Sliwa) vote in the November 2021 general mayoral election."),
    ("Elections", "mayor_2021_sliwa_pct", "Sliwa vote share, 2021 mayor", "pct", "%",
     "Curtis Sliwa (Republican) share of the two-major-candidate vote in 2021."),
    ("Elections", "mayor_2021_total", "Major-candidate votes cast, 2021 mayor", "int", "votes",
     "Total Adams + Sliwa votes cast in this tract in 2021."),

    # --- 13. Other (always last) ---
    ("Other", "pct_veteran", "Veterans (18+)", "pct", "%",
     "Share of civilians 18+ who are veterans."),
    ("Other", "pct_no_internet", "No internet access", "pct", "%",
     "Share of households with no internet access at all — neither a paid subscription nor any other means of getting online (ACS B28002_013)."),
]

vars_meta = [
    {"group": g, "key": k, "label": l, "fmt": f, "units": u, "desc": desc}
    for (g, k, l, f, u, desc) in VARS
]
json.dump(vars_meta, open(DOCS / "variables.json", "w"))
print(f"Wrote {DOCS/'variables.json'}  ({len(vars_meta)} variables)")

# release info — include crime window when present
release_blob = dict(release_meta or {})
if crime_meta:
    release_blob["crime_window"] = {
        "start": crime_meta["window_start"],
        "end": crime_meta["window_end"],
        "days": crime_meta["window_days"],
        "matched": crime_meta["matched_complaints"],
    }
json.dump(release_blob, open(DOCS / "release.json", "w"))
print(f"Wrote {DOCS/'release.json'}")
print("Done.")
