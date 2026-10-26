#!/usr/bin/env python3
"""
Turn pulled player histories into data/history_fit.json, which build_model.py merges into the site's model.

Measures (from data/history.jsonl.gz, or any .jsonl given with --history):
  top        typical pp of a player's best play at each total pp, fitted the same way
  stars      typical star rating of top plays at each total pp (median of each player's top 10 plays
             without DT/NC/HT/DC/HR/EZ/FL), fitted as ln(stars) = a + b ln(pp) + c ln(pp)^2
  drift      how fast a player's distance from the trend (z, in std devs) fades as they play:
             z_later = z_now * (H / H_later)^kappa. Past pp has to be rebuilt from today's top 100, which
             can't see scores a player later beat, so kappa is fitted under several reasonable rebuild
             choices; the site uses the median as the central value and the min/max as its range.
  spread     how much real outcomes scatter around that (variance per ln-hour)
  pph        pp gained per hour by players near the median, by play time
  rank_pairs (pp, global rank) for calibrating ranks

Usage:
    python scripts/fit_histories.py
    python scripts/fit_histories.py --history out/history.jsonl
"""
import argparse, gzip, json, math, os
from datetime import datetime, timezone

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SR_CHANGING = {"DT", "NC", "HT", "DC", "HR", "EZ", "FL"}


def load(path):
    op = gzip.open if path.endswith(".gz") else open
    with op(path, "rt") as f:
        return [r for r in (json.loads(l) for l in f if l.strip()) if not r.get("missing")]


def curve_from_model(path):
    if os.path.exists(path):
        with open(path) as f:
            m = json.load(f)
        return m["fwd"], m["fwdSd"]
    # fallback: September 2026 sample
    return ([3.58469, 0.924351, -0.0232724],
            [[0.272, 0.645], [0.799, 0.664], [1.285, 0.624], [1.716, 0.530], [2.163, 0.516], [2.626, 0.465], [3.035, 0.407],
             [3.428, 0.404], [3.855, 0.409], [4.252, 0.374], [4.660, 0.361], [5.071, 0.344], [5.599, 0.308], [6.483, 0.277]])


def z_function(model_path):
    """z of (hours, pp) exactly as the site computes it: through the curve's real quantiles (z = -2..2 at the
    2.3/15.9/50/84.1/97.7% points, linear past them); older model files fall back to the normal bands."""
    fwd, sd_tab = curve_from_model(model_path)
    fq = None
    if os.path.exists(model_path):
        with open(model_path) as f:
            fq = json.load(f).get("fq")
    if not fq:
        fq = [[x, -2 * s, -s, 0, s, 2 * s] for x, s in sd_tab]
    T = np.array(fq, float)

    def z(h, p):
        x, y = math.log(h), math.log(p)
        b = fwd[0] + fwd[1] * x + fwd[2] * x * x
        k = [b + float(np.interp(x, T[:, 0], T[:, j])) for j in range(1, 6)]
        if y <= k[0]: return -2 + (y - k[0]) / (k[1] - k[0])
        if y >= k[4]: return 2 + (y - k[4]) / (k[4] - k[3])
        i = 0
        while i < 3 and y > k[i + 1]: i += 1
        return i - 2 + (y - k[i]) / (k[i + 1] - k[i])
    return z


