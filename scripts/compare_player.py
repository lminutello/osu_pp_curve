#!/usr/bin/env python3
"""
Skill profile of any player, next to yours, using the same numbers as the "Skills vs peers" section.
Prints a table to the log; saves nothing. Percentiles are against your current peer group (players near YOUR pp),
so they're only like-for-like when the other player's pp is close to yours. With PEER_PP set, a fresh group of ~30
players within 15% of that pp is pulled instead and both of you are ranked against it.

Reads OSU_CLIENT_ID / OSU_CLIENT_SECRET, COMPARE_USER and optional PEER_PP. About 30 requests (~30 seconds);
with PEER_PP roughly 10-15 minutes.
Usage:  COMPARE_USER=name python scripts/compare_player.py
"""
import json, os, sys, urllib.parse
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from me_plays import Api, play, attributes, pull_peers  # noqa: E402
from me_insights import AXES, profile, D     # noqa: E402


OUT = []
def say(line=""):
    print(line); OUT.append(line)


def main():
    who = (os.environ.get("COMPARE_USER") or "").strip()
    if not who:
        sys.exit("No player given.")
    api = Api(os.environ["OSU_CLIENT_ID"], os.environ["OSU_CLIENT_SECRET"])
    u = api.call(f"/users/{urllib.parse.quote('@' + who)}/osu")
    if not u:
        sys.exit(f"Couldn't find player '{who}'.")
    st = u.get("statistics") or {}
    with open(D("me_plays.json")) as f:
        me = json.load(f)
    cache = me.get("attrCache", {})                         # maps you share cost nothing
    best = [play(s) for s in api.call(f"/users/{u['id']}/scores/best?mode=osu&limit=50&legacy_only=0") or []]
    for p in best[:25]:
        p["attr"] = attributes(api, cache, p)
    now = datetime.now(timezone.utc)
    them, mine = profile(best, now), profile(me["best"], now)
    if not them:
        sys.exit(f"{u['username']} doesn't have enough top plays to profile.")
    them["accuracy"] = st.get("hit_accuracy") or them["accuracy"]
    mine["accuracy"] = (me.get("profile") or {}).get("acc") or mine["accuracy"]
    peer_pp = float(os.environ.get("PEER_PP") or 0)
    if peer_pp:
        peers = pull_peers(api, cache, peer_pp, u["id"], n_near=30, n_ahead=0)
    else:
        with open(D("me_peers.json")) as f:
            peers = json.load(f)
    near = []
    for p in peers.get("players", []):
        q = profile(p["best"], now) if p["group"] == "near" else None
        if q:
            q["accuracy"] = p.get("acc") or q["accuracy"]
            near.append(q)

    def pct(key, v):
        vals = [q[key] for q in near if q.get(key) is not None]
        if v is None or not vals: return "—"
        return f"{round(100 * (sum(x < v for x in vals) + 0.5 * sum(x == v for x in vals)) / len(vals))}th"

    fmt = {"num": "{:.2f}", "sec": "{:.0f}s", "pct": lambda v: f"{100 * v:.0f}%", "acc": "{:.2f}%", "stars": "{:.2f}★"}
    show = lambda unit, v: "—" if v is None else (fmt[unit](v) if callable(fmt[unit]) else fmt[unit].format(v))
    say(f"\n{u['username']}: {st.get('pp'):.0f} pp, {(st.get('play_time') or 0) / 3600:.0f} h, rank #{st.get('global_rank')}")
    say(f"{me['username']}: {me['profile']['pp']:.0f} pp, {me['profile']['hours']:.0f} h")
    say(f"(percentiles vs {len(near)} players near {f"{peer_pp:.0f}" if peer_pp else me['username'] + "'s"} pp)\n")
    say(f"{'skill':20}{u['username']:>22}{me['username']:>22}")
    for group, key, label, unit, better, why in AXES:
        say(f"{group:20}{show(unit, them.get(key)) + ' (' + pct(key, them.get(key)) + ')':>22}"
              f"{show(unit, mine.get(key)) + ' (' + pct(key, mine.get(key)) + ')':>22}")
    say(f"{'HD in top 50':20}{100 * them['hd']:>21.0f}%{100 * mine['hd']:>21.0f}%")
    say(f"{'DT in top 50':20}{100 * them['dt']:>21.0f}%{100 * mine['dt']:>21.0f}%")


if __name__ == "__main__":
    main()
    if os.environ.get("GITHUB_ACTIONS"):                    # also as a run annotation, readable without the log
        print("::notice title=Skill comparison::" + "%0A".join(l.replace("%", "%25").replace("\n", "%0A") for l in OUT))
