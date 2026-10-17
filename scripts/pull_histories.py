#!/usr/bin/env python3
"""
Pull players' progression data from the osu! API (v2) into data/history.jsonl.gz.

For each player, 2 requests: their profile (pp, play time, plays per month, global rank, 90-day rank history)
and their top 100 scores (pp, date set, map stars, mods). Players are picked at random within play-time groups
from a users.csv (build_model.py --write-users makes one), weighted toward experienced players.

Credentials: set OSU_CLIENT_ID and OSU_CLIENT_SECRET (in GitHub: repo Settings > Secrets > Actions),
or run it locally and it asks (the secret is typed hidden and never saved).

Usage:
    python scripts/pull_histories.py --users .cache/users.csv
    python scripts/pull_histories.py --users .cache/users.csv --plan 600:1e9:400 150:600:500 30:150:400 1:30:200

Already-pulled players are skipped, so it resumes after a stop. ~1 request per second (API guideline: 60/min).
"""
import argparse, csv, getpass, gzip, json, os, random, sys, time, urllib.error, urllib.parse, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = "https://osu.ppy.sh/api/v2"
OUT = os.path.join(ROOT, "data", "history.jsonl.gz")
DEFAULT_PLAN = ["600:1e9:400", "150:600:500", "30:150:400", "1:30:200"]


def token(cid, secret):
    body = urllib.parse.urlencode({"client_id": cid, "client_secret": secret, "grant_type": "client_credentials", "scope": "public"}).encode()
    req = urllib.request.Request("https://osu.ppy.sh/oauth/token", data=body,
                                 headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)["access_token"]
    except urllib.error.HTTPError as e:
        sys.exit(f"Couldn't get an API token ({e.code}). Check the client ID and secret.")


class Api:
    def __init__(self, tok):
        self.tok, self.last = tok, 0.0

    def get(self, path, **params):
        for attempt in range(5):
            wait = 1.05 - (time.time() - self.last)
            if wait > 0: time.sleep(wait)
            self.last = time.time()
            url = f"{API}{path}?{urllib.parse.urlencode(params)}"
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.tok}", "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code == 404: return None
                if e.code == 401: sys.exit("Token rejected (expired?). Run again.")
                if e.code == 429 or e.code >= 500: time.sleep(10 * (attempt + 1)); continue
                raise
            except (urllib.error.URLError, TimeoutError):
                time.sleep(5 * (attempt + 1))
        return None


def done_ids(path):
    if not os.path.exists(path): return set()
    with gzip.open(path, "rt") as f:
        return {json.loads(l)["id"] for l in f if l.strip()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--users", required=True, help="users.csv with user_id, hours, pp")
    ap.add_argument("--plan", nargs="*", default=DEFAULT_PLAN, help="min:max:count groups of play time")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()

    with open(args.users, newline="") as f:
        rows = [(r["user_id"], float(r["hours"])) for r in csv.DictReader(f) if float(r["hours"]) >= 1 and float(r["pp"]) > 0]
    done, rnd, todo = done_ids(args.out), random.Random(args.seed), []
    for g in args.plan:
        lo, hi, n = g.split(":"); lo, hi, n = float(lo), float(hi), int(n)
        group = [u for u, h in rows if lo <= h < hi and u not in done]
        rnd.shuffle(group); todo += group[:n]
        print(f"  {lo:>5.0f}-{hi:<6.0f} h: {min(n, len(group))} new players")
    rnd.shuffle(todo)
    if not todo:
        print("Nothing new to pull."); return
    print(f"{len(done)} already saved, {len(todo)} to go (~{len(todo) * 2.1 / 60:.0f} min)")

    cid = os.environ.get("OSU_CLIENT_ID") or input("Client ID: ").strip()
    secret = os.environ.get("OSU_CLIENT_SECRET") or getpass.getpass("Client Secret (hidden): ").strip()
    api = Api(token(cid, secret))

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    t0 = time.time()
    with gzip.open(args.out, "at") as out:
        for k, uid in enumerate(todo, 1):
            u = api.get(f"/users/{uid}/osu", key="id")
            rec = {"id": uid, "missing": u is None, "pulled": time.strftime("%Y-%m-%d")}
            if u:
                st = u.get("statistics") or {}
                scores = api.get(f"/users/{uid}/scores/best", mode="osu", limit=100, legacy_only=0) or []
                rec.update({
                    "pp": st.get("pp"), "play_time": st.get("play_time"), "play_count": st.get("play_count"),
                    "rank": st.get("global_rank"), "acc": st.get("hit_accuracy"),
                    "rank_history": (u.get("rank_history") or {}).get("data"),
                    "months": [[m["start_date"], m["count"]] for m in u.get("monthly_playcounts") or []],
                    "scores": [{"pp": s.get("pp"), "at": s.get("ended_at") or s.get("created_at"),
                                "sr": (s.get("beatmap") or {}).get("difficulty_rating"),
                                "mods": [m if isinstance(m, str) else m.get("acronym") for m in s.get("mods") or []]}
                               for s in scores if s.get("pp")],
                })
            out.write(json.dumps(rec) + "\n"); out.flush()
            if k % 25 == 0 or k == len(todo):
                print(f"  {k}/{len(todo)}  (~{(time.time() - t0) / k * (len(todo) - k) / 60:.0f} min left)", flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped. Run again to resume.")
