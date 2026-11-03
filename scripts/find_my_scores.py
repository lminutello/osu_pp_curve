#!/usr/bin/env python3
"""
Find your own submitted scores on every version of a song: searches osu!'s beatmap listing for SEARCH (all
statuses), then asks for your scores on each osu! standard difficulty. Prints the result, also as a run
annotation. Only scores osu! keeps online show up (ranked, approved, loved or qualified maps, played online).
Reads OSU_CLIENT_ID / OSU_CLIENT_SECRET, SEARCH and OSU_USERNAME (default sakul333). ~1 request per difficulty.
Usage:  SEARCH="freedom dive" python scripts/find_my_scores.py
"""
import os, sys, urllib.parse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from me_plays import Api  # noqa: E402

OUT = []
def say(line=""):
    print(line); OUT.append(line)


def main():
    q, user = os.environ["SEARCH"].strip(), os.environ.get("OSU_USERNAME") or "sakul333"
    api = Api(os.environ["OSU_CLIENT_ID"], os.environ["OSU_CLIENT_SECRET"])
    uid = (api.call(f"/users/{urllib.parse.quote('@' + user)}/osu") or {})["id"]
    sets, cursor = [], None
    for _ in range(4):                                     # up to ~200 sets
        r = api.call(f"/beatmapsets/search?q={urllib.parse.quote(q)}&m=0&s=any" + (f"&cursor_string={cursor}" if cursor else "")) or {}
        sets += r.get("beatmapsets") or []
        cursor = r.get("cursor_string")
        if not cursor: break
    say(f"{len(sets)} sets match '{q}'")
    found = 0
    for s in sets:
        for b in s.get("beatmaps") or []:
            if b.get("mode") != "osu": continue
            sc = (api.call(f"/beatmaps/{b['id']}/scores/users/{uid}/all?ruleset=osu") or {}).get("scores") or []
            for x in sc:
                found += 1
                mods = "+".join(m if isinstance(m, str) else m.get("acronym") for m in x.get("mods") or []) or "NM"
                say(f"{s['artist']} - {s['title']} (set {s['id']}, by {s['creator']}, {s.get('status')}) [{b['version']}] {b['difficulty_rating']}★ "
                    f"-> {x.get('rank')} {100 * (x.get('accuracy') or 0):.2f}% {mods} {(x.get('statistics') or {}).get('miss', 0)}x "
                    f"{'pass' if x.get('passed', True) else 'fail'} {x.get('ended_at') or x.get('created_at')} "
                    f"https://osu.ppy.sh/beatmapsets/{s['id']}#osu/{b['id']}")
    say(f"{found} score(s) found")


if __name__ == "__main__":
    main()
    if os.environ.get("GITHUB_ACTIONS"):
        print("::notice title=My scores::" + "%0A".join(l.replace("%", "%25").replace("\n", "%0A") for l in OUT))
