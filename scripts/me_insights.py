#!/usr/bin/env python3
"""
Turn the player's saved plays into insights for the site's progress tab (data/me_insights.json).

Inputs (all written by scripts/me_plays.py): data/me_plays.json (top 100 with difficulty attributes, last day,
most played), data/me_log.jsonl.gz (every play seen in a daily pull), data/me_peers.json (players near your pp and a
step ahead, weekly).

  skills      seven axes measured the same way for you and for each peer, with your percentile among peers
              near your pp; the lowest ones are flagged as weaknesses
  recs        for each weakness, the kind of map that trains it, plus specific maps peers at or just above your
              level have top plays on (maps you've never played, most popular among those peers first)
  sessions    from the play log: pass rate, attempts per map, warm-up/fatigue within sessions, best time of day
  trend       the aim and speed difficulty of the hardest top plays you set each week
  chokes      top plays lost to a few misses

No network. Usage:  python scripts/me_insights.py
"""
import gzip, json, math, os, statistics as stats
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
D = lambda n: os.path.join(ROOT, "data", n)
TZ = timezone(timedelta(hours=-4))                            # the player's local time (US Eastern) for time-of-day


def eff_ar(ar, mods):
    if ar is None: return None
    if "HR" in mods: ar = min(10, ar * 1.4)
    if "EZ" in mods: ar *= 0.5
    ms = 1800 - 120 * ar if ar < 5 else 1200 - 150 * (ar - 5)
    if mods & {"DT", "NC"}: ms /= 1.5
    if mods & {"HT", "DC"}: ms /= 0.75
    return (1800 - ms) / 120 if ms > 1200 else 5 + (1200 - ms) / 150


def rate(mods):
    return 1.5 if mods & {"DT", "NC"} else 0.75 if mods & {"HT", "DC"} else 1.0


def med(v):
    v = [x for x in v if x is not None]
    return stats.median(v) if v else None


def profile(best, now):
    """The same numbers for anyone, from their top plays (top 50; difficulty attributes from the top 25)."""
    b50 = [p for p in best[:50] if p.get("map")]
    b25 = [p for p in b50[:25] if p.get("attr") and p["attr"].get("aim_difficulty")]
    if len(b50) < 20 or len(b25) < 10:
        return None
    m = lambda p: set(p.get("mods") or [])
    length = [(p.get("length") or 0) / rate(m(p)) for p in b50]
    return {
        "aim": med([p["attr"]["aim_difficulty"] for p in b25]),
        "chokes": sum(1 for p in b50 if 1 <= (p.get("miss") or 0) <= 3) / len(b50),
        "speed": med([p["attr"]["speed_difficulty"] for p in b25]),
        "speedAim": med([p["attr"]["speed_difficulty"] / p["attr"]["aim_difficulty"] for p in b25]),   # higher = more streamy
        "length": med(length),
        "reading": sum(1 for p in b50 if (eff_ar(p.get("ar"), m(p)) or 0) >= 9.6) / len(b50),
        "consistency": sum(1 for p in b50 if p.get("fc") or p.get("miss") == 0) / len(b50),
        "accuracy": med([p.get("acc") for p in b50]),
        "variety": len({p.get("mapper") for p in b50}) / len(b50),
        "stars": med([p["attr"]["star_rating"] for p in b25]),
        "momentum": sum(1 for p in b50 if p.get("at") and now - datetime.fromisoformat(p["at"].replace("Z", "+00:00")) < timedelta(days=14)) / len(b50),
        "long": sum(1 for x in length if x >= 120) / len(b50),
        "dt": sum(1 for p in b50 if m(p) & {"DT", "NC"}) / len(b50),
        "hd": sum(1 for p in b50 if "HD" in m(p)) / len(b50),
    }


