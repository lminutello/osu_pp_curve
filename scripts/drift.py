#!/usr/bin/env python3
"""
How players really move: real before/after pairs for the projection, refreshed monthly.

data.ppy.sh keeps dumps for roughly half a year. Each month this takes the oldest one still up, picks players from it
who had at least an hour of play time, and looks up their stats today (1 API request each). Every pair (then, now) is
stored as z (std devs from the typical pp, measured on that dump's own curve) before and after, plus how much play
time was added, in data/drift_pairs.jsonl.gz. From all pairs it fits:

  kappa   how fast z fades as you play: z_later = z_now * (H / H_later)^kappa, with a bootstrap 95% range
  band    the real spread of outcomes around that (16th and 84th percentile of the miss, in z units), by play time
          and by how much play time was added, so the projection's range holds about 68% of real players
  pph     real pp gained per hour by players near the typical pp, by play time

Usage:  python scripts/drift.py pull --players 2000     (needs OSU_CLIENT_ID / OSU_CLIENT_SECRET)
        python scripts/drift.py fit                     (writes data/drift_fit.json; build_model.py merges it)
        python scripts/drift.py seed --dump-csv X --now Y.jsonl.gz --date YYYY_MM_DD   (from audit files)
"""
import argparse, csv, gzip, json, math, os, random, re, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_model import archive_dump, fetch, kernel_quantiles, quad_fit, read_stats, z_of  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAIRS = os.path.join(ROOT, "data", "drift_pairs.jsonl.gz")
OUT = os.path.join(ROOT, "data", "drift_fit.json")
UA = {"User-Agent": "osu-playtime-curve (github.com/lminutello/osu_pp_curve)"}


class Curve:
    """The site's curve (quadratic backbone + real quantiles) for one dump."""
    def __init__(self, hours, pp):
        H, P = np.array(hours, float), np.array(pp, float); k = (H >= 1) & (P > 0)
        x, y = np.log(H[k]), np.log(P[k]); self.c = quad_fit(x, y)
        self.tab = kernel_quantiles(x, y - (self.c[0] + self.c[1] * x + self.c[2] * x * x), self.c)

    def z(self, h, p):
        return float(z_of(self.c, self.tab, np.array([math.log(h)]), np.array([math.log(p)]))[0])


def load_pairs():
    if not os.path.exists(PAIRS):
        return []
    with gzip.open(PAIRS, "rt") as f:
        return [json.loads(l) for l in f if l.strip()]


def save_pairs(recs):
    with gzip.open(PAIRS, "wt") as f:
        for r in recs:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")


def record(dump, uid, h0, p0, now, curve, pulled):
    """One pair, or None if the player is gone or has no stats."""
    st = (now or {}).get("statistics") or now or {}
    p1, t1 = st.get("pp"), st.get("play_time")
    if not p1 or not t1:
        return None
    h1 = t1 / 3600
    r = {"dump": dump, "id": int(uid), "h0": round(h0, 3), "p0": round(p0, 1), "h1": round(h1, 3), "p1": round(p1, 1),
         "pulled": pulled, "z0": round(curve.z(h0, p0), 4)}
    if h1 > 0 and p1 > 0:
        r["z1"] = round(curve.z(max(h1, 1.0), p1), 4)
    return r


def cmd_pull(args):
    cid, secret = os.environ.get("OSU_CLIENT_ID"), os.environ.get("OSU_CLIENT_SECRET")
    if not cid or not secret:
        print("OSU_CLIENT_ID / OSU_CLIENT_SECRET aren't set; skipping."); return
    html = urllib.request.urlopen(urllib.request.Request("https://data.ppy.sh/", headers=UA), timeout=60).read().decode()
    dates = sorted(set(re.findall(r"(\d{4}_\d{2}_\d{2})_performance_osu_random_10000\.tar\.bz2", html)))
    dump = dates[0]                                        # the oldest one still up: the longest real gap
    print(f"dumps available: {', '.join(dates)}; using {dump}", flush=True)
    stats, _ = read_stats(fetch(dump, args.cache))
    archive_dump(dump, stats)
    curve = Curve([s["hours"] for s in stats.values()], [s["pp"] for s in stats.values()])
    recs = load_pairs(); done = {(r["dump"], r["id"]) for r in recs}
    pool = [u for u, s in stats.items() if s["hours"] >= 1 and s["pp"] > 0 and (dump, int(u)) not in done]
    pick = random.Random(int(time.time())).sample(pool, min(args.players, len(pool)))
    print(f"pulling {len(pick)} players", flush=True)
    body = urllib.parse.urlencode({"client_id": cid, "client_secret": secret, "grant_type": "client_credentials", "scope": "public"}).encode()
    tok = json.load(urllib.request.urlopen(urllib.request.Request("https://osu.ppy.sh/oauth/token", data=body,
                    headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}), timeout=30))["access_token"]
    today, last, new = datetime.now(timezone.utc).strftime("%Y-%m-%d"), 0.0, 0
    for i, u in enumerate(pick):
        d = None
        for attempt in range(4):
            wait = 1.05 - (time.time() - last)
            if wait > 0: time.sleep(wait)
            last = time.time()
            try:
                req = urllib.request.Request(f"https://osu.ppy.sh/api/v2/users/{u}/osu?key=id",
                                             headers={"Authorization": f"Bearer {tok}", "Accept": "application/json"})
                d = json.load(urllib.request.urlopen(req, timeout=30)); break
            except urllib.error.HTTPError as e:
                if e.code == 404: break
                time.sleep(10 * (attempt + 1))
            except (urllib.error.URLError, TimeoutError):
                time.sleep(5 * (attempt + 1))
        r = record(dump, u, stats[u]["hours"], stats[u]["pp"], d, curve, today) if d else None
        if r:
            recs.append(r); new += 1
        if (i + 1) % 250 == 0:
            print(f"  {i + 1}/{len(pick)}", flush=True); save_pairs(recs)
    save_pairs(recs)
    print(f"added {new} pairs from {dump}; {len(recs)} in total")


