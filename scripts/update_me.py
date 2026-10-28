#!/usr/bin/env python3
"""
Save one player's current stats to data/me.json (the site's "my stats" button) and keep their progress over time in
data/me_history.json (the "My progress" curve).

Each run (daily, by the "Update my stats" workflow):
  - 3 API requests: a token, the profile, and the player's top 100 plays
  - me.json: the latest pp / play time / rank, written only if something changed
  - me_history.json:
      points   one [time, hours, pp, rank, accuracy] per pull where pp or play time changed; kept forever
      rebuilt  the player's past before recording started, [hours, pp] per month: past pp rebuilt from the dates of
               their top 100 plays, past play time from plays per month (the same method the site used to measure
               other players' histories; rough, since it can't see scores that were later beaten)

Reads OSU_CLIENT_ID / OSU_CLIENT_SECRET from the environment and the player from OSU_USERNAME (default: sakul333).
Exits quietly if the secrets aren't set.

Usage:  python scripts/update_me.py
"""
import json, os, sys, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fit_histories import trajectory  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "me.json")
HIST = os.path.join(ROOT, "data", "me_history.json")


def get(url, tok):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}", "Accept": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=30))


def main():
    cid, secret = os.environ.get("OSU_CLIENT_ID"), os.environ.get("OSU_CLIENT_SECRET")
    user = os.environ.get("OSU_USERNAME") or "sakul333"
    if not cid or not secret:
        print("OSU_CLIENT_ID / OSU_CLIENT_SECRET aren't set; skipping."); return
    body = urllib.parse.urlencode({"client_id": cid, "client_secret": secret, "grant_type": "client_credentials", "scope": "public"}).encode()
    req = urllib.request.Request("https://osu.ppy.sh/oauth/token", data=body,
                                 headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"})
    try:
        token = json.load(urllib.request.urlopen(req, timeout=30))["access_token"]
    except urllib.error.HTTPError as e:
        sys.exit(f"Couldn't get an API token ({e.code}). Check the OSU_CLIENT_ID and OSU_CLIENT_SECRET secrets.")
    try:
        u = get(f"https://osu.ppy.sh/api/v2/users/{urllib.parse.quote('@' + user)}/osu", token)
    except urllib.error.HTTPError as e:
        sys.exit(f"Couldn't load player '{user}' ({e.code}).")
    st = u.get("statistics") or {}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    me = {
        "username": u.get("username", user),
        "pp": round(st.get("pp") or 0, 2),
        "hours": round((st.get("play_time") or 0) / 3600, 3),
        "rank": st.get("global_rank"),
        "acc": st.get("hit_accuracy"),
    }

    # ---- progress history
    hist = {"username": me["username"], "points": [], "rebuilt": []}
    if os.path.exists(HIST):
        with open(HIST) as f:
            hist = json.load(f)
    if hist.get("username") != me["username"]:            # a different player now: start their own history
        hist = {"username": me["username"], "points": [], "rebuilt": []}
    # when the account was made and plays per month: lets the site put rough dates on the estimated past
    hist["joined"] = u.get("join_date")
    hist["months"] = [[m["start_date"], m["count"]] for m in u.get("monthly_playcounts") or []]
    last = hist["points"][-1] if hist["points"] else None
    if me["pp"] and me["hours"] and (not last or abs(last[1] - me["hours"]) > 1e-3 or abs(last[2] - me["pp"]) > 1e-2):
        hist["points"].append([now, me["hours"], me["pp"], me["rank"], me["acc"]])
    try:                                                   # rebuild the past from dated top plays (best effort)
        scores = get(f"https://osu.ppy.sh/api/v2/users/{u['id']}/scores/best?mode=osu&limit=100&legacy_only=0", token) or []
        rec = {"pp": st.get("pp"), "play_time": st.get("play_time"), "play_count": st.get("play_count"),
               "months": [[m["start_date"], m["count"]] for m in u.get("monthly_playcounts") or []],
               "scores": [{"pp": s.get("pp"), "at": s.get("ended_at") or s.get("created_at")} for s in scores if s.get("pp")]}
        first_h = min([p[1] for p in hist["points"]] or [me["hours"]])
        rebuilt = [[round(h, 2), round(p, 1)] for h, p, _ in trajectory(rec, "plays") if h < first_h * 0.98]
        if rebuilt:
            hist["rebuilt"], hist["rebuiltAt"] = rebuilt, now[:10]
    except Exception as e:                                 # the history still gets today's point
        print(f"couldn't rebuild the past ({e})")
    with open(HIST, "w") as f:
        json.dump(hist, f, separators=(",", ":"))

    old = {}
    if os.path.exists(OUT):
        with open(OUT) as f:
            old = json.load(f)
    if all(old.get(k) == v for k, v in me.items()):
        print(f"{me['username']}: no change ({me['pp']} pp)."); return
    me["updatedAt"] = now
    with open(OUT, "w") as f:
        json.dump(me, f)
    print(f"{me['username']}: {me['pp']} pp, {me['hours']:.1f} h, rank #{me['rank']}; {len(hist['points'])} recorded points, "
          f"{len(hist['rebuilt'])} rebuilt months")


if __name__ == "__main__":
    main()