AXES = [  # group, key, label, unit, which way is better, what a weak value means
    ("Aim", "aim", "Aim difficulty", "num", 1, "the jumps in your top plays are easier than peers'"),
    ("Speed", "speed", "Speed difficulty", "num", 1, "the tapping in your top plays is easier than peers'; streams and fast tapping are barely trained"),
    ("Stamina", "length", "Map length", "sec", 1, "your top plays are short; long maps test stamina and recovering from mistakes"),
    ("Consistency", "consistency", "Full combos", "pct", 1, "more of your top plays have misses than peers'"),
    ("Accuracy", "accuracy", "Accuracy", "acc", 1, "your accuracy on top plays trails peers'"),
    ("Difficulty played", "stars", "Star rating of top plays", "stars", 1, "your top plays are easier maps than peers' at your pp"),
    ("Mapper variety", "variety", "Different mappers in top plays", "pct", 1, "your top plays come from few mappers"),
    ("Reading", "reading", "High AR (9.6+)", "pct", 1, "few plays at high approach rate; fast reading is untrained"),
]
FUTURE = {"reading"}                                        # shown, but not flagged or used for recommendations yet
# which weaknesses get map recommendations, in this order (mapper variety is shown but gets none: every
# recommendation is already a map you've never played, so a "new mappers" list just repeated the others)
REC_ORDER = ["speed", "length", "stars", "aim", "consistency", "accuracy"]

REC_TYPES = {
    "aim": ("Bigger jumps", lambda p, a, my: a["aim_difficulty"] >= my["aim_hi"]),
    "speed": ("Stream maps", lambda p, a, my: a["speed_difficulty"] / a["aim_difficulty"] >= 0.8 and (p.get("bpm") or 0) * rate(set(p.get("mods") or [])) >= 160),
    "length": ("Longer maps (2–4 min)", lambda p, a, my: (p.get("length") or 0) / rate(set(p.get("mods") or [])) >= 120),
    "reading": ("High-AR / DT maps", lambda p, a, my: (eff_ar(p.get("ar"), set(p.get("mods") or [])) or 0) >= 9.6),
    "consistency": ("Long-combo maps to FC", lambda p, a, my: (a.get("max_combo") or 0) >= 500),
    "chokes": ("Long-combo maps to FC", lambda p, a, my: (a.get("max_combo") or 0) >= 500),
    "accuracy": ("High-OD maps", lambda p, a, my: (p.get("od") or 0) >= 8.7),
    "variety": ("New mappers' styles", lambda p, a, my: p.get("mapper") not in my["mappers"]),
    "stars": ("A step harder", lambda p, a, my: True),
}


def recs_for(key, me_prof, my, pool, used, n=4):
    name, ok = REC_TYPES[key]
    lo, hi = (my["stars_hi"], my["stars_hi"] + 0.45) if key == "stars" else (my["stars_lo"] * (0.9 if key in ("speed", "accuracy") else 1), my["stars_med"] * 1.05)
    cands = []
    for (mp, mods), c in pool.items():
        p, a = c["play"], c["play"]["attr"]
        if mp in my["played"] or not a or not a.get("aim_difficulty") or not lo <= a["star_rating"] <= hi or not ok(p, a, my):
            continue
        cands.append((len(c["who"]), c["ahead"], p.get("pp") or 0, p, a))
    cands.sort(key=lambda x: (-x[0], -x[1], -x[2]))
    out, seen = [], set()
    for k, ahead, _, p, a in cands:
        if p["map"] in seen or p["map"] in used: continue
        seen.add(p["map"]); used.add(p["map"])
        r = rate(set(p.get("mods") or []))
        out.append({"map": p["map"], "title": p.get("title"), "mapper": p.get("mapper"), "mods": [m for m in p.get("mods") or [] if m in ("DT", "NC", "HR", "HD", "EZ", "FL")],
                    "stars": round(a["star_rating"], 2), "length": round((p.get("length") or 0) / r), "bpm": round((p.get("bpm") or 0) * r),
                    "ar": round(eff_ar(p.get("ar"), set(p.get("mods") or [])) or 0, 1), "od": p.get("od"),
                    "speedAim": round(a["speed_difficulty"] / a["aim_difficulty"], 2), "peers": k})
        if len(out) >= n: break
    return {"key": key, "type": name, "stars": [round(lo, 1), round(hi, 1)], "maps": out}