def parse_date(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def weighted(pps):
    return sum(p * 0.95 ** i for i, p in enumerate(sorted(pps, reverse=True)[:100]))


def trajectory(r, bonus):
    """Monthly (hours, pp, scores_so_far). bonus = how bonus pp (+ scores past the top 100) is spread over time."""
    if not r.get("scores") or not r.get("months") or not r.get("pp") or not r.get("play_time"):
        return []
    total_h, mplays = r["play_time"] / 3600, sum(c for _, c in r["months"])
    if total_h < 1 or mplays < 20 or (r.get("play_count") and mplays / r["play_count"] > 1.1):
        return []                                   # skip players whose monthly plays include other modes
    sc = [(parse_date(s["at"]), s["pp"]) for s in r["scores"] if s.get("at")]
    B, n_all = max(0.0, r["pp"] - weighted([p for _, p in sc])), max(len(sc), 1)
    pts, cum = [], 0
    for start, c in sorted(r["months"]):
        cum += c
        m = parse_date(start + "T00:00:00Z")
        end = datetime(m.year + (m.month == 12), m.month % 12 + 1, 1, tzinfo=timezone.utc)
        frac, n = cum / mplays, sum(1 for d, _ in sc if d < end)
        b = {"plays": B * frac, "scores": B * n / n_all, "const": B}[bonus]
        pp = weighted([p for d, p in sc if d < end]) + b
        if total_h * frac >= 1 and pp > 0:
            pts.append((total_h * frac, pp, n))
    return pts


def fit_kappa(z1, z2, d):
    grid = np.linspace(-1, 2, 301)
    sse = [np.sum((z2 - z1 * np.exp(-k * d)) ** 2) for k in grid]
    return float(grid[int(np.argmin(sse))])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--history", default=os.path.join(ROOT, "data", "history.jsonl.gz"))
    ap.add_argument("--model", default=os.path.join(ROOT, "data", "model.json"))
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "history_fit.json"))
    args = ap.parse_args()

    recs = load(args.history)
    zscore = z_function(args.model)
    print(f"{len(recs):,} players")

    # ---- star level
    rows = []
    for r in recs:
        clean = sorted([s for s in r.get("scores") or [] if s.get("sr") and not (set(s.get("mods") or []) & SR_CHANGING)],
                       key=lambda s: -s["pp"])[:10]
        if len(clean) >= 5 and r.get("pp"):
            rows.append((r["pp"], float(np.median([s["sr"] for s in clean]))))
    S = np.array(rows); c2, b2, a2 = np.polyfit(np.log(S[:, 0]), np.log(S[:, 1]), 2)
    resid = np.log(S[:, 1]) - (a2 + b2 * np.log(S[:, 0]) + c2 * np.log(S[:, 0]) ** 2)
    print(f"stars: {len(S)} players, ln(stars) = {a2:.5f} + {b2:.5f} ln pp + {c2:.6f} ln^2 pp")

    # ---- top play: each player's best play (any mods) against total pp, same quadratic in log space
    T = np.array([(r["pp"], max(x["pp"] for x in r["scores"] if x.get("pp")))
                  for r in recs if r.get("pp") and sum(1 for x in r.get("scores") or [] if x.get("pp")) >= 20])
    c3, b3, a3 = np.polyfit(np.log(T[:, 0]), np.log(T[:, 1]), 2)
    tres = np.log(T[:, 1]) - (a3 + b3 * np.log(T[:, 0]) + c3 * np.log(T[:, 0]) ** 2)
    print(f"top play: {len(T)} players, ln(top) = {a3:.5f} + {b3:.5f} ln pp + {c3:.6f} ln^2 pp, spread {np.std(tres):.3f}")

    # ---- drift under several rebuild choices (12-month windows)
    variants = {}
    for bonus in ("plays", "scores", "const"):
        trajs = [trajectory(r, bonus) for r in recs]
        for min_n in (30, 60):
            W = []
            for t in trajs:
                for i in range(0, len(t) - 12, 3):
                    (h1, p1, n1), (h2, p2, _) = t[i], t[i + 12]
                    if h2 > h1 * 1.1 and n1 >= min_n:
                        W.append((zscore(h1, p1), zscore(h2, p2), math.log(h2 / h1)))
            if len(W) >= 100:
                W = np.array(W); k = fit_kappa(W[:, 0], W[:, 1], W[:, 2])
                variants[f"{bonus}/{min_n}"] = (k, len(W), float(np.sum((W[:, 1] - W[:, 0] * np.exp(-k * W[:, 2])) ** 2 * W[:, 2]) / np.sum(W[:, 2] ** 2)))
    ks = sorted(v[0] for v in variants.values())
    kappa = float(np.median(ks)); k_lo, k_hi = max(0.05, min(ks)), max(ks)
    spread = variants.get("plays/30", next(iter(variants.values())))[2]
    for name, (k, n, _) in variants.items():
        print(f"  drift {name:<10} kappa {k:+.2f}  ({n} windows)")
    print(f"drift: kappa {kappa:.2f} (range {k_lo:.2f}-{k_hi:.2f}), spread {spread:.3f}")

    # ---- real pp per hour near the median (6-month windows), two sensible bonus choices
    bins = [(5, 15), (15, 40), (40, 100), (100, 250), (250, 600), (600, 1500)]
    pph = []
    trajs = {b: [trajectory(r, b) for r in recs] for b in ("plays", "scores")}
    for lo, hi in bins:
        meds = []
        for b in ("plays", "scores"):
            v = [(p2 - p1) / (h2 - h1) for t in trajs[b] for i in range(0, len(t) - 6, 2)
                 for (h1, p1, _), (h2, p2, _) in [(t[i], t[i + 6])]
                 if lo <= h1 < hi and h2 > h1 * 1.05 and abs(zscore(h1, p1)) < 0.5]
            meds.append(float(np.median(v)) if len(v) >= 15 else None)
        if None not in meds:
            pph.append([round(math.exp((math.log(lo) + math.log(hi)) / 2), 1), round(min(meds), 1), round(max(meds), 1)])
    print("pp/hour:", pph)

    # when the newest player was pulled (records from older pulls have no date: keep the previous value)
    pulled = max((r["pulled"] for r in recs if r.get("pulled")), default=None)
    if not pulled and os.path.exists(args.out):
        with open(args.out) as f:
            pulled = json.load(f).get("pulledAt")
    out = {
        "fitted": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "pulledAt": pulled,
        "players": len(recs),
        "stars": {"a": round(float(a2), 5), "b": round(float(b2), 5), "c": round(float(c2), 6), "n": len(S),
                  "spread": round(float(np.std(resid)), 3)},
        "top": {"a": round(float(a3), 5), "b": round(float(b3), 5), "c": round(float(c3), 6), "n": len(T),
                "spread": round(float(np.std(tres)), 3)},
        "drift": {"kappa": round(kappa, 3), "lo": round(k_lo, 3), "hi": round(k_hi, 3), "spread": round(spread, 3),
                  "variants": {k: round(v[0], 3) for k, v in variants.items()}},
        "pph": pph,
        "rank_pairs": [[round(r["pp"], 1), int(r["rank"])] for r in recs if r.get("rank") and r.get("pp")],
    }
    with open(args.out, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
