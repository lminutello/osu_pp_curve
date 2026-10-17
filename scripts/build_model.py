#!/usr/bin/env python3
"""
Build data/model.json for the site from the official osu! data dumps (data.ppy.sh).

  1. finds the newest N "performance_osu_random_10000" dumps (default 3; each month is a fresh random draw)
  2. reads only the user stats table from each, keeps one row per player (newest dump wins)
  3. fits the pp-vs-hours curve, the spread around it, and the reverse fit (hours given pp)
  4. builds the pp distribution used for ranks, and calibrates leaderboard size from real ranks
     (the dump's own rank column if it has one, otherwise the (pp, rank) pairs in data/history_fit.json)
  5. merges in data/history_fit.json (drift, star level, real pp/hour) and writes data/model.json

Usage:
    python scripts/build_model.py                       # newest 3 dumps, downloads into .cache/
    python scripts/build_model.py --dumps 2026_07_13 2026_08_01 2026_09_01
    python scripts/build_model.py --users-csv out/users.csv   # skip dumps, use a users.csv from the old script
    python scripts/build_model.py --write-users .cache/users.csv   # also save the merged sample (for the history pull)

Needs: numpy. Only the standard library otherwise.
"""
import argparse, csv, json, math, os, re, sys, tarfile, urllib.request
from datetime import datetime, timezone

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = "https://data.ppy.sh/"
URL = "https://data.ppy.sh/{d}_performance_osu_random_10000.tar.bz2"
UA = {"User-Agent": "osu-playtime-curve (github.com/lminutello/osu_pp_curve)"}


# ------------------------------------------------------------------ dumps ---
def latest_dumps(n):
    req = urllib.request.Request(INDEX, headers=UA)
    html = urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "replace")
    dates = sorted(set(re.findall(r"(\d{4}_\d{2}_\d{2})_performance_osu_random_10000\.tar\.bz2", html)), reverse=True)
    if not dates:
        sys.exit("Couldn't find any osu! random_10000 dumps on data.ppy.sh")
    return dates[:n]


