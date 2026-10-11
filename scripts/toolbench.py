"""Tool benchmark: is the stitched router better than any one tool?

Every arm reads exactly the same URLs and is judged by the site benchmark's
content checks:

  main     the URLs of the latest paid site-benchmark run (search cases plus
           the listing links they led to), frozen so every arm reads the same
           pages; search cases keep their link check but follow nothing
  heldout  scripts/toolbench-heldout.json: sites no FrankenSurf change or route
           seed was tuned on
  heldout2 scripts/toolbench-heldout2.json: a second untouched set, chosen
           after the first held-out run, that validates changes made since
  heldout4 scripts/toolbench-heldout4.json: 139 more, chosen before launch
  heldout5 scripts/toolbench-heldout5.json: 150 more, chosen after v0.29 for
           fresh accuracy figures (scripts/make_heldout5.py)
  heldout6 scripts/toolbench-heldout6.json: 150 more, chosen blind after v0.31
           and read first from a cloud VM (scripts/make_heldout6.py)
  heldout3 scripts/toolbench-heldout3.json: 152 sites chosen before any was
           read, after the completeness escalation was designed

The stitched_agent arm adds a calling agent: a model (TOOLBENCH_JUDGE_MODEL,
via OpenRouter) reads each result and, when it is not the page asked for,
retries up to twice with the next stronger provider. Its judging cost is
reported separately (judge_cost_usd).

Arms: one forced provider each (http, jina_reader, local, camoufox, scrapling,
firecrawl, zenrows) and the stitched router, free-only and with paid
fallbacks. Each arm has its own state directory. Origin cool-downs are off for
every arm, so one block does not end an arm's later reads of that site. For
each URL the arm order is shuffled (fixed seed), so no tool always reaches a
site first or last; URLs from one origin never run concurrently.

Single tools read pages the way they would on their own: without the owner's
logins. Login cases are reported separately for that reason.

  PYTHONPATH=.:src python scripts/toolbench.py --state /tmp/tb --rows <paid sitebench jsonl>
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))
import sitebench  # noqa: E402

EVALS = Path.home() / ".local/share/frankensurf/evals/toolbench"
HELDOUT = {"heldout": Path(__file__).with_name("toolbench-heldout.json"),
           "heldout2": Path(__file__).with_name("toolbench-heldout2.json"),
           "heldout3": Path(__file__).with_name("toolbench-heldout3.json"),
           "heldout4": Path(__file__).with_name("toolbench-heldout4.json"),
           "heldout5": Path(__file__).with_name("toolbench-heldout5.json"),
           "heldout6": Path(__file__).with_name("toolbench-heldout6.json"),
           "heldout7": Path(__file__).with_name("toolbench-heldout7.json")}
COMMON = {"origin_cooldown_seconds": 0, "freshness": "now", "lightning": False, "allow_real_browser": False}
ARMS = {
    "http": {"provider": "http"},
    "jina_reader": {"provider": "jina_reader"},
    "local": {"provider": "local"},
    "camoufox": {"provider": "camoufox"},
    "scrapling": {"provider": "scrapling"},
    "firecrawl": {"provider": "firecrawl", "allow_paid_fallbacks": True},
    "zenrows": {"provider": "zenrows", "allow_paid_fallbacks": True},
    "scrapfly": {"provider": "scrapfly", "allow_paid_fallbacks": True},
    "zyte": {"provider": "zyte", "allow_paid_fallbacks": True},
    "patchright": {"provider": "patchright"},
    "stitched_free": {},
    # The free router in lightning mode: the read and two other tools start at once.
    "stitched_lightning": {"lightning": True},
    "stitched_paid": {"allow_paid_fallbacks": True},
    # v0.32's behaviour, for A/B runs: a slow read waited 10 s for one hedge, paid
    # tools moved ahead after 4 walls, and a not-found page stayed not found.
    "stitched_free_v032": {"hedge_after_seconds": 10, "hedge_racers": 1, "rescue_not_found": False},
    "stitched_paid_v032": {"allow_paid_fallbacks": True, "paid_after_walls": 0, "hedge_after_seconds": 10,
                           "hedge_racers": 1, "rescue_not_found": False},
    # The router plus a calling agent: a model reads each result and, when it is
    # not the page that was asked for, retries with the next stronger provider.
    "stitched_agent": {"allow_paid_fallbacks": True},
}
STITCHED = {"stitched_free", "stitched_lightning", "stitched_paid", "stitched_agent",
            "stitched_free_v032", "stitched_paid_v032"}
AGENT_LADDER = ("scrapling", "camoufox", "firecrawl", "zyte", "scrapfly", "zenrows", "patchright")
AGENT_RETRIES = 2
JUDGE_MODEL = os.environ.get("TOOLBENCH_JUDGE_MODEL", "google/gemini-3.5-flash-lite")
JUDGE_PROMPT = """You asked a web reading tool for this page: {url}

