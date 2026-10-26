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
import json, os, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "me_plays.json")
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
        key_mods = sorted(m for m in p["mods"] if m in {"DT", "NC", "HT", "DC", "HR", "EZ", "FL"})
        key = f"{p['map']}|{'+'.join(key_mods)}"
        if key not in cache and p["map"]:
            r = api.call(f"/beatmaps/{p['map']}/attributes", {"mods": key_mods, "ruleset": "osu"})
            a = (r or {}).get("attributes") or {}
            cache[key] = {k: a.get(k) for k in ("star_rating", "aim_difficulty", "speed_difficulty", "slider_factor",
                                                 "speed_note_count", "approach_rate", "overall_difficulty", "max_combo")}
        p["attr"] = cache.get(key)
    st = u.get("statistics") or {}
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
