#!/usr/bin/env python3
"""Quick osu! API health check: gets a token and tries a few endpoints, printing each status and the start of any
error body. For when the daily update fails. Reads OSU_CLIENT_ID / OSU_CLIENT_SECRET (and OSU_USERNAME)."""
import json, os, urllib.error, urllib.parse, urllib.request

user = os.environ.get("OSU_USERNAME") or "sakul333"
body = urllib.parse.urlencode({"client_id": os.environ["OSU_CLIENT_ID"], "client_secret": os.environ["OSU_CLIENT_SECRET"],
                               "grant_type": "client_credentials", "scope": "public"}).encode()
tok = json.load(urllib.request.urlopen(urllib.request.Request("https://osu.ppy.sh/oauth/token", data=body,
      headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}), timeout=30))
print("token ok, scopes:", tok.get("scope"), "expires in", tok.get("expires_in"))
for path, ua in [(f"/users/@{user}/osu", None), (f"/users/{user}/osu?key=username", None), (f"/users/@{user}/osu", "osu-pp-curve/1.0"),
                 ("/beatmaps/129891", None), ("/rankings/osu/performance", None)]:
    h = {"Authorization": f"Bearer {tok['access_token']}", "Accept": "application/json", "x-api-version": "20240529"}
    if ua: h["User-Agent"] = ua
    try:
        r = urllib.request.urlopen(urllib.request.Request("https://osu.ppy.sh/api/v2" + path, headers=h), timeout=30)
        d = json.load(r); print(f"{r.status} {path} ua={ua or 'default'} -> {d.get('username') or d.get('version') or list(d)[:3]}")
    except urllib.error.HTTPError as e:
        b = e.read().decode("utf-8", "replace"); import re
        code = re.search(r"Error (?:code:?\s*)?(\d{4})", b); ray = re.search(r"Ray ID: <strong[^>]*>([0-9a-f]+)", b)
        print(f"{e.code} {path} ua={ua or 'default'} -> cloudflare error {code.group(1) if code else '?'} ray {ray.group(1) if ray else '?'}")

import subprocess                                       # same request through curl (different TLS fingerprint)
r = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-H", f"Authorization: Bearer {tok['access_token']}",
                    "-H", "Accept: application/json", f"https://osu.ppy.sh/api/v2/users/@{user}/osu"], capture_output=True, text=True)
print("curl /users:", r.stdout)