It returned (title, then the start of the visible text):
TITLE: {title}
TEXT: {text}

Is this the real content of that page? For a search or category URL that means
actual results; for a product, listing or article URL, the item or article
itself. Answer NO for a block or CAPTCHA page, a sign-in wall, a consent screen,
an error page, or a page whose main content has not loaded.
Reply with exactly one word: YES or NO."""


async def judge(url: str, result: dict) -> tuple[bool, float]:
    """A calling agent's view of one result: is this the page? Returns (ok, cost)."""
    import httpx
    text = (result.get("text") or "")[:4000]
    prompt = JUDGE_PROMPT.format(url=url, title=(result.get("title") or "")[:200], text=text)
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": "Bearer " + os.environ["OPENROUTER_API_KEY"]},
                json={"model": JUDGE_MODEL, "max_tokens": 5, "temperature": 0,
                      "messages": [{"role": "user", "content": prompt}], "usage": {"include": True}})
            body = response.json()
        answer = body["choices"][0]["message"]["content"].strip().upper()
        return answer.startswith("YES"), float((body.get("usage") or {}).get("cost") or 0)
    except Exception:
        # A judge outage must not count as a verdict; accept the page as-is.
        return True, 0.0


def frozen_main(rows_path: Path) -> list[dict]:
    """The exact URLs of one site-benchmark run, as fixed cases."""
    corpus = json.loads(sitebench.CORPUS.read_text())["cases"]
    by_url = {case["url"]: case for case in corpus}
    cases = []
    for line in rows_path.read_text().splitlines():
        row = json.loads(line)
        if row["status"] == "dead_link":
            continue
        parent = by_url.get(row["followed_from"] or row["url"], {})
        case = {"set": "main", "site": row["site"], "wall": row["wall"], "kind": row["kind"],
                "url": row["url"], "identity": parent.get("identity")}
        if row["followed_from"]:
            case["expect"] = (parent.get("follow") or {}).get("expect")
        else:
            case["expect"] = parent.get("expect")
            if parent.get("follow"):
                case["follow"] = {**parent["follow"], "n": 0}
        cases.append(case)
    return cases


def heldout() -> list[dict]:
    return [{"set": name, "kind": "article", "expect_min": 3, **case}
            for name, path in HELDOUT.items() for case in json.loads(path.read_text())["cases"]]


def check(case: dict, result: dict) -> tuple[bool, str | None]:
    valid, reason = sitebench.check(case, result)
    if valid and case.get("expect_min"):
        found = len(re.findall(case["expect"], result.get("text") or "", re.I))
        if found < case["expect_min"]:
            return False, f"expect_count:{found}"
    return valid, reason