def cmd_seed(args):
    """Seed from the accuracy audit's files (a dump's stats table and that dump's players pulled again)."""
    rows = list(csv.DictReader(gzip.open(args.dump_csv, "rt")))
    stats = {r["user_id"]: (float(r["total_seconds_played"]) / 3600, float(r["rank_score"])) for r in rows}
    curve = Curve([h for h, _ in stats.values()], [p for _, p in stats.values()])
    recs = load_pairs(); done = {(r["dump"], r["id"]) for r in recs}; new = 0
    with gzip.open(args.now, "rt") as f:
        for line in f:
            v = json.loads(line)
            if v.get("missing") or str(v["id"]) not in stats or (args.date, int(v["id"])) in done:
                continue
            h0, p0 = stats[str(v["id"])]
            if h0 < 1 or p0 <= 0:
                continue
            r = record(args.date, v["id"], h0, p0, v, curve, (v.get("pulled") or "")[:10])
            if r:
                recs.append(r); new += 1
    save_pairs(recs)
    print(f"seeded {new} pairs from {args.date}; {len(recs)} in total")


def fit_kappa(z0, z1, d):
    grid = np.linspace(-0.5, 2, 251)
    return float(grid[int(np.argmin([np.sum((z1 - z0 * np.exp(-k * d)) ** 2) for k in grid]))])


def wquant(v, w, q):
    o = np.argsort(v); v, w = v[o], w[o]; c = (np.cumsum(w) - 0.5 * w) / w.sum()
    return float(np.interp(q, c, v))


def merge_into(model, dr):
    """Put a drift fit into a model dict (used by build_model.py and after each monthly drift pull)."""
    h = model.setdefault("history", {})
    h["drift"] = {"kappa": dr["kappa"], "lo": dr["lo"], "hi": dr["hi"], "band": dr["band"], "n": dr["n"],
                  "gapDays": dr["gapDays"], "pulledAt": dr["pulledAt"], "real": True, "check": dr.get("check")}
    if dr.get("pph"):
        h["pph"] = [[x, lo, hi, med] for x, lo, hi, med, _ in dr["pph"]]
        h["pphReal"] = True


def usable(recs):
    return [r for r in recs if "z1" in r and r["h1"] > r["h0"] * 1.05 and abs(r["z0"]) <= 3.5]


