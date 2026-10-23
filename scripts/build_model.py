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
            out[r[ix["user_id"]]] = {"pp": pp, "hours": sec / 3600, "rank": rank,
                                     "playcount": r[ix["playcount"]] if "playcount" in ix else "",
                                     "last_played": r[ix["last_played"]] if "last_played" in ix else "",
                                     "acc": r[ix["accuracy_new"]] if "accuracy_new" in ix else ""}
    return out, rank_col


ARCHIVE = os.path.join(ROOT, "data", "dumps")


def archive_dump(dump, stats):
    """Keep each dump's player stats in the repo: data.ppy.sh only keeps about 6 months of dumps."""
    path = os.path.join(ARCHIVE, f"{dump}.csv.gz")
    if os.path.exists(path):
        return
    os.makedirs(ARCHIVE, exist_ok=True)
    import gzip
    with gzip.open(path, "wt", newline="") as f:
        w = csv.writer(f); w.writerow(["user_id", "pp", "hours", "rank", "playcount", "last_played", "acc"])
        for u, p in stats.items():
            w.writerow([u, round(p["pp"], 2), round(p["hours"], 4), p["rank"], p.get("playcount", ""), p.get("last_played", ""), p.get("acc", "")])
    print(f"  archived {dump} -> data/dumps/{dump}.csv.gz")