async def read(web, arm: str, case: dict) -> dict:
    overrides = {**COMMON, **ARMS[arm]}
    if case.get("identity") and arm in STITCHED:
        overrides["identity"] = case["identity"]
    started = time.monotonic()
    judge_cost, retries = 0.0, []
    try:
        result = await asyncio.wait_for(web.read(case["url"], policy_overrides=overrides), 300)
        if arm == "stitched_agent":
            receipt = result.get("receipt") or {}
            tried = {a.get("provider") for a in receipt.get("attempts") or ()}
            tried |= {s.get("provider") for s in (receipt.get("completeness") or {}).get("escalations") or ()}
            for _ in range(AGENT_RETRIES):
                ok, cost = (await judge(case["url"], result)) if receipt.get("status") == "observed" else (False, 0.0)
                judge_cost += cost
                if ok:
                    break
                remaining = [p for p in AGENT_LADDER if p not in tried and web.providers.is_available(p)]
                if not remaining:
                    break
                tried.add(remaining[0])
                retry = await asyncio.wait_for(web.read(case["url"], policy_overrides={
                    **overrides, "provider_candidates": [remaining[0]]}), 300)
                retries.append(remaining[0])
                retry_receipt = retry.get("receipt") or {}
                if retry_receipt.get("status") == "observed":
                    result, receipt = retry, retry_receipt
    except Exception as error:  # an arm crash is that arm's failure, not the run's
        result = {"receipt": {"status": "failed", "failure": {"code": type(error).__name__}}}
    receipt = result.get("receipt") or {}
    observed = receipt.get("status") == "observed"
    valid, reason = check(case, result) if observed else (False, None)
    attempts = receipt.get("attempts") or [{}]
    return {"arm": arm, "set": case["set"], "site": case["site"], "wall": case["wall"],
            "kind": case["kind"], "url": case["url"], "login": bool(case.get("identity")),
            "status": receipt.get("status"), "valid": valid, "invalid_reason": reason,
            "failure": (receipt.get("failure") or {}).get("code"),
            "provider": attempts[-1].get("provider"), "attempts": len(receipt.get("attempts") or []),
            "latency_ms": round((time.monotonic() - started) * 1000),
            "cost_usd": receipt.get("cost_usd"), "judge_cost_usd": round(judge_cost, 6),
            "agent_retries": retries,
            "escalations": [step.get("provider") for step in
                            (receipt.get("completeness") or {}).get("escalations") or ()],
            # Which v0.33 behaviour served the read: a racer won, or the site's own
            # search stood in for a URL that answered not found.
            "hedge_won": (receipt.get("hedge") or {}).get("provider") if (receipt.get("hedge") or {}).get("won") else None,
            "rescued": bool(receipt.get("rescued"))}


async def run(cases, state: Path, out: Path, workers: int, gap: float, arms: list[str],
              done: set | None = None):
    from frankensurf import Runtime
    rng = random.Random(7)
    done = done or set()
    plans = []
    for case in cases:
        order = [arm for arm in rng.sample(arms, len(arms)) if (arm, case["url"]) not in done]
        if order:
            plans.append((case, order))
    locks: dict[str, asyncio.Lock] = {}
    queue: asyncio.Queue = asyncio.Queue()
    for plan in plans:
        queue.put_nowait(plan)
    rows = []
    runtimes = {arm: Runtime(state_dir=state / arm) for arm in arms}
    for web in runtimes.values():
        await web.__aenter__()

    async def worker():
        while True:
            try:
                case, order = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            origin = urlparse(case["url"]).hostname or ""
            lock = locks.setdefault(origin.removeprefix("www."), asyncio.Lock())
            async with lock:
                for arm in order:
                    row = await read(runtimes[arm], arm, case)
                    rows.append(row)
                    with out.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(row, sort_keys=True) + "\n")
                    print(json.dumps({k: row[k] for k in ("arm", "site", "valid", "invalid_reason",
                                                          "failure", "latency_ms")}), flush=True)
                    await asyncio.sleep(gap)
    try:
        await asyncio.gather(*(worker() for _ in range(workers)))
    finally:
        for web in runtimes.values():
            await web.__aexit__(None, None, None)
    return rows


