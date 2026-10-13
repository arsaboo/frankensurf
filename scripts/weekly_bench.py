"""The weekly benchmark: the same 150 sites (held-out set 7) read every week, by each tool on its own
and by Frankensurf, judged by the same content checks as scripts/toolbench.py.

Run by .github/workflows/weekly-bench.yml on a GitHub machine (a data-centre address, which sites wall
more often than a home connection, so rates sit below the headline benchmark). Free arms always run;
a paid arm runs only when its key is set (FIRECRAWL_API_KEY, ZYTE_API_KEY), and the stitched paid arm
when any paid key is. Writes benchmarks/weekly/<date>/rows.jsonl and summary.json, and updates
benchmarks/weekly/latest.json and history.json, which the docs page reads.

  PYTHONPATH=.:src python scripts/weekly_bench.py [--sample 20] [--where "GitHub Actions"]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import toolbench  # noqa: E402

FREE = ["http", "jina_reader", "local", "camoufox", "scrapling", "patchright", "stitched_free"]
PAID = {"firecrawl": "FIRECRAWL_API_KEY", "zyte": "ZYTE_API_KEY"}
LABELS = {"http": "Plain HTTP", "jina_reader": "Jina Reader", "local": "Headless Chromium",
          "camoufox": "Camoufox", "scrapling": "Scrapling", "patchright": "Patchright",
          "firecrawl": "Firecrawl", "zyte": "Zyte", "stitched_free": "Frankensurf, free tools only",
          "stitched_paid": "Frankensurf, with paid fallbacks"}
OUT = Path(__file__).resolve().parents[1] / "benchmarks" / "weekly"


def _key(name):
    return os.environ.get(name) or os.environ.get("FRANKENSURF_" + name)


def arms():
    paid = [arm for arm, key in PAID.items() if _key(key)]
    return FREE + paid + (["stitched_paid"] if paid else [])


def summarize(rows):
    out = {}
    for arm in dict.fromkeys(row["arm"] for row in rows):
        mine = [row for row in rows if row["arm"] == arm]
        valid = [row for row in mine if row["valid"]]
        latency = sorted(row["latency_ms"] for row in valid)
        costs = [row["cost_usd"] for row in mine if row.get("cost_usd") is not None]
        out[arm] = {"label": LABELS.get(arm, arm), "sites": len(mine), "read": len(valid),
                    "rate": round(len(valid) / len(mine), 4) if mine else None,
                    "median_s": round(statistics.median(latency) / 1000, 1) if latency else None,
                    "p90_s": round(latency[int(len(latency) * 0.9)] / 1000, 1) if latency else None,
                    "cost_usd": round(sum(costs), 4)}
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sample", type=int, help="read only this many sites (a quick check)")
    parser.add_argument("--where", default="GitHub Actions (data-centre address)")
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args(argv)
    import tomllib
    cases = [case for case in toolbench.heldout() if case["set"] == "heldout7"]
    if args.sample and args.sample < len(cases):
        step = len(cases) / args.sample
        cases = [cases[int(index * step)] for index in range(args.sample)]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    folder = OUT / today
    folder.mkdir(parents=True, exist_ok=True)
    rows_path = folder / "rows.jsonl"
    rows_path.unlink(missing_ok=True)
    chosen = arms()
    with tempfile.TemporaryDirectory() as state:
        rows = asyncio.run(toolbench.run(cases, Path(state), rows_path, args.workers, 2.0, chosen))
    package = tomllib.loads((OUT.parents[1] / "pyproject.toml").read_text())["project"]["version"]
    summary = {"date": today, "where": args.where, "version": package,
               "set": "held-out set 7 (150 sites picked blind on 10 October 2026)", "sites": len(cases),
               "arms": summarize(rows)}
    (folder / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    (OUT / "latest.json").write_text(json.dumps(summary, indent=2) + "\n")
    history_path = OUT / "history.json"
    history = json.loads(history_path.read_text()) if history_path.exists() else []
    history = [entry for entry in history if entry["date"] != today]
    history.append({"date": today, "version": summary["version"], "sites": len(cases),
                    "rates": {arm: data["rate"] for arm, data in summary["arms"].items()}})
    history_path.write_text(json.dumps(sorted(history, key=lambda entry: entry["date"]), indent=1) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
