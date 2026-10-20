#!/usr/bin/env python3
"""
Save (rank, pp) from the top of the real osu!standard leaderboard to data/leaderboard.json.

The random 10k sample has only a handful of players above ~12,000 pp, so ranks up there can't be read off it.
The API's ranking pages cover the top 10,000 (200 pages of 50); this reads every page in the top 500 and a
spread of pages after that (~25 requests). build_model.py uses it for exact ranks at the top.

Reads OSU_CLIENT_ID / OSU_CLIENT_SECRET from the environment; exits quietly if they aren't set.
Usage:  python scripts/pull_leaderboard.py
"""
import json, os, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "leaderboard.json")
PAGES = list(range(1, 11)) + [12, 14, 17, 20, 25, 30, 40, 50, 60, 80, 100, 120, 150, 175, 200]


def main():
    cid, secret = os.environ.get("OSU_CLIENT_ID"), os.environ.get("OSU_CLIENT_SECRET")
    if not cid or not secret:
        print("OSU_CLIENT_ID / OSU_CLIENT_SECRET aren't set; skipping."); return
    body = urllib.parse.urlencode({"client_id": cid, "client_secret": secret, "grant_type": "client_credentials", "scope": "public"}).encode()
    req = urllib.request.Request("https://osu.ppy.sh/oauth/token", data=body,
                                 headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"})
    try:
        tok = json.load(urllib.request.urlopen(req, timeout=30))["access_token"]
    except urllib.error.HTTPError as e:
        sys.exit(f"Couldn't get an API token ({e.code}). Check the OSU_CLIENT_ID and OSU_CLIENT_SECRET secrets.")
    pairs = {}
    for page in PAGES:
        url = "https://osu.ppy.sh/api/v2/rankings/osu/performance?" + urllib.parse.urlencode({"cursor[page]": page})
        for attempt in range(4):
            try:
                r = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}", "Accept": "application/json"})
                data = json.load(urllib.request.urlopen(r, timeout=30)); break
            except (urllib.error.URLError, TimeoutError) as e:
                if getattr(e, "code", 0) == 404: data = None; break
                time.sleep(5 * (attempt + 1))
        else:
            data = None
        time.sleep(1.05)                                   # API guideline: ~60 requests a minute
        rows = (data or {}).get("ranking") or []
        got = 0
        for i, row in enumerate(rows):
            pp, rank = row.get("pp"), row.get("global_rank") or (page - 1) * 50 + i + 1
            if pp:
                pairs[int(rank)] = round(float(pp), 1); got += 1
        print(f"page {page}: {got} players")
    if len(pairs) < 200:
        sys.exit(f"Only got {len(pairs)} players from the rankings; not saving.")
    out = {"pulledAt": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
           "pairs": [[r, pairs[r]] for r in sorted(pairs)]}
    with open(OUT, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"saved {len(pairs)} players, #1 {pairs[min(pairs)]} pp, #{max(pairs)} {pairs[max(pairs)]} pp")


if __name__ == "__main__":
    main()
