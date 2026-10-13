"""Blind search review tooling: pool, blind and score search results for independent graders.

  prep  ROWS... --arms A B C --queries FILE --out DIR [--per 95]
        Pool each arm's top-5 results for the sampled queries, drop results already in
        the verdict cache, shuffle, strip which tool found them, and write batches.
  merge DIR   Fold DIR/verdicts*.json into the cache (keyed by query id and URL).
  score ROWS... --arms A B C --queries FILE [--compare A B]
        Useful results per query from the cache, per arm and kind, with paired sign tests.
"""
import argparse
import os
import json
import random
from collections import defaultdict
from math import comb
from pathlib import Path

ROOT = Path(os.environ.get("SEARCHREVIEW_DIR", "searchreview"))
CACHE = ROOT / "verdicts.json"
QUERIES = {q["id"]: q for q in json.load(open(Path(__file__).with_name("searchbench-queries.json")))["queries"]}
WINDOW = {"day": "the last day", "week": "the last 7 days", "month": "the last 31 days", "year": "the last 366 days"}


def load_rows(paths, rename=()):
    rows = defaultdict(dict)
    renames = dict(item.split("=") for item in rename)
    for path in paths:
        for line in open(path):
            r = json.loads(line)
            arm = renames.get(f"{path}:{r['arm']}", r["arm"])
            rows[arm][r["query_id"]] = r
    return rows


def cache():
    return json.loads(CACHE.read_text()) if CACHE.exists() else {}


def ckey(q, url):
    return q + " " + url


def prep(args):
    rows, have = load_rows(args.rows, args.rename), cache()
    qs = [line.strip() for line in open(args.queries) if line.strip()]
    pending = {}
    for q in qs:
        for arm in args.arms:
            r = rows[arm].get(q)
            if not r:
                continue
            for x in r["results"][:5]:
                k = ckey(q, x["url"])
                if k in have or k in pending:
                    continue
                pending[k] = {"query": QUERIES[q]["query"], "window": WINDOW.get(QUERIES[q].get("recency") or "", "none"),
                              "url": x["url"], "title": x.get("title") or "", "snippet": (x.get("snippet") or "")[:400],
                              "date_shown": (x.get("date") or "")[:10] or "none", "_key": k}
    items = list(pending.values())
    random.Random(len(items)).shuffle(items)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    keymap = {}
    for i, it in enumerate(items):
        it["id"] = "r%05d" % i
        keymap[it["id"]] = it.pop("_key")
    n = max(1, -(-len(items) // args.per))
    for b in range(n):
        (out / ("batch%d.json" % b)).write_text(json.dumps(items[b::n], indent=1, ensure_ascii=False))
    (out / "key.json").write_text(json.dumps(keymap))
    print(len(items), "new results to review in", n, "batches")


def merge(args):
    out = Path(args.dir)
    keymap = json.loads((out / "key.json").read_text())
    have = cache()
    added = 0
    for f in sorted(out.glob("verdicts*.json")):
        for rid, v in json.loads(f.read_text()).items():
            if rid in keymap:
                have[keymap[rid]] = v
                added += 1
    CACHE.write_text(json.dumps(have))
    print("cache:", len(have), "(added", added, "of", len(keymap), ")")


def sign_p(w, l):
    n, k = w + l, min(w, l)
    return min(1, 2 * sum(comb(n, i) for i in range(k + 1)) / 2 ** n) if n else 1.0


def score(args):
    rows, have = load_rows(args.rows, args.rename), cache()
    qs = [line.strip() for line in open(args.queries) if line.strip()]
    useful = defaultdict(dict)
    missing = 0
    for arm in args.arms:
        for q in qs:
            r = rows[arm].get(q)
            n = 0
            for x in (r or {}).get("results", [])[:5]:
                v = have.get(ckey(q, x["url"]))
                if v is None:
                    missing += 1
                    continue
                n += v["useful"] == "yes"
            useful[arm][q] = (n, len((r or {}).get("results", [])[:5]))
    print("unreviewed results:", missing)
    print("%-20s %7s %8s %9s %6s" % ("arm", "useful", "unuseful", "any", "3+"))
    for arm in args.arms:
        u = useful[arm]
        print("%-20s %7.2f %8.2f %8d/%d %4d/%d" % (arm, sum(a for a, b in u.values()) / len(qs),
              sum(b - a for a, b in u.values()) / len(qs), sum(a >= 1 for a, b in u.values()), len(qs),
              sum(a >= 3 for a, b in u.values()), len(qs)))
    kinds = sorted({QUERIES[q]["category"] for q in qs})
    print("useful by kind:", " ".join("%12s" % k for k in kinds))
    for arm in args.arms:
        print("%-20s" % arm, " ".join("%12.2f" % (sum(useful[arm][q][0] for q in qs if QUERIES[q]["category"] == k) /
              max(1, sum(1 for q in qs if QUERIES[q]["category"] == k))) for k in kinds))
    for a, b in args.compare:
        w = sum(useful[a][q][0] > useful[b][q][0] for q in qs)
        l = sum(useful[a][q][0] < useful[b][q][0] for q in qs)
        print("%s vs %s: more useful on %d, fewer on %d, sign test p = %.4f" % (a, b, w, l, sign_p(w, l)))


parser = argparse.ArgumentParser()
sub = parser.add_subparsers(dest="cmd", required=True)
p = sub.add_parser("prep")
p.add_argument("rows", nargs="+")
p.add_argument("--arms", nargs="+", required=True)
p.add_argument("--queries", required=True)
p.add_argument("--out", required=True)
p.add_argument("--per", type=int, default=95)
p.add_argument("--rename", nargs="*", default=[])
p = sub.add_parser("merge")
p.add_argument("dir")
p = sub.add_parser("score")
p.add_argument("rows", nargs="+")
p.add_argument("--arms", nargs="+", required=True)
p.add_argument("--queries", required=True)
p.add_argument("--compare", nargs=2, action="append", default=[])
p.add_argument("--rename", nargs="*", default=[])
args = parser.parse_args()
{"prep": prep, "merge": merge, "score": score}[args.cmd](args)
