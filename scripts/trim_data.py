#!/usr/bin/env python3
"""
Trim data files that only ever grow, keeping what the site and its fits still use. Run yearly by the
"Trim old data" workflow (or by hand). Prints what it would cut first; --dry-run stops there.

What it trims (limits at the top of the file):
  data/dumps/              dump archives older than KEEP_DUMP_MONTHS (the model only uses the newest 3; the
                           projection uses the oldest dump still online, ~6 months back)
  data/drift_pairs.jsonl.gz  real before/after pairs looked up more than KEEP_PAIRS_MONTHS ago (the projection
                           should reflect how players improve now)
  data/me_log.jsonl.gz     your logged plays older than KEEP_LOG_DAYS (sessions and pass rates are about recent play)
  data/me_plays.json       difficulty-attribute cache entries no current top play or peer play uses (they'd simply be
                           looked up again if needed)

Never touched: your progress points and skill snapshots (data/me_history.json, data/me_skills_history.json; tiny, kept forever), the model, ranks, histories
used for star level, and git history (old versions stay in the repo's history either way).

Usage:  python scripts/trim_data.py [--dry-run]
"""
import argparse, gzip, json, os, re
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
D = lambda *p: os.path.join(ROOT, "data", *p)

KEEP_DUMP_MONTHS = 36
KEEP_PAIRS_MONTHS = 36
KEEP_LOG_DAYS = 730


def size(path):
    if os.path.isdir(path):
        return sum(os.path.getsize(os.path.join(path, f)) for f in os.listdir(path))
    return os.path.getsize(path) if os.path.exists(path) else 0


def jsonl(path):
    if not os.path.exists(path):
        return []
    with gzip.open(path, "rt") as f:
        return [json.loads(l) for l in f if l.strip()]


def write_jsonl(path, rows):
    with gzip.open(path, "wt") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--dry-run", action="store_true"); args = ap.parse_args()
    now = datetime.now(timezone.utc)
    before = {p: size(D(p)) for p in ("dumps", "drift_pairs.jsonl.gz", "me_log.jsonl.gz", "me_plays.json")}
    plan = []

    # dump archives
    cutoff = (now - timedelta(days=30.44 * KEEP_DUMP_MONTHS)).strftime("%Y_%m_%d")
    old_dumps = sorted(f for f in os.listdir(D("dumps")) if re.match(r"\d{4}_\d{2}_\d{2}\.csv\.gz$", f) and f[:10] < cutoff) if os.path.isdir(D("dumps")) else []
    plan.append(f"dump archives: remove {len(old_dumps)} older than {KEEP_DUMP_MONTHS} months")

    # real before/after pairs
    pairs = jsonl(D("drift_pairs.jsonl.gz"))
    pcut = (now - timedelta(days=30.44 * KEEP_PAIRS_MONTHS)).strftime("%Y-%m-%d")
    keep_pairs = [r for r in pairs if (r.get("pulled") or "9999") >= pcut]
    plan.append(f"projection pairs: keep {len(keep_pairs):,} of {len(pairs):,} (drop those looked up before {pcut})")

    # play log
    log = jsonl(D("me_log.jsonl.gz"))
    lcut = (now - timedelta(days=KEEP_LOG_DAYS)).strftime("%Y-%m-%d")
    keep_log = [r for r in log if (r.get("at") or "9999") >= lcut]
    plan.append(f"play log: keep {len(keep_log):,} of {len(log):,} plays (drop before {lcut})")

    # attribute cache
    me, cache, keep_cache = {}, {}, {}
    if os.path.exists(D("me_plays.json")):
        with open(D("me_plays.json")) as f:
            me = json.load(f)
        cache = me.get("attrCache", {})
        peers = {}
        if os.path.exists(D("me_peers.json")):
            with open(D("me_peers.json")) as f:
                peers = json.load(f)
        rate = {"DT", "NC", "HT", "DC", "HR", "EZ", "FL"}
        key = lambda p: f"{p.get('map')}|{'+'.join(sorted(m for m in p.get('mods') or [] if m in rate))}"
        used = {key(p) for p in me.get("best", [])} | {key(p) for pl in peers.get("players", []) for p in pl.get("best", [])}
        keep_cache = {k: v for k, v in cache.items() if k in used}
        plan.append(f"attribute cache: keep {len(keep_cache):,} of {len(cache):,} entries")

    print("\n".join(plan))
    if args.dry_run:
        print("dry run: nothing changed"); return

    for f in old_dumps:
        os.remove(D("dumps", f))
    if len(keep_pairs) != len(pairs):
        write_jsonl(D("drift_pairs.jsonl.gz"), keep_pairs)
    if len(keep_log) != len(log):
        write_jsonl(D("me_log.jsonl.gz"), keep_log)
    if me and len(keep_cache) != len(cache):
        me["attrCache"] = keep_cache
        with open(D("me_plays.json"), "w") as f:
            json.dump(me, f, separators=(",", ":"))
    after = {p: size(D(p)) for p in before}
    for p in before:
        print(f"  {p}: {before[p] / 1e6:.2f} MB -> {after[p] / 1e6:.2f} MB")


if __name__ == "__main__":
    main()
