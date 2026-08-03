#!/usr/bin/env python3
"""Self-gating crime refresh for the NYC population map.

Runs on a schedule (launchd). Cheap pre-check: ask the NYPD datasets for the
latest report date. If it hasn't advanced past the window already baked into
docs/release.json, exit immediately — no rebuild, no commit, no notification.

When new data IS available it runs the full pipeline:
    fetch_crime.py  ->  build.py  ->  git commit  ->  git push (joshgreenman1973)
and sends an iMessage summary. NYPD posts the current-YTD complaint file
quarterly, so in practice this only does real work a few times a year.
"""
import json
import subprocess
import sys
import urllib.request
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).parent
DOCS = ROOT / "docs"
LOG = ROOT / "auto_refresh.log"
PHONE = "9175823254"

HISTORIC = "qgea-i56i"
CURRENT = "5uac-w243"


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def send_imessage(text):
    """Best-effort iMessage to Josh. Logs failure rather than raising."""
    script = (
        'tell application "Messages"\n'
        '  set svc to 1st service whose service type = iMessage\n'
        f'  send "{text}" to participant "{PHONE}" of svc\n'
        'end tell'
    )
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            log(f"iMessage send failed: {r.stderr.strip()[:200]}")
            return False
        return True
    except Exception as e:
        log(f"iMessage exception: {e}")
        return False


def max_rpt_dt(resource):
    url = (f"https://data.cityofnewyork.us/resource/{resource}.json"
           f"?$select=max(rpt_dt)&$limit=1")
    req = urllib.request.Request(url, headers={"User-Agent": "nyc-population-map/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read())
    return datetime.strptime(data[0]["max_rpt_dt"][:10], "%Y-%m-%d").date()


def current_window_end():
    """The window_end already published in release.json (the baked-in data)."""
    try:
        rel = json.load(open(DOCS / "release.json"))
        return datetime.strptime(rel["crime_window"]["end"], "%Y-%m-%d").date()
    except Exception as e:
        log(f"Could not read release.json window end: {e}")
        return date(2000, 1, 1)


def run(cmd):
    log("$ " + " ".join(cmd))
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=1800)
    if r.stdout.strip():
        log(r.stdout.strip()[-2000:])
    if r.returncode != 0:
        log(f"FAILED ({r.returncode}): {r.stderr.strip()[-2000:]}")
        raise RuntimeError(f"{cmd[0]} failed")
    return r


def git_sync():
    """Bring local main up to date with origin. The GitHub Actions twin of
    this job may have shipped a refresh already; without this, a stale clone
    re-does its work and the push gets rejected. Aborts a failed rebase so
    the repo is never left mid-rebase."""
    run(["git", "fetch", "origin"])
    try:
        run(["git", "rebase", "--autostash", "origin/main"])
    except Exception:
        subprocess.run(["git", "rebase", "--abort"], cwd=ROOT, capture_output=True)
        raise


def main():
    log("=== auto_refresh start ===")

    # --- sync first so the gate reads what's actually shipped, not a stale clone ---
    try:
        git_sync()
    except Exception as e:
        log(f"Git sync error: {e}")
        send_imessage(f"NYC population map: could not sync with GitHub before "
                      f"refresh ({e}). See auto_refresh.log.")
        return 1

    # --- cheap gate: has NYPD data advanced past what we already shipped? ---
    try:
        latest = max(max_rpt_dt(CURRENT), max_rpt_dt(HISTORIC))
    except Exception as e:
        log(f"Could not reach NYPD Socrata: {e} — will retry next run.")
        return 0

    have = current_window_end()
    log(f"NYPD latest rpt_dt = {latest};  shipped window end = {have}")
    if latest <= have:
        log("No newer crime data than what's already live. Exiting (no-op).")
        return 0

    log(f"New data available (advances window by {(latest - have).days} days). Rebuilding...")

    py = sys.executable
    try:
        run([py, "fetch_crime.py"])
        run([py, "build.py"])
    except Exception as e:
        log(f"Pipeline error: {e}")
        send_imessage(f"NYC population map: crime refresh FAILED during build "
                      f"({e}). See auto_refresh.log.")
        return 1

    # --- deploy ---
    new_end = current_window_end()
    try:
        run(["git", "add", "docs/crime_by_tract.json", "docs/tracts.geojson",
             "docs/ntas.geojson", "docs/variables.json", "docs/release.json"])
        # Nothing staged => nothing changed; bail gracefully.
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT)
        if diff.returncode == 0:
            log("Build produced no changes — skipping commit.")
            return 0
        run(["git", "commit", "-m",
             f"Auto-refresh crime: rolling window now ends {new_end}"])
        try:
            run(["git", "push", "origin", "HEAD"])
        except Exception:
            # Lost a race with a concurrent run — rebase onto it, retry once.
            git_sync()
            run(["git", "push", "origin", "HEAD"])
    except Exception as e:
        log(f"Deploy error: {e}")
        send_imessage(f"NYC population map: crime data rebuilt but PUSH FAILED "
                      f"({e}). See auto_refresh.log.")
        return 1

    url = "https://joshgreenman1973.github.io/nyc-population-map/"
    msg = (f"NYC population map auto-updated: crime window now ends {new_end} "
           f"(was {have}). Live in ~1 min: {url}")
    log(msg)
    send_imessage(msg)
    log("=== auto_refresh done ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
