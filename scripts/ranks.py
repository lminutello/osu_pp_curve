#!/usr/bin/env python3
"""
Ranks for the site: a lookup of real (pp, global rank) pairs, refreshed weekly.

Global rank is a fixed, rising function of pp on any given day, so the most accurate way to estimate it is to look
it up from real players' current pp and rank. This pulls ~1,000 players spread evenly over every pp level (their ids
are in data/rank_ids.json, picked from the dump sample by build_model.py), smooths ln(rank) against ln(pp), and
writes the table into data/model.json. Ranks past ~9,500 pp come from the real top 10,000 (data/leaderboard.json).
Held-out check (Oct 2026): ranks land within ~0.3% of real at every pp level.

Usage:  python scripts/ranks.py            # pull fresh pairs (needs OSU_CLIENT_ID / OSU_CLIENT_SECRET), update model
        python scripts/ranks.py --no-pull  # just rebuild the table from data/rank_pairs.json
"""
import argparse, gzip, json, math, os, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IDS = os.path.join(ROOT, "data", "rank_ids.json")
PAIRS = os.path.join(ROOT, "data", "rank_pairs.json")
MODEL = os.path.join(ROOT, "data", "model.json")


def rank_table(pairs, step=0.02):
    """[[ln pp, ln rank], ...] on a grid: local-linear smoothing in ln pp, then forced to only go down."""
    P = np.array([(p, r) for p, r in pairs if p and r and p > 0 and r > 0], float)
    x, y = np.log(P[:, 0]), np.log(P[:, 1])
    grid = np.arange(x.min(), x.max() + step, step); out = []
    for c in grid:
        b = 0.04
        while True:
            w = np.exp(-0.5 * ((x - c) / b) ** 2)
            if w.sum() >= 15 or b > 1: break
            b *= 1.3
        X = np.stack([np.ones_like(x), x - c], 1); sw = np.sqrt(w)
        out.append(float(np.linalg.lstsq(X * sw[:, None], y * sw, rcond=None)[0][0]))
    out = np.minimum.accumulate(np.array(out))
    return [[round(float(g), 4), round(float(v), 5)] for g, v in zip(grid, out)]


def token():
    cid, secret = os.environ.get("OSU_CLIENT_ID"), os.environ.get("OSU_CLIENT_SECRET")
    if not cid or not secret:
        return None
    body = urllib.parse.urlencode({"client_id": cid, "client_secret": secret, "grant_type": "client_credentials", "scope": "public"}).encode()
    r = urllib.request.Request("https://osu.ppy.sh/oauth/token", data=body,
                               headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"})
    return json.load(urllib.request.urlopen(r, timeout=30))["access_token"]


def pull(ids, tok):
    pairs, last = [], 0.0
    for i, u in enumerate(ids):
        for attempt in range(4):
            wait = 1.05 - (time.time() - last)
            if wait > 0: time.sleep(wait)
            last = time.time()
            try:
                r = urllib.request.Request(f"https://osu.ppy.sh/api/v2/users/{u}/osu?key=id",
                                           headers={"Authorization": f"Bearer {tok}", "Accept": "application/json"})
                st = (json.load(urllib.request.urlopen(r, timeout=30)).get("statistics") or {})
                if st.get("pp") and st.get("global_rank"):
                    pairs.append([round(st["pp"], 2), int(st["global_rank"])])
                break
            except urllib.error.HTTPError as e:
                if e.code == 404: break
                time.sleep(10 * (attempt + 1))
            except (urllib.error.URLError, TimeoutError):
                time.sleep(5 * (attempt + 1))
        if (i + 1) % 200 == 0:
            print(f"  {i + 1}/{len(ids)}", flush=True)
    return pairs


def update_model(model_path=MODEL):
    """Write the rank lookup (and leaderboard size) into model.json from data/rank_pairs.json."""
    if not os.path.exists(PAIRS):
        return False
    with open(PAIRS) as f:
        rp = json.load(f)
    if len(rp["pairs"]) < 300:
        return False
    with open(model_path) as f:
        m = json.load(f)
    tab = rank_table(rp["pairs"])
    # held-out check: build the table from 4/5 of the players, look up the rest
    P = np.array(rp["pairs"], float); idx = np.random.default_rng(0).permutation(len(P)); cut = len(P) // 5
    T = np.array(rank_table(P[idx[cut:]].tolist())); te = P[idx[:cut]]
    err = np.exp(np.abs(np.interp(np.log(te[:, 0]), T[:, 0], T[:, 1]) - np.log(te[:, 1]))) - 1
    rk = m.setdefault("rank", {})
    rk["table"] = tab
    rk["N"] = int(max(r for _, r in rp["pairs"]))          # the lowest-pp players sit at the bottom of the board
    rk["source"] = f"{len(rp['pairs']):,} real players' current ranks ({rp['pulledAt']})"
    lb = os.path.join(ROOT, "data", "leaderboard.json")
    if os.path.exists(lb):
        with open(lb) as f:
            L = json.load(f)
        rk["top"] = [[round(math.log(pp), 5), r] for r, pp in sorted(L["pairs"], key=lambda x: x[1]) if pp > 0]
        rk["source"] += f" and the real top {L['pairs'][-1][0]:,} ({L['pulledAt']})"
    rk["pairsAt"] = rp["pulledAt"]
    rk["check"] = {"median": round(float(np.median(err)) * 100, 2), "p90": round(float(np.percentile(err, 90)) * 100, 2), "n": int(cut)}
    print(f"rank self-check on {cut} held-out players: median error {rk['check']['median']}%, 90% within {rk['check']['p90']}%")
    with open(model_path, "w") as f:
        json.dump(m, f, separators=(",", ":"))
    print(f"rank table: {len(tab)} points from {len(rp['pairs'])} players; leaderboard ~{rk['N']:,}")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-pull", action="store_true")
    args = ap.parse_args()
    if not args.no_pull:
        tok = token()
        if not tok:
            print("OSU_CLIENT_ID / OSU_CLIENT_SECRET aren't set; skipping the pull.")
        elif not os.path.exists(IDS):
            print("No data/rank_ids.json yet (the monthly build makes it); skipping the pull.")
        else:
            with open(IDS) as f:
                ids = json.load(f)
            print(f"pulling {len(ids)} players' current ranks", flush=True)
            pairs = pull(ids, tok)
            if len(pairs) >= 300:
                with open(PAIRS, "w") as f:
                    json.dump({"pulledAt": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "pairs": pairs}, f, separators=(",", ":"))
            else:
                sys.exit(f"Only {len(pairs)} players came back; keeping the previous ranks.")
    update_model()


if __name__ == "__main__":
    main()
