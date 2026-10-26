#!/usr/bin/env python3
"""
Save the details of one player's plays to data/me_plays.json, for looking at how they play, not just results.

  best         top 100 plays: map (stars, length, BPM, AR/OD/CS, circles/sliders), mods, accuracy, misses, combo, pp, date,
               plus the map's aim and speed difficulty with those mods (from the API's difficulty attributes)
  recent       plays and fails from the last 24 hours (the API keeps no more)
  most_played  the maps they've played most

Difficulty attributes are cached in the file, so after the first run only new top plays cost a request.
Reads OSU_CLIENT_ID / OSU_CLIENT_SECRET and OSU_USERNAME (default sakul333). ~1 request per second.
Usage:  python scripts/me_plays.py
"""
import gzip, json, os, random, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "me_plays.json")
LOG = os.path.join(ROOT, "data", "me_log.jsonl.gz")          # every play seen in a daily pull, kept forever
PEERS = os.path.join(ROOT, "data", "me_peers.json")           # players near your pp, refreshed weekly
HISTORY = os.path.join(ROOT, "data", "history.jsonl.gz")      # where peer candidates come from
RATE_MODS = {"DT", "NC", "HT", "DC", "HR", "EZ", "FL"}
API = "https://osu.ppy.sh/api/v2"


