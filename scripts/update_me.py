#!/usr/bin/env python3
"""
Save one player's current stats to data/me.json, for the site's "my stats" button.

One API request (plus a token request). Reads OSU_CLIENT_ID / OSU_CLIENT_SECRET from the environment
and the player from OSU_USERNAME (default: sakul333). Exits quietly if the secrets aren't set.

Usage:  python scripts/update_me.py
"""
import json, os, sys, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "me.json")


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
    req = urllib.request.Request(f"https://osu.ppy.sh/api/v2/users/{urllib.parse.quote('@' + user)}/osu",
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    try:
        u = json.load(urllib.request.urlopen(req, timeout=30))
    except urllib.error.HTTPError as e:
        sys.exit(f"Couldn't load player '{user}' ({e.code}).")
    st = u.get("statistics") or {}
    me = {
        "username": u.get("username", user),
        "pp": round(st.get("pp") or 0, 2),
        "hours": round((st.get("play_time") or 0) / 3600, 3),
        "rank": st.get("global_rank"),
        "acc": st.get("hit_accuracy"),
    }
    old = {}
    if os.path.exists(OUT):
        with open(OUT) as f:
            old = json.load(f)
    if all(old.get(k) == v for k, v in me.items()):
        print(f"{me['username']}: no change ({me['pp']} pp)."); return
    me["updatedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(OUT, "w") as f:
        json.dump(me, f)
    print(f"{me['username']}: {me['pp']} pp, {me['hours']:.1f} h, rank #{me['rank']}")


if __name__ == "__main__":
    main()