def cmd_fit(args):
    recs = usable(load_pairs())
    if len(recs) < 300:
        print(f"only {len(recs)} usable pairs; not fitting"); return
    out = fit(recs)
    out["check"] = check(recs)
    print(f"held-out: {out['check']['inside']}% of real outcomes inside the 68% range")
    with open(OUT, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    model_path = os.path.join(ROOT, "data", "model.json")
    if os.path.exists(model_path):                      # update the live model too, so no full rebuild is needed
        with open(model_path) as f:
            m = json.load(f)
        merge_into(m, out)
        with open(model_path, "w") as f:
            json.dump(m, f, separators=(",", ":"))
    print(f"drift from {out['n']} real pairs ({', '.join(out['dumps'])} -> {out['pulledAt']}, median gap {out['gapDays']} days): "
          f"kappa {out['kappa']:.2f} (95% {out['lo']:.2f}-{out['hi']:.2f}); pp/hour {out['pph']}")


def band_at(B, lh, d):
    """The site's bandAt: bilinear in (ln h, ln d), shrinking to 0 for tiny gains, widening like sqrt past the grid."""
    ld = math.log(max(d, 1e-9)); D0, DN = math.exp(B["ld"][0]), math.exp(B["ld"][-1])
    def ix(g, v):
        if v <= g[0]: return 0, 0, 0.0
        if v >= g[-1]: return len(g) - 1, len(g) - 1, 0.0
        i = 0
        while v > g[i + 1]: i += 1
        return i, i + 1, (v - g[i]) / (g[i + 1] - g[i])
    a0, a1, ta = ix(B["lh"], lh); b0, b1, tb = ix(B["ld"], ld)
    def bil(T):
        r0 = T[a0][b0] + (T[a0][b1] - T[a0][b0]) * tb; r1 = T[a1][b0] + (T[a1][b1] - T[a1][b0]) * tb
        return r0 + (r1 - r0) * ta
    f = d / D0 if d < D0 else math.sqrt(d / DN) if d > DN else 1.0
    return bil(B["q16"]) * f, bil(B["q84"]) * f


def check(recs, folds=5):
    """Held-out: fit on 4/5 of the pairs, then how often the rest land inside the 68% range (in z, so no curve needed)."""
    fold = np.random.default_rng(1).integers(0, folds, len(recs)); inside = []
    for k in range(folds):
        fp = fit([r for r, f in zip(recs, fold) if f != k])
        for r, f in zip(recs, fold):
            if f != k: continue
            d = math.log(r["h1"] / r["h0"]); zc = r["z0"] * math.exp(-fp["kappa"] * d)
            q16, q84 = band_at(fp["band"], math.log(r["h0"]), d)
            inside.append(zc + q16 <= r["z1"] <= zc + q84)
    return {"inside": round(100 * float(np.mean(inside)), 1), "n": len(inside)}


def fit(recs):
    z0 = np.array([r["z0"] for r in recs]); z1 = np.array([r["z1"] for r in recs])
    d = np.log(np.array([r["h1"] / r["h0"] for r in recs])); lh = np.log(np.array([r["h0"] for r in recs]))
    kappa = fit_kappa(z0, z1, d)
    rng = np.random.default_rng(0); boot = []
    for _ in range(200):
        i = rng.integers(0, len(z0), len(z0)); boot.append(fit_kappa(z0[i], z1[i], d[i]))
    lo, hi = np.percentile(boot, [2.5, 97.5])
    # real spread of outcomes around the centre, in z units, by play time and by play time added
    miss = z1 - z0 * np.exp(-kappa * d); ld = np.log(d)
    GH = [round(math.log(h), 3) for h in (1.5, 4, 12, 40, 150, 500)]
    GD = [round(math.log(x), 3) for x in (0.05, 0.15, 0.4, 1.0, 2.0)]
    q16, q84 = [], []
    for gh in GH:
        r16, r84 = [], []
        for gd in GD:
            w = np.exp(-0.5 * ((lh - gh) / 0.8) ** 2 - 0.5 * ((ld - gd) / 0.6) ** 2)
            r16.append(round(wquant(miss, w, 0.15866), 3)); r84.append(round(wquant(miss, w, 0.84134), 3))
        q16.append(r16); q84.append(r84)
    # real pp per hour near the typical pp (|z| < 0.5), by play time: median with a bootstrap 90% range
    pph = []
    for a, b in [(3, 10), (10, 30), (30, 80), (80, 200), (200, 500), (500, 1500)]:
        m = [(r["p1"] - r["p0"]) / (r["h1"] - r["h0"]) for r in recs if a <= r["h0"] < b and abs(r["z0"]) < 0.5]
        if len(m) >= 25:
            m = np.array(m); bs = [np.median(rng.choice(m, len(m))) for _ in range(300)]
            pph.append([round(math.sqrt(a * b), 1), round(float(np.percentile(bs, 5)), 1), round(float(np.percentile(bs, 95)), 1), round(float(np.median(m)), 1), len(m)])
    dumps = sorted({r["dump"] for r in recs}); pulled = max(r["pulled"] for r in recs if r.get("pulled"))
    gap = float(np.median([(datetime.strptime(r["pulled"], "%Y-%m-%d") - datetime.strptime(r["dump"], "%Y_%m_%d")).days for r in recs if r.get("pulled")]))
    return {"n": len(recs), "dumps": dumps, "pulledAt": pulled, "gapDays": round(gap), "kappa": round(kappa, 3),
            "lo": round(float(lo), 3), "hi": round(float(hi), 3), "band": {"lh": GH, "ld": GD, "q16": q16, "q84": q84}, "pph": pph}


def main():
    ap = argparse.ArgumentParser(); sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pull"); p.add_argument("--players", type=int, default=2000); p.add_argument("--cache", default=os.path.join(ROOT, ".cache"))
    sub.add_parser("fit")
    s = sub.add_parser("seed"); s.add_argument("--dump-csv", required=True); s.add_argument("--now", required=True); s.add_argument("--date", required=True)
    args = ap.parse_args()
    {"pull": cmd_pull, "fit": cmd_fit, "seed": cmd_seed}[args.cmd](args)


if __name__ == "__main__":
    main()