class Api:
    def __init__(self, cid, secret):
        body = urllib.parse.urlencode({"client_id": cid, "client_secret": secret, "grant_type": "client_credentials", "scope": "public"}).encode()
        r = urllib.request.Request("https://osu.ppy.sh/oauth/token", data=body,
                                   headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"})
        self.tok, self.last = json.load(urllib.request.urlopen(r, timeout=30))["access_token"], 0.0

    def call(self, path, data=None):
        for attempt in range(4):
            wait = 1.05 - (time.time() - self.last)
            if wait > 0: time.sleep(wait)
            self.last = time.time()
            try:
                req = urllib.request.Request(API + path, data=json.dumps(data).encode() if data is not None else None,
                                             headers={"Authorization": f"Bearer {self.tok}", "Accept": "application/json",
                                                      "Content-Type": "application/json", "x-api-version": "20240529"})
                return json.load(urllib.request.urlopen(req, timeout=30))
            except urllib.error.HTTPError as e:
                if e.code == 404: return None
                time.sleep(5 * (attempt + 1))
            except (urllib.error.URLError, TimeoutError):
                time.sleep(5 * (attempt + 1))
        return None


def mods_of(s):
    return [m if isinstance(m, str) else m.get("acronym") for m in s.get("mods") or []]


def play(s):
    b, bs, st = s.get("beatmap") or {}, s.get("beatmapset") or {}, s.get("statistics") or {}
    return {
        "pp": s.get("pp"), "acc": round(100 * (s.get("accuracy") or 0), 2), "rank": s.get("rank"), "passed": s.get("passed", True),
        "combo": s.get("max_combo"), "fc": bool(s.get("legacy_perfect") or s.get("is_perfect_combo")),
        "miss": st.get("miss", st.get("count_miss", 0)) or 0, "n100": st.get("ok", st.get("count_100", 0)) or 0,
        "n50": st.get("meh", st.get("count_50", 0)) or 0, "mods": mods_of(s), "at": s.get("ended_at") or s.get("created_at"),
        "map": b.get("id"), "title": f"{bs.get('artist', '')} - {bs.get('title', '')} [{b.get('version', '')}]", "mapper": bs.get("creator"),
        "stars": b.get("difficulty_rating"), "length": b.get("hit_length") or b.get("total_length"), "bpm": b.get("bpm"),
        "ar": b.get("ar"), "od": b.get("accuracy"), "cs": b.get("cs"), "hp": b.get("drain"),
        "circles": b.get("count_circles"), "sliders": b.get("count_sliders"), "status": b.get("status"),
    }


def attributes(api, cache, p):
    """Difficulty attributes (aim, speed, star rating, max combo...) of a play's map with its rate/difficulty mods."""
    key_mods = sorted(m for m in p["mods"] if m in RATE_MODS)
    key = f"{p['map']}|{'+'.join(key_mods)}"
    if key not in cache and p["map"]:
        r = api.call(f"/beatmaps/{p['map']}/attributes", {"mods": key_mods, "ruleset": "osu"})
        a = (r or {}).get("attributes") or {}
        cache[key] = {k: a.get(k) for k in ("star_rating", "aim_difficulty", "speed_difficulty", "slider_factor",
                                             "speed_note_count", "max_combo")}
    return cache.get(key)


def pull_peers(api, cache, my_pp, my_id, n_near=40, n_ahead=20, top=25):
    """Top plays of ~40 players within 15% of your pp and ~20 a step ahead (1.3-1.8x), picked at random from the
    history sample (current pp re-checked). Attributes for their top 25 plays (cached, so shared maps cost nothing)."""
    with gzip.open(HISTORY, "rt") as f:
        recs = [json.loads(l) for l in f if l.strip()]
    rng = random.Random(int(time.time()) // 604800)
    pick = lambda lo, hi, n: rng.sample([r["id"] for r in recs if r.get("pp") and lo <= r["pp"] <= hi and r["id"] != my_id],
                                        min(n, sum(1 for r in recs if r.get("pp") and lo <= r["pp"] <= hi and r["id"] != my_id)))
    groups = {"near": pick(my_pp * 0.8, my_pp * 1.25, int(n_near * 1.3)), "ahead": pick(my_pp * 1.3, my_pp * 1.8, int(n_ahead * 1.3))}
    out = {"at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "myPP": my_pp, "players": []}
    for group, ids in groups.items():
        lo, hi = (0.85, 1.15) if group == "near" else (1.3, 1.8)
        got = 0
        for pid in ids:
            if got >= (n_near if group == "near" else n_ahead): break
            u = api.call(f"/users/{pid}/osu?key=id")
            st = (u or {}).get("statistics") or {}
            if not st.get("pp") or not lo <= st["pp"] / my_pp <= hi:
                continue
            best = [play(s) for s in api.call(f"/users/{pid}/scores/best?mode=osu&limit=50&legacy_only=0") or []]
            for p in best[:top]:
                p["attr"] = attributes(api, cache, p)
            keep = ("pp", "acc", "fc", "miss", "mods", "at", "map", "title", "mapper", "stars", "length", "bpm", "ar", "od", "cs", "attr")
            best = [{k: p.get(k) for k in keep} for p in best]
            out["players"].append({"id": pid, "group": group, "pp": st.get("pp"), "hours": round((st.get("play_time") or 0) / 3600, 1),
                                   "acc": st.get("hit_accuracy"), "plays": st.get("play_count"), "best": best})
            got += 1
        print(f"peers ({group}): {got}", flush=True)
    return out


def main():
    cid, secret = os.environ.get("OSU_CLIENT_ID"), os.environ.get("OSU_CLIENT_SECRET")
    user = os.environ.get("OSU_USERNAME") or "sakul333"
    if not cid or not secret:
        print("OSU_CLIENT_ID / OSU_CLIENT_SECRET aren't set; skipping."); return
    api = Api(cid, secret)
    u = api.call(f"/users/{urllib.parse.quote('@' + user)}/osu")
    if not u:
        sys.exit(f"Couldn't load player '{user}'.")
    uid = u["id"]
    st = u.get("statistics") or {}
    old = {}
    if os.path.exists(OUT):
        with open(OUT) as f:
            old = json.load(f)
    cache = old.get("attrCache", {}) if old.get("username") == u.get("username") else {}
    best = [play(s) for s in api.call(f"/users/{uid}/scores/best?mode=osu&limit=100&legacy_only=0") or []]
    recent = [play(s) for s in api.call(f"/users/{uid}/scores/recent?mode=osu&limit=100&include_fails=1&legacy_only=0") or []]
    most = [{"map": (m.get("beatmap") or {}).get("id"), "count": m.get("count"),
             "title": f"{(m.get('beatmapset') or {}).get('artist', '')} - {(m.get('beatmapset') or {}).get('title', '')} [{(m.get('beatmap') or {}).get('version', '')}]",
             "stars": (m.get("beatmap") or {}).get("difficulty_rating"), "length": (m.get("beatmap") or {}).get("hit_length")}
            for m in api.call(f"/users/{uid}/beatmapsets/most_played?limit=50") or []]
    # aim / speed difficulty of each top play with its mods (only rate- and difficulty-changing mods matter)
    for p in best:
        p["attr"] = attributes(api, cache, p)

    # ---- the play log: recent plays only cover the last 24 hours, so keep every one we see
    seen, log = set(), []
    if os.path.exists(LOG):
        with gzip.open(LOG, "rt") as f:
            for line in f:
                if line.strip():
                    r = json.loads(line); log.append(r); seen.add((r.get("at"), r.get("map")))
    new = [p for p in recent if (p["at"], p["map"]) not in seen]
    with gzip.open(LOG, "at") as f:
        for p in sorted(new, key=lambda p: p["at"] or ""):
            f.write(json.dumps(p, separators=(",", ":")) + "\n")
    print(f"play log: +{len(new)} plays ({len(log) + len(new)} total)")

    # ---- peers: players near your pp (and a step ahead), from the history sample, refreshed weekly
    peers = {}
    if os.path.exists(PEERS):
        with open(PEERS) as f:
            peers = json.load(f)
    fresh = peers.get("at") and datetime.now(timezone.utc) - datetime.fromisoformat(peers["at"].replace("Z", "+00:00")) < timedelta(days=7)
    if not fresh and os.path.exists(HISTORY) and st.get("pp"):
        peers = pull_peers(api, cache, st["pp"], uid)
        with open(PEERS, "w") as f:
            json.dump(peers, f, separators=(",", ":"))
    out = {"username": u.get("username"), "pulledAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "profile": {"pp": st.get("pp"), "hours": round((st.get("play_time") or 0) / 3600, 2), "plays": st.get("play_count"),
                       "acc": st.get("hit_accuracy"), "max_combo": st.get("maximum_combo"), "grades": st.get("grade_counts"),
                       "join": u.get("join_date"), "playstyle": u.get("playstyle")},
           "best": best, "recent": recent, "most_played": most, "attrCache": cache}
    with open(OUT, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"{out['username']}: {len(best)} top plays, {len(recent)} recent, {len(most)} most played")


if __name__ == "__main__":
    main()