def from_users_csv(path):
    out = {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            try:
                out[r["user_id"]] = {"pp": float(r["pp"]), "hours": float(r["hours"]), "rank": int(float(r.get("rank") or 0)),
                                     "last_played": r.get("last_played") or "", "dump": r.get("dump") or "",
                                     "acc": r.get("acc") or ""}
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


# quantiles of the residual at z = -2, -1, 0, +1, +2 of a normal: the middle 68.3% / 95.4% bands and the median
QS = [0.02275, 0.15866, 0.5, 0.84134, 0.97725]


def kernel_quantiles(x, resid, coef=None, bw=0.25, min_eff=300, step=0.1):
    """Real quantiles of the residual around the quadratic, smoothed along x with Gaussian weights (bandwidth bw,
    widened where data is thin). The quadratic is only the backbone: the site's median and bands come from these."""
    grid = np.arange(x.min(), x.max() + step, step); out = []
    for g in grid:
        b = bw
        while True:
            w = np.exp(-0.5 * ((x - g) / b) ** 2)
            if w.sum() ** 2 / (w * w).sum() >= min_eff or b > 3: break
            b *= 1.25
        o = np.argsort(resid); v, ww = resid[o], w[o]; c = (np.cumsum(ww) - 0.5 * ww) / ww.sum()
        out.append([round(float(g), 3)] + [round(float(q), 4) for q in np.interp(QS, c, v)])
    if coef is not None:
        # the median and every band edge must keep rising (more hours -> more pp and vice versa). They can dip where
        # data is thin or cut off (the 1-hour floor, the far tails); there, pull the lower point down to the next one
        base = [coef[0] + coef[1] * r[0] + coef[2] * r[0] ** 2 for r in out]
        for j in range(1, 6):
            for i in range(len(out) - 2, -1, -1):
                lim = base[i + 1] + out[i + 1][j] - 1e-4
                if base[i] + out[i][j] > lim:
                    out[i][j] = round(lim - base[i], 4)
        for r in out:                                  # keep the five edges in order at every point
            r[1:] = sorted(r[1:])
    return out


def z_of(coef, tab, x, y):
    """z through the quantile knots, exactly like the site (vectorized)."""
    T = np.array(tab, float); b = coef[0] + coef[1] * x + coef[2] * x * x
    K = np.stack([b + np.interp(x, T[:, 0], T[:, j]) for j in range(1, 6)], -1)
    z = np.empty(len(x))
    for i in range(len(x)):
        k = K[i]
        z[i] = (-2 + (y[i] - k[0]) / (k[1] - k[0]) if y[i] <= k[0] else 2 + (y[i] - k[4]) / (k[4] - k[3]) if y[i] >= k[4]
                else float(np.interp(y[i], k, [-2, -1, 0, 1, 2])))
    return z


def self_check(x, y, folds=5):
    """Held-out check of the median and bands: fit on 4/5 of players, test on the rest."""
    fold = np.random.default_rng(0).integers(0, folds, len(x)); z = np.empty(len(x))
    for k in range(folds):
        tr, te = fold != k, fold == k
        c = quad_fit(x[tr], y[tr]); tab = kernel_quantiles(x[tr], y[tr] - (c[0] + c[1] * x[tr] + c[2] * x[tr] ** 2), c)
        z[te] = z_of(c, tab, x[te], y[te])
    edges = np.quantile(x, np.linspace(0, 1, 9)); worst = 0.0
    for a, b in zip(edges[:-1], edges[1:]):
        m = (x >= a) & (x <= b); worst = max(worst, abs(np.mean(np.abs(z[m]) < 1) - 0.6827), abs(np.mean(z[m] < 0) - 0.5))
    return {"in68": round(float(np.mean(np.abs(z) < 1)) * 100, 1), "in95": round(float(np.mean(np.abs(z) < 2)) * 100, 1),
            "below": round(float(np.mean(z < 0)) * 100, 1), "worstBin": round(worst * 100, 1)}


ACC_Q = [1, 2, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80, 85, 90, 95, 98, 99]


def accuracy_table(players, bw=0.06, min_eff=300, step=0.05):
    """Accuracy (the profile's hit accuracy, from the dumps) of players at each pp: real percentiles, smoothed along
    ln(pp) with Gaussian weights (widened where data is thin). Accuracy changes fast with pp, so the window is narrow.
    None if the dumps have no accuracy column."""
    pairs = []
    for p in players.values():
        try:
            a = float(p.get("acc") or "nan")
        except ValueError:
            continue
        if p["pp"] > 0 and 0 < a <= 100:
            pairs.append((math.log(p["pp"]), a))
    if len(pairs) < 2000:
        return None
    x, a = np.array([q for q, _ in pairs]), np.array([v for _, v in pairs]); o = np.argsort(a); x, a = x[o], a[o]
    tab = []
    for g in np.arange(x.min(), x.max() + step, step):
        b = bw
        while True:
            w = np.exp(-0.5 * ((x - g) / b) ** 2)
            if w.sum() ** 2 / (w * w).sum() >= min_eff or b > 3: break
            b *= 1.25
        c = (np.cumsum(w) - 0.5 * w) / w.sum()
        tab.append([round(float(g), 3)] + [round(float(v), 3) for v in np.interp(np.array(ACC_Q) / 100, c, a)])
    return {"q": ACC_Q, "tab": tab, "n": len(pairs)}


def accuracy_check(players, folds=5):
    """Held-out: build the table from 4/5 of players; for the rest, how far each tenth of 'better than' strays from 10%."""
    ids = [u for u, p in players.items() if p["pp"] > 0 and (p.get("acc") or "") not in ("",)]
    fold = np.random.default_rng(2).integers(0, folds, len(ids)); pit = []
    for k in range(folds):
        t = accuracy_table({u: players[u] for u, f in zip(ids, fold) if f != k})
        if not t: return None
        T = np.array(t["tab"])
        for u, f in zip(ids, fold):
            if f != k: continue
            a = float(players[u]["acc"])
            if not 0 < a <= 100: continue
            row = [np.interp(math.log(players[u]["pp"]), T[:, 0], T[:, j]) for j in range(1, len(t["q"]) + 1)]
            pit.append(float(np.interp(a, row, t["q"], left=0.5, right=99.5)))
    h = np.histogram(pit, bins=np.linspace(0, 100, 11))[0] / len(pit) * 100
    return round(float(np.abs(h - 10).max()), 1)


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
            archive_dump(d, stats)
            rank_col = rank_col or rc
            print(f"  {d}: {len(stats):,} players{'' if rc else ' (no rank column)'}")
            for s_ in stats.values():
                s_["dump"] = d                        # when this row was recorded, for "played recently"
            players.update(stats)
        sources = sorted(dumps)
    print(f"merged: {len(players):,} distinct players")

    if args.write_users:
        os.makedirs(os.path.dirname(os.path.abspath(args.write_users)), exist_ok=True)
        with open(args.write_users, "w", newline="") as f:
            w = csv.writer(f); w.writerow(["user_id", "pp", "hours", "rank", "last_played", "dump", "acc"])
            for u, p in players.items():
                w.writerow([u, round(p["pp"], 2), round(p["hours"], 4), p["rank"], p.get("last_played", ""), p.get("dump", ""), p.get("acc", "")])
        with open(args.write_users + ".sources.json", "w") as f:
            json.dump(sources, f)

    allpp = np.sort(np.array([p["pp"] for p in players.values() if p["pp"] > 0]))
    cur = [(p["hours"], p["pp"]) for p in players.values() if p["hours"] >= 1 and p["pp"] > 0]
    H = np.array([h for h, _ in cur]); PP = np.array([p for _, p in cur]); lh, lp = np.log(H), np.log(PP)

    fwd = quad_fit(lh, lp); fwd_res = lp - (fwd[0] + fwd[1] * lh + fwd[2] * lh * lh)
    # hours given pp ("faster than", "typical took") uses everyone with any play time: the curve's 1-hour floor
    # would otherwise cut off the quick low-pp players and make the lowest pp levels look slow
    under = [(p["hours"], p["pp"]) for p in players.values() if 0 < p["hours"] < 1 and p["pp"] > 0]
    H0 = np.array([h for h, _ in under]); PP0 = np.array([p for _, p in under])
    lh2, lp2 = np.log(np.concatenate([H, H0])), np.log(np.concatenate([PP, PP0]))
    rev = quad_fit(lp2, lh2); rev_res = lh2 - (rev[0] + rev[1] * lp2 + rev[2] * lp2 * lp2)

    hist = {}
    if os.path.exists(args.history_fit):
        with open(args.history_fit) as f:
            hist = json.load(f)

    fa = frac_above_fn(np.log(allpp))
    dump_pairs = [(p["pp"], p["rank"]) for p in players.values() if p["rank"] > 0 and p["pp"] > 0]
    api_pairs = hist.get("rank_pairs", [])
    if len(dump_pairs) >= 500:
        # the dumps give the high-pp correction from lots of players, but each rank is from that dump's date.
        # The leaderboard keeps growing, so take its size from whichever source is newer.
        N, corr, err = calibrate_ranks(dump_pairs, fa); rank_src = f"{len(dump_pairs):,} players' ranks in the dumps"
        newest_dump = max((s for s in sources if re.fullmatch(r"\d{4}_\d{2}_\d{2}", s)), default="")
        pulled = (hist.get("pulledAt") or "").replace("-", "_")
        if len(api_pairs) >= 200 and pulled > newest_dump:
            K = np.array(corr) if corr else None
            c = (lambda p: math.exp(np.interp(math.log(p), K[:, 0], K[:, 1]))) if K is not None else (lambda p: 1.0)
            N = round(float(np.median([r / (fa(p) * c(p)) for p, r in api_pairs])))
            rank_src += f", sized to {len(api_pairs):,} players' current ranks from the osu! API ({hist['pulledAt']})"
    elif len(api_pairs) >= 200:
        N, corr, err = calibrate_ranks(hist["rank_pairs"], fa); rank_src = f"{len(hist['rank_pairs']):,} players' ranks from the osu! API"
    else:
        N, corr, err, rank_src = 2985000, [], None, "default"
    # the top of the real leaderboard (scripts/pull_leaderboard.py): exact ranks where the sample has almost nobody
    top = []
    lb_path = os.path.join(ROOT, "data", "leaderboard.json")
    if os.path.exists(lb_path):
        with open(lb_path) as f:
            lb = json.load(f)
        top = [[round(math.log(pp), 5), r] for r, pp in sorted(lb["pairs"], key=lambda x: x[1]) if pp > 0]
        rank_src += f"; top {lb['pairs'][-1][0]:,} from the real leaderboard ({lb['pulledAt']})"
    print(f"ranks: leaderboard ~{N:,} players, from {rank_src}" + (f", median error after correction {err * 100:.1f}%" if err is not None else ""))

    # thin random subsample for the scatter so the page stays light (percentiles use everything)
    rng = np.random.default_rng(0)
    keep = np.arange(len(H)) if len(H) <= 12000 else np.sort(rng.choice(len(H), 12000, replace=False))

    # the Overwatch comparison group: players who are actually playing, like Overwatch's ladder (which only counts
    # people who played ranked this season): 10+ hours in total and last played within 60 days of their dump
    OW_MIN_H, OW_DAYS = 10, 60
    def active(p):
        try:
            return (datetime.strptime(p["dump"], "%Y_%m_%d") - datetime.strptime(p["last_played"][:10], "%Y-%m-%d")).days <= OW_DAYS
        except (KeyError, ValueError, TypeError):
            return None
    flags = [active(p) for p in players.values()]
    known = sum(f is not None for f in flags) >= 0.9 * len(players)
    ow_pp = sorted(p["pp"] for p, f in zip(players.values(), flags) if p["pp"] > 0 and p["hours"] >= OW_MIN_H and (f or not known))
    ow = {"minHours": OW_MIN_H, "activeDays": OW_DAYS if known else None, "n": len(ow_pp),
          "lpp": [round(math.log(x), 4) for x in ow_pp]}
    print(f"Overwatch group: {len(ow_pp):,} players with {OW_MIN_H}+ hours" + (f" who played in the last {OW_DAYS} days" if known else " (no last-played data)"))
    acc = accuracy_table(players)
    if acc:
        print(f"accuracy: {acc['n']:,} players; median at 1,000 pp {np.interp(math.log(1000), [r[0] for r in acc['tab']], [r[1 + acc['q'].index(50)] for r in acc['tab']]):.2f}%")
    check = self_check(lh, lp)
    if acc:
        check["accWorst"] = accuracy_check(players)
    print(f"self-check (held-out): {check['in68']}% inside the 68% band, {check['in95']}% inside the 95% band, "
          f"{check['below']}% below the median; worst play-time bracket off by {check['worstBin']} points")
    model = {
        "built": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "builtAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),   # exact time; the page shows it in the viewer's time zone
        "sources": sources,
        "n": int(len(H)),
        "fwd": [round(float(v), 7) for v in fwd],
        "fwdSd": robust_sd_table(lh, fwd_res),
        "fq": kernel_quantiles(lh, fwd_res, fwd),
        "rev": [round(float(v), 7) for v in rev],
        "revSd": robust_sd_table(lp2, rev_res),
        "rq": kernel_quantiles(lp2, rev_res, rev),
        "check": check,
        "ow": ow,
        "acc": acc,
        "pts0": [[round(float(h), 3), round(float(p), 1)] for h, p in zip(H0, PP0)],
        "hMin": round(float(H.min()), 3), "hMax": round(float(H.max()), 1),
        "thin": round(float(np.percentile(H, 98)), 1),
        "pts": [[round(float(h), 2), round(float(p))] for h, p in zip(H, PP)],
        "scatter": keep.tolist() if len(keep) < len(H) else None,
        "allpp": [round(float(p), 1) for p in allpp],
        "rank": {"N": N, "corr": corr, "source": rank_src, "top": top},
        "history": {k: v for k, v in hist.items() if k != "rank_pairs"},
    }
    # real before/after pairs (scripts/drift.py) beat the rebuilt histories for drift, its range and pp/hour
    dpath = os.path.join(ROOT, "data", "drift_fit.json")
    if os.path.exists(dpath):
        with open(dpath) as f:
            dr = json.load(f)
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import drift
        drift.merge_into(model, dr)
        print(f"drift from {dr['n']:,} real before/after pairs: kappa {dr['kappa']} ({dr['lo']}-{dr['hi']})")
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(model, f, separators=(",", ":"))
    print(f"wrote {args.out} ({os.path.getsize(args.out) / 1e6:.1f} MB): curve pp = exp({fwd[0]:.3f} + {fwd[1]:.3f} ln H + {fwd[2]:.4f} ln^2 H)")

    # players whose current ranks the weekly rank refresh looks up: 2,000 spread evenly over ln(pp)
    pos = sorted((p["pp"], u) for u, p in players.items() if p["pp"] > 0)
    lo, hi = math.log(pos[0][0]), math.log(pos[-1][0]); bins = {}
    for pp_, u in pos:
        bins.setdefault(min(39, int((math.log(pp_) - lo) / (hi - lo + 1e-9) * 40)), []).append(u)
    rng2 = np.random.default_rng(int(datetime.now(timezone.utc).strftime("%Y%m")))
    ids = [u for b in sorted(bins) for u in rng2.permutation(bins[b])[:50]]
    with open(os.path.join(ROOT, "data", "rank_ids.json"), "w") as f:
        json.dump([int(u) for u in ids], f)
    # rank lookup from the newest real (pp, rank) pairs, if there are any
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import ranks
    ranks.update_model(args.out)


if __name__ == "__main__":
    main()