def summarize(rows: list[dict]) -> dict:
    def block(group):
        if not group:
            return None
        valid = [r for r in group if r["valid"]]
        latency = sorted(r["latency_ms"] for r in valid)
        costs = [r["cost_usd"] for r in group if r["cost_usd"] is not None]
        unknown_cost = sum(r["cost_usd"] is None and r["provider"] in {"zenrows", "firecrawl"}
                           for r in group)
        return {"cases": len(group), "valid": len(valid), "valid_rate": round(len(valid) / len(group), 3),
                "false_success": sum(r["status"] == "observed" and not r["valid"] for r in group),
                "p50_ms": latency[len(latency) // 2] if latency else None,
                "p90_ms": latency[int(len(latency) * 0.9)] if latency else None,
                "mean_ms_all": round(statistics.mean(r["latency_ms"] for r in group)),
                "cost_usd": round(sum(costs), 4), "paid_reads_cost_unknown": unknown_cost,
                "usd_per_1k_valid": round(sum(costs) / len(valid) * 1000, 2) if valid else None}
    out = {}
    for arm in dict.fromkeys(r["arm"] for r in rows):
        mine = [r for r in rows if r["arm"] == arm]
        out[arm] = {"main_public": block([r for r in mine if r["set"] == "main" and not r["login"]]),
                    "main_login": block([r for r in mine if r["set"] == "main" and r["login"]]),
                    "heldout": block([r for r in mine if r["set"] == "heldout"]),
                    "heldout2": block([r for r in mine if r["set"] == "heldout2"]),
                    "heldout3": block([r for r in mine if r["set"] == "heldout3"]),
                    "heldout4": block([r for r in mine if r["set"] == "heldout4"]),
                    "heldout5": block([r for r in mine if r["set"] == "heldout5"])}
    # Oracle: the best any single tool can do if you knew in advance which to use.
    singles = [arm for arm in out if arm not in STITCHED]
    for name, keep in (("main_public", lambda r: r["set"] == "main" and not r["login"]),
                       ("heldout", lambda r: r["set"] == "heldout"),
                       ("heldout2", lambda r: r["set"] == "heldout2"),
                       ("heldout3", lambda r: r["set"] == "heldout3"),
                       ("heldout4", lambda r: r["set"] == "heldout4"),
                       ("heldout5", lambda r: r["set"] == "heldout5")):
        urls = {r["url"] for r in rows if keep(r)}
        covered = {r["url"] for r in rows if keep(r) and r["arm"] in singles and r["valid"]}
        out.setdefault("_union_of_single_tools", {})[name] = {"cases": len(urls), "valid": len(covered)}
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--rows", type=Path, required=True, help="paid sitebench rows (jsonl) to freeze")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--gap", type=float, default=2.0, help="seconds between arms on one URL")
    parser.add_argument("--arms", nargs="*", default=list(ARMS))
    parser.add_argument("--only-set", nargs="*", choices=["main", *HELDOUT])
    parser.add_argument("--label", default="run")
    parser.add_argument("--resume", type=Path, help="rows file to continue; finished (arm, URL) pairs are skipped")
    parser.add_argument("--sample", type=int, help="read only this many cases, spread evenly over the set")
    args = parser.parse_args(argv)
    cases = frozen_main(args.rows) + heldout()
    if args.only_set:
        cases = [case for case in cases if case["set"] in args.only_set]
    if args.sample and args.sample < len(cases):
        step = len(cases) / args.sample
        cases = [cases[int(index * step)] for index in range(args.sample)]
    EVALS.mkdir(parents=True, exist_ok=True)
    out = args.resume or EVALS / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + args.label + ".jsonl")
    previous = [json.loads(line) for line in out.read_text().splitlines()] if args.resume else []
    done = {(row["arm"], row["url"]) for row in previous}
    rows = previous + asyncio.run(run(cases, args.state.expanduser(), out, args.workers, args.gap,
                                      args.arms, done))
    print(json.dumps({"rows": str(out), "summary": summarize(rows)}, indent=2))


if __name__ == "__main__":
    main()
