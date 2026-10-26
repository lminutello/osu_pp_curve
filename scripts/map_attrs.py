#!/usr/bin/env python3
"""
Look up osu!'s difficulty numbers (aim, speed, star rating, max combo...) plus basic map info for a list of beatmap
IDs, e.g. to sort a song library into collections. Writes library/map_attrs.json. ~2 requests per map.
Usage:  MAP_IDS="123,456" python scripts/map_attrs.py
"""
import json, os, sys, time, urllib.error, urllib.parse, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from me_plays import Api  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    ids = [int(x) for x in (os.environ.get("MAP_IDS") or "").replace(" ", "").split(",") if x.strip().isdigit()]
    api = Api(os.environ["OSU_CLIENT_ID"], os.environ["OSU_CLIENT_SECRET"])
    out = {}
    for i in range(0, len(ids), 50):                   # map info in batches of 50
        q = "&".join(f"ids[]={x}" for x in ids[i:i + 50])
        for b in (api.call(f"/beatmaps?{q}") or {}).get("beatmaps", []):
            out[b["id"]] = {"stars": b.get("difficulty_rating"), "length": b.get("hit_length"), "bpm": b.get("bpm"), "ar": b.get("ar"),
                            "od": b.get("accuracy"), "cs": b.get("cs"), "circles": b.get("count_circles"), "sliders": b.get("count_sliders"),
                            "status": b.get("status"), "playcount": b.get("playcount"), "passcount": b.get("passcount"), "version": b.get("version")}
    for n, x in enumerate(ids, 1):
        a = ((api.call(f"/beatmaps/{x}/attributes", {"mods": [], "ruleset": "osu"}) or {}).get("attributes") or {})
        out.setdefault(x, {}).update({k: a.get(k) for k in ("star_rating", "aim_difficulty", "speed_difficulty", "slider_factor", "speed_note_count", "max_combo")})
        if n % 50 == 0: print(f"{n}/{len(ids)}", flush=True)
    os.makedirs(os.path.join(ROOT, "library"), exist_ok=True)
    with open(os.path.join(ROOT, "library", "map_attrs.json"), "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"saved {len(out)} maps")


if __name__ == "__main__":
    main()
