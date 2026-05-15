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
    """Return dict[tract_geoid] -> dict[column_id] -> estimate."""
    out = {}
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
            # geo is like "14000US36005000100" — keep the last 11 chars
            tract = geo[-11:]
            row = out.setdefault(tract, {})
            for tbl, payload in tables.items():
                est = payload.get("estimate", {})
                for col, val in est.items():
                    row[col] = val
    return out, release


print("Fetching from Census Reporter (no API key required)...")
all_data = {}
release_meta = None
for cfips, name in NYC_COUNTIES.items():
    print(f"  {cfips} {name} ...")
    rows, rel = fetch_county(cfips)
    all_data.update(rows)
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


# ---------- derive metrics ----------
derived = {}
for tract, d in all_data.items():
    pop = get(d, "B01003001")
    total_race = get(d, "B03002001")
    hh = get(d, "B11016001")
    inc_hh = get(d, "B19001001")
    edu = get(d, "B15003001")
    fb_total = get(d, "B05002001")
    lang_total = get(d, "C16001001")
    tenure = get(d, "B25003001")
    pov_total = get(d, "B17001001")
    commute = get(d, "B08301001")
    lf_total = get(d, "B23025001")
    lf_in = get(d, "B23025002")
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
        "pct_unemployed": pct(get(d, "B23025005"), lf_in),
        "pct_public_transit": pct(get(d, "B08301010"), commute),
        "pct_walked": pct(get(d, "B08301019"), commute),
        "pct_wfh": pct(get(d, "B08301021"), commute),

        "pct_veteran": pct(get(d, "B21001002"), vet_total),
        "pct_no_internet": pct(get(d, "B28002013"), internet_total),
    }


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
VARS = [
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

    ("Race / ethnicity", "pct_white_nh", "White (non-Hispanic)", "pct", "%",
     "Share identifying as white alone and not Hispanic or Latino."),
    ("Race / ethnicity", "pct_black_nh", "Black (non-Hispanic)", "pct", "%",
     "Share identifying as Black or African American alone and not Hispanic or Latino."),
    ("Race / ethnicity", "pct_asian_nh", "Asian (non-Hispanic)", "pct", "%",
     "Share identifying as Asian alone and not Hispanic or Latino."),
    ("Race / ethnicity", "pct_hispanic", "Hispanic or Latino (any race)", "pct", "%",
     "Share identifying as Hispanic or Latino, of any race."),

    ("Income & poverty", "median_hh_income", "Median household income", "usd", "$",
     "Median annual income across all households."),
    ("Income & poverty", "pct_poverty", "Poverty rate", "pct", "%",
     "Share of residents below the federal poverty line."),
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

    ("Households", "households", "Total households", "int", "households",
     "Count of occupied housing units."),
    ("Households", "avg_hh_size", "Average household size", "num2", "people",
     "Average number of people living in each household."),
    ("Households", "pct_owner_occupied", "Owner-occupied homes", "pct", "%",
     "Share of occupied units that are owner-occupied."),

    ("Housing", "median_gross_rent", "Median gross rent", "usd", "$/mo",
     "Median monthly gross rent paid by renters."),
    ("Housing", "median_home_value", "Median home value", "usd", "$",
     "Median value of owner-occupied homes."),
    ("Housing", "median_rent_burden", "Median rent burden", "num1", "%",
     "Median gross rent as a percentage of household income."),

    ("Education", "pct_bachelor_plus", "Bachelor's degree or higher", "pct", "%",
     "Share of residents 25+ holding at least a bachelor's degree."),
    ("Education", "pct_hs_only", "High-school diploma only", "pct", "%",
     "Share of residents 25+ whose highest credential is a high-school diploma or equivalent."),

    ("Origin & language", "pct_foreign_born", "Foreign-born", "pct", "%",
     "Share of residents born outside the United States."),
    ("Origin & language", "pct_non_english_home", "Non-English at home", "pct", "%",
     "Share of residents 5+ who speak a language other than English at home."),

    ("Work & commute", "pct_in_labor_force", "In labor force (16+)", "pct", "%",
     "Share of residents 16+ in the civilian or armed-forces labor force."),
    ("Work & commute", "pct_unemployed", "Unemployment rate", "pct", "%",
     "Share of the civilian labor force that is unemployed."),
    ("Work & commute", "pct_public_transit", "Commute by public transit", "pct", "%",
     "Share of commuters using public transportation, excluding taxi."),
    ("Work & commute", "pct_walked", "Walked to work", "pct", "%",
     "Share of commuters who walked to work."),
    ("Work & commute", "pct_wfh", "Worked from home", "pct", "%",
     "Share of workers who worked primarily from home."),

    ("Other", "pct_veteran", "Veterans (18+)", "pct", "%",
     "Share of civilians 18+ who are veterans."),
    ("Other", "pct_no_internet", "No internet access", "pct", "%",
     "Share of households without any internet subscription."),

    ("Crime (rolling 12 mo.)", "crime_total_rate", "Major-felony rate", "num1", "per 1,000 residents",
     "All seven major felonies — murder, rape, robbery, felony assault, burglary, grand larceny, grand larceny of motor vehicle — per 1,000 residents over the most recent 12 months (NYPD)."),
    ("Crime (rolling 12 mo.)", "crime_violent_rate", "Violent-crime rate", "num1", "per 1,000 residents",
     "Murder, rape, robbery, and felony assault per 1,000 residents over the most recent 12 months (NYPD)."),
    ("Crime (rolling 12 mo.)", "crime_property_rate", "Property-crime rate", "num1", "per 1,000 residents",
     "Burglary, grand larceny, and grand larceny of motor vehicle per 1,000 residents over the most recent 12 months (NYPD)."),
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
