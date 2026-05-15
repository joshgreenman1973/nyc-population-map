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


# ---------- derive metrics ----------
derived = {}
RELIABILITY_THRESHOLD = 0.30  # MOE > 30% of estimate → flag as unreliable
for tract, d in all_data.items():
    m = all_moes.get(tract, {})
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
    inc_150p = sum_safe(d, ["B19001016", "B19001017"])
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
    veh_3plus = sum_safe(d, ["B08201005", "B08201006"])
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

    derived[tract] = {
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
        "pct_hh_150kplus": pct(inc_150p, inc_hh),
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
        "pct_3plus_vehicles":    pct(veh_3plus, veh_total),
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
        # SNAP, tenure
        "pct_snap":             moe_pct(get(d,"B22010002"), get(m,"B22010002"), snap_total, get(m,"B22010001")),
        "pct_owner_occupied":   moe_pct(get(d,"B25003002"), get(m,"B25003002"), tenure, get(m,"B25003001")),
        # Education
        "pct_bachelor_plus":    moe_pct(bach_plus, bach_plus_moe, edu, get(m,"B15003001")),
        "pct_hs_only":          moe_pct(hs_only,   hs_only_moe,   edu, get(m,"B15003001")),
        # Origin / language
        "pct_foreign_born":     moe_pct(get(d,"B05002013"), get(m,"B05002013"), fb_total, get(m,"B05002001")),
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
        "pct_3plus_vehicles":    moe_pct(veh_3plus, moe_sum([get(m,"B08201005"),get(m,"B08201006")]), veh_total, get(m,"B08201001")),
        "pct_owner_no_vehicle":  moe_pct(own_no_v,  get(m,"B25044003"), own_total,  get(m,"B25044002")),
        "pct_renter_no_vehicle": moe_pct(rent_no_v, get(m,"B25044010"), rent_total, get(m,"B25044009")),
        # Schools
        "pct_kids_public_k12":   moe_pct(k12_public,  k12_pub_moe, k12_total, k12_tot_moe),
        "pct_kids_private_k12":  moe_pct(k12_private, k12_pri_moe, k12_total, k12_tot_moe),
    }

    # Attach MOE companion fields. Reliability tiers (Census Bureau guidance):
    #   moe_ratio = moe / |estimate|
    #   ratio ≤ 0.20  → reliable;  0.20–0.66 → use with caution;  > 0.66 → unreliable
    # UI computes moe_ratio on the fly from moe + estimate to save geojson size.
    rec = derived[tract]
    for key, moe_val in moes.items():
        if moe_val is None:
            continue
        # Round to a few significant figures based on magnitude to keep file size down
        if moe_val >= 1000:
            rec[key + "_moe"] = round(moe_val)
        elif moe_val >= 10:
            rec[key + "_moe"] = round(moe_val, 1)
        else:
            rec[key + "_moe"] = round(moe_val, 2)


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
        # Suppress rates for very small denominators (pop < 50) — divides blow up.
        suppress = (pop is None) or (pop < 50)
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
    print(f"  suppressed rates for {suppressed_low_pop} tracts with population < 50.")
else:
    print(f"No {crime_path.name} found — skipping crime merge. Run fetch_crime.py first.")


# ---------- join geometry ----------
print("Joining tract geometry (NYC DCP 2020 tracts, shoreline-clipped)...")
base = json.load(open(ROOT / "nyc2020_tracts.geojson"))

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
    f["properties"] = d
    features.append(f)

print(f"Joined {len(features)} tracts (geometry without data: {unmatched_geom}).")

out_geo = {"type": "FeatureCollection", "features": features}
json.dump(out_geo, open(DOCS / "tracts.geojson", "w"))
print(f"Wrote {DOCS/'tracts.geojson'}  ({(DOCS/'tracts.geojson').stat().st_size/1_000_000:.2f} MB)")

# ---------- variable metadata ----------
# Variables list — ordered as a "neighborhood scouting" narrative:
#   who lives there → how they're grouped → money → housing → safety → education → work → leftovers.
VARS = [
    # --- 1. People ---
    ("People", "pop_total", "Total population", "int", "people",
     "Total residents living in the tract."),
    ("People", "pop_density", "Population density", "num1", "people/sq mi",
     "Residents per square mile of land area."),
    ("People", "median_age", "Median age", "num1", "years",
     "Half of residents are older than this age, half younger."),
    ("People", "pct_under18", "Share under 18", "pct", "%",
     "Share of residents younger than 18."),
    ("People", "pct_over65", "Share 65 and over", "pct", "%",
     "Share of residents 65 years or older."),

    # --- 2. Race / ethnicity ---
    ("Race / ethnicity", "pct_white_nh", "White (non-Hispanic)", "pct", "%",
     "Share identifying as white alone and not Hispanic or Latino."),
    ("Race / ethnicity", "pct_black_nh", "Black (non-Hispanic)", "pct", "%",
     "Share identifying as Black or African American alone and not Hispanic or Latino."),
    ("Race / ethnicity", "pct_asian_nh", "Asian (non-Hispanic)", "pct", "%",
     "Share identifying as Asian alone and not Hispanic or Latino."),
    ("Race / ethnicity", "pct_hispanic", "Hispanic or Latino (any race)", "pct", "%",
     "Share identifying as Hispanic or Latino, of any race."),

    # --- 3. Origin & language ---
    ("Origin & language", "pct_foreign_born", "Foreign-born", "pct", "%",
     "Share of residents born outside the United States, not counting those born abroad to American parents (who Census classifies as native)."),
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
     "Share of households earning $150,000 or more."),
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
    ("Vehicles", "pct_3plus_vehicles", "Households with 3+ vehicles", "pct", "%",
     "Share of households with three or more vehicles available."),
    ("Vehicles", "avg_vehicles_per_hh", "Average vehicles per household", "num2", "vehicles",
     "Average number of vehicles per household (the \"4 or more\" bucket is counted as 4.5)."),
    ("Vehicles", "pct_owner_no_vehicle", "Homeowners without a vehicle", "pct", "%",
     "Share of owner-occupied households that have no vehicle — a distinctively dense-city pattern."),
    ("Vehicles", "pct_renter_no_vehicle", "Renters without a vehicle", "pct", "%",
     "Share of renter-occupied households that have no vehicle."),

    # --- 12. Other (always last) ---
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