def fetch(dump, cache):
    os.makedirs(cache, exist_ok=True)
    path = os.path.join(cache, f"{dump}_performance_osu_random_10000.tar.bz2")
    if not os.path.exists(path):
        print(f"  downloading {dump} ...", flush=True)
        tmp = path + ".part"
        with urllib.request.urlopen(urllib.request.Request(URL.format(d=dump), headers=UA), timeout=600) as r, open(tmp, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        os.replace(tmp, path)
    return path


def parse_values(body):
    """Yield rows (lists of strings) from a MySQL 'VALUES (...),(...)' body, handling quoted strings."""
    i, n = 0, len(body)
    while i < n:
        if body[i] != "(":
            i += 1; continue
        i += 1; row, cur, q = [], [], False
        while i < n:
            c = body[i]
            if q:
                if c == "\\" and i + 1 < n: cur.append(body[i + 1]); i += 2; continue
                if c == "'": q = False
                else: cur.append(c)
            elif c == "'": q = True
            elif c == ",": row.append("".join(cur)); cur = []
            elif c == ")": row.append("".join(cur)); i += 1; break
            else: cur.append(c)
            i += 1
        yield row


def read_stats(path):
    """Players from the stats table of one dump: {user_id: dict}."""
    with tarfile.open(path, "r|bz2") as tar:          # streaming: stops once the stats file is read
        for m in tar:
            if re.search(r"osu_user_stats(_osu)?\.sql$", m.name):
                sql = tar.extractfile(m).read().decode("utf-8", "replace"); break
        else:
            sys.exit(f"No user stats table in {path}")
    head = re.search(r"CREATE TABLE `?\w+`?\s*\((.*?)\)\s*ENGINE", sql, re.S)
    cols = re.findall(r"^\s*`(\w+)`", head.group(1), re.M)
    ix = {c: i for i, c in enumerate(cols)}
    for need in ("user_id", "rank_score", "total_seconds_played"):
        if need not in ix:
            sys.exit(f"Dump format changed: no '{need}' column. Columns: {cols}")
    rank_col = next((c for c in ("rank_score_index", "global_rank") if c in ix), None)
    out = {}
    for stmt in re.finditer(r"INSERT INTO `?\w+`?(?:\s*\([^)]*\))?\s+VALUES\s*(.*?);\s*$", sql, re.S | re.M):
        for r in parse_values(stmt.group(1)):
            if len(r) != len(cols):
                continue
            try:
                pp, sec = float(r[ix["rank_score"]]), float(r[ix["total_seconds_played"]])
                rank = int(float(r[ix[rank_col]])) if rank_col and r[ix[rank_col]] not in ("", "NULL") else 0
            except ValueError:
                continue
            out[r[ix["user_id"]]] = {"pp": pp, "hours": sec / 3600, "rank": rank}
    return out, rank_col


def from_users_csv(path):
    out = {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            try:
                out[r["user_id"]] = {"pp": float(r["pp"]), "hours": float(r["hours"]), "rank": int(float(r.get("rank") or 0))}
            except (KeyError, ValueError):
                pass
    return out


# ----------------------------------------------------------------- fitting ---
def quad_fit(x, y):
    return np.polyfit(x, y, 2)[::-1]                   # [a, b, c] for a + b x + c x^2


def robust_sd_table(x, resid, bins=14):
    edges = np.quantile(x, np.linspace(0, 1, bins + 1)); out = []
    for a, b in zip(edges[:-1], edges[1:]):
        m = (x >= a) & (x <= b)
        q75, q25 = np.percentile(resid[m], [75, 25])
        out.append([round(float(np.median(x[m])), 4), round(float((q75 - q25) / 1.349), 4)])
    return out


def frac_above_fn(sorted_log_pp):
    L, n = sorted_log_pp, len(sorted_log_pp)
    def fa(pp):
        l = math.log(pp)
        if l >= L[-1]: return 0.5 / n
        if l <= L[0]: return 1 - 0.5 / n
        i = int(np.searchsorted(L, l)) - 1
        t = (l - L[i]) / (L[i + 1] - L[i]) if L[i + 1] > L[i] else 0.0
        return 1 - (i + t + 0.5) / n
    return fa


def calibrate_ranks(pairs, fa):
    """Leaderboard size N and a high-pp correction (ln pp -> ln factor) from real (pp, rank) pairs."""
    pp = np.array([p for p, _ in pairs]); R = np.array([r for _, r in pairs], float)
    f = np.array([fa(p) for p in pp]); N = float(np.median(R / f))
    lr = np.log(R / (N * f)); lp = np.log(pp); knots = []
    for c in np.quantile(lp, [0.05, 0.2, 0.4, 0.55, 0.7, 0.8, 0.87, 0.92, 0.95, 0.97, 0.985, 0.995]):
        m = np.abs(lp - c) < 0.18
        if m.sum() >= 10:
            knots.append([round(float(c), 3), round(float(np.median(lr[m])), 3)])
    after = np.log(R / (N * f * np.exp(np.interp(lp, [k[0] for k in knots], [k[1] for k in knots])))) if knots else lr
    return round(N), knots, float(np.median(np.abs(after)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dumps", nargs="*")
    ap.add_argument("--count", type=int, default=3, help="how many of the newest dumps to merge")
    ap.add_argument("--cache", default=os.path.join(ROOT, ".cache"))
    ap.add_argument("--users-csv", help="use this users.csv instead of reading dumps")
    ap.add_argument("--write-users", help="also save the merged sample as a users.csv")
    ap.add_argument("--sources", nargs="*", help="dump names to record when using --users-csv")
    ap.add_argument("--history-fit", default=os.path.join(ROOT, "data", "history_fit.json"))
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "model.json"))
    args = ap.parse_args()

    rank_col, sources = None, []
    if args.users_csv:
        players = from_users_csv(args.users_csv)
        side = args.users_csv + ".sources.json"            # written by --write-users: which dumps the sample came from
        sources = args.sources or (json.load(open(side)) if os.path.exists(side) else [os.path.basename(args.users_csv)])
    else:
        dumps = args.dumps or latest_dumps(args.count)
        print("dumps:", ", ".join(dumps))
        players = {}
        for d in sorted(dumps):                       # oldest first, so the newest row for a player wins
            stats, rc = read_stats(fetch(d, args.cache))
            rank_col = rank_col or rc
            print(f"  {d}: {len(stats):,} players{'' if rc else ' (no rank column)'}")
            players.update(stats)
        sources = sorted(dumps)
    print(f"merged: {len(players):,} distinct players")

    if args.write_users:
        os.makedirs(os.path.dirname(os.path.abspath(args.write_users)), exist_ok=True)
        with open(args.write_users, "w", newline="") as f:
            w = csv.writer(f); w.writerow(["user_id", "pp", "hours", "rank"])
            for u, p in players.items():
                w.writerow([u, round(p["pp"], 2), round(p["hours"], 4), p["rank"]])
        with open(args.write_users + ".sources.json", "w") as f:
            json.dump(sources, f)

    allpp = np.sort(np.array([p["pp"] for p in players.values() if p["pp"] > 0]))
    cur = [(p["hours"], p["pp"]) for p in players.values() if p["hours"] >= 1 and p["pp"] > 0]
    H = np.array([h for h, _ in cur]); PP = np.array([p for _, p in cur]); lh, lp = np.log(H), np.log(PP)

    fwd = quad_fit(lh, lp); fwd_res = lp - (fwd[0] + fwd[1] * lh + fwd[2] * lh * lh)
    rev = quad_fit(lp, lh); rev_res = lh - (rev[0] + rev[1] * lp + rev[2] * lp * lp)

    hist = {}
    if os.path.exists(args.history_fit):
        with open(args.history_fit) as f:
            hist = json.load(f)

    fa = frac_above_fn(np.log(allpp))
    dump_pairs = [(p["pp"], p["rank"]) for p in players.values() if p["rank"] > 0 and p["pp"] > 0]
    if len(dump_pairs) >= 500:
        N, corr, err = calibrate_ranks(dump_pairs, fa); rank_src = f"{len(dump_pairs):,} players' ranks in the dumps"
    elif len(hist.get("rank_pairs", [])) >= 200:
        N, corr, err = calibrate_ranks(hist["rank_pairs"], fa); rank_src = f"{len(hist['rank_pairs']):,} players' ranks from the osu! API"
    else:
        N, corr, err, rank_src = 2985000, [], None, "default"
    print(f"ranks: leaderboard ~{N:,} players, from {rank_src}" + (f", median error after correction {err * 100:.1f}%" if err is not None else ""))

    # thin random subsample for the scatter so the page stays light (percentiles use everything)
    rng = np.random.default_rng(0)
    keep = np.arange(len(H)) if len(H) <= 12000 else np.sort(rng.choice(len(H), 12000, replace=False))

    model = {
        "built": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "builtAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),   # exact time; the page shows it in the viewer's time zone
        "sources": sources,
        "n": int(len(H)),
        "fwd": [round(float(v), 7) for v in fwd],
        "fwdSd": robust_sd_table(lh, fwd_res),
        "rev": [round(float(v), 7) for v in rev],
        "revSd": robust_sd_table(lp, rev_res),
        "hMin": round(float(H.min()), 3), "hMax": round(float(H.max()), 1),
        "thin": round(float(np.percentile(H, 98)), 1),
        "pts": [[round(float(h), 2), round(float(p))] for h, p in zip(H, PP)],
        "scatter": keep.tolist() if len(keep) < len(H) else None,
        "allpp": [round(float(p), 1) for p in allpp],
        "rank": {"N": N, "corr": corr, "source": rank_src},
        "history": {k: v for k, v in hist.items() if k != "rank_pairs"},
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(model, f, separators=(",", ":"))
    print(f"wrote {args.out} ({os.path.getsize(args.out) / 1e6:.1f} MB): curve pp = exp({fwd[0]:.3f} + {fwd[1]:.3f} ln H + {fwd[2]:.4f} ln^2 H)")


if __name__ == "__main__":
    main()
