#!/usr/bin/env python3
"""
Pull independent data for checking the site's accuracy (run by the "Accuracy audit" workflow).

  1. every column of the stats tables in the newest dumps -> audit/dumps/<date>_stats.csv.gz, plus each dump's
     file list and table schemas -> audit/dumps/<date>_schema.txt
  2. current stats for a random sample of the dumps' players, spread evenly over pp levels so the extremes are
     covered too -> audit/validation.jsonl.gz (1 API request per player)

Credentials from OSU_CLIENT_ID / OSU_CLIENT_SECRET.
  or, with --then: every player of one older dump (e.g. 12 months back) -> audit/then_<date>.jsonl.gz, which gives
     real before/after pairs for checking the projection

Usage:  python scripts/audit_pull.py --players 3000
        python scripts/audit_pull.py --then 12      # the dump closest to 12 months ago, all ~10,000 players
"""
import argparse, csv, gzip, io, json, os, random, re, sys, tarfile, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_model import fetch, latest_dumps, parse_values  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "audit")


def dump_tables(path, date):
    os.makedirs(os.path.join(OUT, "dumps"), exist_ok=True)
    schema, users = [], {}
    with tarfile.open(path, "r|bz2") as tar:
        for m in tar:
            schema.append(f"{m.name}\t{m.size}")
            if not m.isfile() or not m.name.endswith(".sql"):
                continue
            fh = tar.extractfile(m); first = fh.read(1 << 16).decode("utf-8", "replace")
            head = re.search(r"CREATE TABLE `?(\w+)`?\s*\((.*?)\)\s*ENGINE", first, re.S)
            if not head:
                continue
            cols = re.findall(r"^\s*`(\w+)`", head.group(2), re.M)
            schema.append(f"  {head.group(1)}: {', '.join(cols)}")
            # keep whole tables only for the per-user ones (small); scores/beatmaps just get their schema
            if re.fullmatch(r"osu_user_stats\w*|sample_users", head.group(1)):
                sql = first + fh.read().decode("utf-8", "replace")
                rows = [r for st in re.finditer(r"INSERT INTO `?\w+`?(?:\s*\([^)]*\))?\s+VALUES\s*(.*?);\s*$", sql, re.S | re.M)
                        for r in parse_values(st.group(1)) if len(r) == len(cols)]
                schema.append(f"    {len(rows):,} rows")
                with gzip.open(os.path.join(OUT, "dumps", f"{date}_{head.group(1)}.csv.gz"), "wt", newline="") as f:
                    w = csv.writer(f); w.writerow(cols); w.writerows(rows)
                if "stats" in head.group(1):
                    ix = cols.index("user_id"); pi = cols.index("rank_score")
                    for r in rows:
                        users[r[ix]] = float(r[pi] or 0)
    with open(os.path.join(OUT, "dumps", f"{date}_schema.txt"), "w") as f:
        f.write("\n".join(schema) + "\n")
    return users