def sessions(plays):
    """Sessions from the play log (a gap of over 30 minutes starts a new one)."""
    plays = sorted((p for p in plays if p.get("at")), key=lambda p: p["at"])
    if len(plays) < 20:
        return None
    t = lambda p: datetime.fromisoformat(p["at"].replace("Z", "+00:00"))
    groups, cur = [], [plays[0]]
    for a, b in zip(plays, plays[1:]):
        if (t(b) - t(a)) > timedelta(minutes=30): groups.append(cur); cur = []
        cur.append(b)
    groups.append(cur)
    per_map = Counter(p["map"] for p in plays)
    out = {"plays": len(plays), "days": len({t(p).astimezone(TZ).date() for p in plays}), "sessions": len(groups),
           "passRate": round(100 * sum(1 for p in plays if p.get("passed")) / len(plays), 1),
           "perMap": round(len(plays) / len(per_map), 1), "maps": len(per_map),
           "mostRetried": [{"title": next(p["title"] for p in plays if p["map"] == m), "attempts": c,
                            "passes": sum(1 for p in plays if p["map"] == m and p.get("passed"))} for m, c in per_map.most_common(3)]}
    # warm-up / fatigue: accuracy of passed plays early vs late in sessions with enough plays
    early, late = [], []
    for g in groups:
        ok = [p for p in g if p.get("passed") and p.get("acc")]
        if len(ok) >= 9:
            k = len(ok) // 3; early += [p["acc"] for p in ok[:k]]; late += [p["acc"] for p in ok[-k:]]
    if len(early) >= 10:
        out["warmup"] = {"early": round(stats.median(early), 2), "late": round(stats.median(late), 2), "n": len(early) + len(late)}
    # time of day (your local time), once there's enough
    if len(plays) >= 150:
        by = defaultdict(list)
        for p in plays:
            by[["night (12–6am)", "morning (6am–12pm)", "afternoon (12–6pm)", "evening (6pm–12am)"][t(p).astimezone(TZ).hour // 6]].append(p)
        out["timeOfDay"] = [{"when": k, "plays": len(v), "passRate": round(100 * sum(1 for p in v if p.get("passed")) / len(v), 1)}
                            for k, v in by.items() if len(v) >= 30]
    return out


def main():
    with open(D("me_plays.json")) as f:
        me = json.load(f)
    now = datetime.now(timezone.utc)
    best = me["best"]
    log = []
    if os.path.exists(D("me_log.jsonl.gz")):
        with gzip.open(D("me_log.jsonl.gz"), "rt") as f:
            log = [json.loads(l) for l in f if l.strip()]
    peers = {}
    if os.path.exists(D("me_peers.json")):
        with open(D("me_peers.json")) as f:
            peers = json.load(f)

    mine = profile(best, now)
    near = [q for q in (profile(p["best"], now) for p in peers.get("players", []) if p["group"] == "near") if q]
    skills = []
    for group, key, label, unit, better, why in AXES:
        vals = sorted(q[key] for q in near if q.get(key) is not None)
        pct = None
        if vals and mine.get(key) is not None:
            below = sum(v < mine[key] for v in vals) if better > 0 else sum(v > mine[key] for v in vals)
            pct = round(100 * (below + 0.5 * sum(v == mine[key] for v in vals)) / len(vals))
        skills.append({"group": group, "key": key, "label": label, "unit": unit, "future": key in FUTURE, "you": mine.get(key), "peerMedian": med(vals),
                       "peerP25": vals[len(vals) // 4] if vals else None, "peerP75": vals[3 * len(vals) // 4] if vals else None,
                       "pct": pct, "why": why})
    weak = [s for s in sorted(skills, key=lambda s: s["pct"] if s["pct"] is not None else 50) if s["pct"] is not None and s["pct"] <= 35 and not s["future"]][:4]
    strong = [s for s in sorted(skills, key=lambda s: -(s["pct"] or 0)) if (s["pct"] or 0) >= 65 and not s["future"]][:3]

    # recommendation pool: peers' top plays (near and a step ahead) with difficulty attributes
    pool = {}
    for p in peers.get("players", []):
        for q in p["best"][:25]:
            if not q.get("attr") or not q.get("map"): continue
            key = (q["map"], "+".join(sorted(m for m in q.get("mods") or [] if m in ("DT", "NC", "HR"))))
            c = pool.setdefault(key, {"play": q, "who": set(), "ahead": 0})
            c["who"].add(p["id"]); c["ahead"] += p["group"] == "ahead"
    my_stars = sorted(p["attr"]["star_rating"] for p in best[:50] if p.get("attr") and p["attr"].get("star_rating"))
    played = {p["map"] for p in best} | {m["map"] for m in me.get("most_played", [])} | {p["map"] for p in log} | {p["map"] for p in me.get("recent", [])}
    my_aim = sorted(p["attr"]["aim_difficulty"] for p in best[:50] if p.get("attr") and p["attr"].get("aim_difficulty"))
    my = {"aim_hi": my_aim[int(len(my_aim) * 0.75)], "played": played, "mappers": {p.get("mapper") for p in best[:50]},
          "stars_lo": my_stars[len(my_stars) // 4], "stars_med": stats.median(my_stars), "stars_hi": my_stars[int(len(my_stars) * 0.9)]}
    used = set()                                         # each map is recommended once
    rec_keys = [k for k in REC_ORDER if k in {s["key"] for s in weak}] or ["stars"]
    why = {s["key"]: s["why"] for s in skills}
    recs = [dict(recs_for(k, mine, my, pool, used), why=why[k]) for k in rec_keys]

    # trend: hardest aim / speed among top plays set each week
    weeks = defaultdict(list)
    for p in best:
        if p.get("at") and p.get("attr") and p["attr"].get("aim_difficulty"):
            d = datetime.fromisoformat(p["at"].replace("Z", "+00:00")).astimezone(TZ).date()
            weeks[(d - timedelta(days=d.weekday())).isoformat()].append(p)
    trend = [{"week": w, "plays": len(v), "aim": round(max(p["attr"]["aim_difficulty"] for p in v), 2),
              "speed": round(max(p["attr"]["speed_difficulty"] for p in v), 2), "stars": round(max(p["attr"]["star_rating"] for p in v), 2)}
             for w, v in sorted(weeks.items())]

    # chokes: top plays with a few misses (an FC there would have been worth more)
    miss = Counter(min(p.get("miss") or 0, 10) for p in best)
    chokes = {"fc": sum(1 for p in best if p.get("fc") or not p.get("miss")), "few": sum(1 for p in best if 1 <= (p.get("miss") or 0) <= 3),
              "many": sum(1 for p in best if (p.get("miss") or 0) > 3), "n": len(best),
              "worst": [{"title": p["title"], "miss": p["miss"], "pp": round(p["pp"]), "acc": p["acc"]} for p in sorted(
                  (p for p in best if 1 <= (p.get("miss") or 0) <= 3), key=lambda p: -p["pp"])[:3]]}

    plays_all = log + [p for p in me.get("recent", []) if (p["at"], p["map"]) not in {(q["at"], q["map"]) for q in log}]
    out = {"at": me.get("pulledAt"), "user": me.get("username"), "peers": {"near": len(near), "ahead": sum(1 for p in peers.get("players", []) if p["group"] == "ahead"),
           "at": peers.get("at"), "ppRange": [round(peers.get("myPP", 0) * 0.85), round(peers.get("myPP", 0) * 1.15)]},
           "skills": skills, "weak": [s["key"] for s in weak], "strong": [s["key"] for s in strong], "recs": recs,
           "sessions": sessions(plays_all), "trend": trend, "chokes": chokes,
           "extra": {"lengthMedian": mine["length"], "longShare": mine["long"], "dtShare": mine["dt"], "hdShare": mine["hd"], "momentum": mine["momentum"],
                     "peerMomentum": med([q["momentum"] for q in near]), "peerDt": med([q["dt"] for q in near]), "peerHd": med([q["hd"] for q in near])}}
    with open(D("me_insights.json"), "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print("weak:", out["weak"], "strong:", out["strong"], "recs:", [(r["type"], len(r["maps"])) for r in recs])


if __name__ == "__main__":
    main()