class Api:
    def __init__(self):
        cid, secret = os.environ.get("OSU_CLIENT_ID"), os.environ.get("OSU_CLIENT_SECRET")
        if not cid or not secret:
            sys.exit("OSU_CLIENT_ID / OSU_CLIENT_SECRET aren't set.")
        body = urllib.parse.urlencode({"client_id": cid, "client_secret": secret, "grant_type": "client_credentials", "scope": "public"}).encode()
        r = urllib.request.Request("https://osu.ppy.sh/oauth/token", data=body,
                                   headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"})
        self.tok, self.last, self.born = json.load(urllib.request.urlopen(r, timeout=30))["access_token"], 0.0, time.time()
        self.cid, self.secret = cid, secret

    def get(self, path):
        for attempt in range(5):
            wait = 1.05 - (time.time() - self.last)
            if wait > 0: time.sleep(wait)
            self.last = time.time()
            try:
                r = urllib.request.Request("https://osu.ppy.sh/api/v2" + path,
                                           headers={"Authorization": f"Bearer {self.tok}", "Accept": "application/json"})
                return json.load(urllib.request.urlopen(r, timeout=30))
            except urllib.error.HTTPError as e:
                if e.code == 404: return None
                if e.code == 429 or e.code >= 500: time.sleep(10 * (attempt + 1)); continue
                if e.code == 401: self.__init__(); continue
                raise
            except (urllib.error.URLError, TimeoutError):
                time.sleep(5 * (attempt + 1))
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--players", type=int, default=3000)
    ap.add_argument("--count", type=int, default=3)
    ap.add_argument("--cache", default=os.path.join(ROOT, ".cache"))
    ap.add_argument("--then", type=float, help="pull every player of the dump closest to this many months ago")
    args = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)

    if args.then:
        html = urllib.request.urlopen(urllib.request.Request("https://data.ppy.sh/", headers={"User-Agent": "osu-playtime-curve"}), timeout=60).read().decode()
        dates = sorted(set(re.findall(r"(\d{4}_\d{2}_\d{2})_performance_osu_random_10000\.tar\.bz2", html)))
        print("dumps available:", ", ".join(dates), flush=True)
        now = datetime.now(timezone.utc)
        age = lambda d: (now - datetime.strptime(d, "%Y_%m_%d").replace(tzinfo=timezone.utc)).days / 30.44
        d = min(dates, key=lambda d: abs(age(d) - args.then))
        print(f"using {d} ({age(d):.1f} months ago)", flush=True)
        users = dump_tables(fetch(d, args.cache), d)
        pull(sorted(users), os.path.join(OUT, f"then_{d}.jsonl.gz"))
        return

    users = {}
    for d in sorted(latest_dumps(args.count)):
        print("dump", d, flush=True)
        users.update(dump_tables(fetch(d, args.cache), d))
    print(f"{len(users):,} players in the dumps", flush=True)

    # even coverage of pp: 25 equal-width bins in ln(pp) (players with 0 pp get their own bin)
    rng = random.Random(7)
    pos = [(u, p) for u, p in users.items() if p > 0]
    import math
    lo, hi = math.log(min(p for _, p in pos)), math.log(max(p for _, p in pos))
    bins = {}
    for u, p in pos:
        bins.setdefault(min(24, int((math.log(p) - lo) / (hi - lo) * 25)), []).append(u)
    zero = [u for u, p in users.items() if p <= 0]
    per = args.players // 26
    pick = rng.sample(zero, min(per, len(zero)))
    for b in sorted(bins):
        pick += rng.sample(bins[b], min(per, len(bins[b])))
    rest = [u for u in users if u not in set(pick)]
    pick += rng.sample(rest, max(0, args.players - len(pick)))       # top up with plain random players

    pull(pick, os.path.join(OUT, "validation.jsonl.gz"))


def pull(pick, path):
    print(f"pulling {len(pick):,} players -> {path}", flush=True)
    api = Api()
    with gzip.open(path, "wt") as f:
        for i, u in enumerate(pick):
            d = api.get(f"/users/{u}/osu?key=id")
            if d is None:
                f.write(json.dumps({"id": u, "missing": True}) + "\n"); continue
            st = d.get("statistics") or {}
            f.write(json.dumps({
                "id": u, "pulled": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "pp": st.get("pp"), "play_time": st.get("play_time"), "rank": st.get("global_rank"),
                "play_count": st.get("play_count"), "acc": st.get("hit_accuracy"), "ranked_score": st.get("ranked_score"),
                "is_ranked": st.get("is_ranked"), "level": (st.get("level") or {}).get("current"),
                "country_rank": st.get("country_rank"), "active": d.get("is_active"), "last_visit": d.get("last_visit"),
                "join": d.get("join_date"), "restricted": d.get("is_restricted"),
                "rank_hist": ((d.get("rank_history") or {}).get("data") or [])[-90:],
                "months": [[m["start_date"], m["count"]] for m in d.get("monthly_playcounts") or []],
            }) + "\n")
            if (i + 1) % 100 == 0:
                f.flush(); print(f"  {i + 1:,}/{len(pick):,}", flush=True)
    print("done")


if __name__ == "__main__":
    main()
