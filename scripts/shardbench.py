"""ShardX (ProxyShard's anti-detect browser) against the free stealth browsers, on held-out set 7
or on a set of Akamai-protected sites.

Run only on a throwaway CI machine (.github/workflows/shardbench.yml): ShardX's SDK downloads a
prebuilt, closed Chromium build from the vendor's bucket, checked only against a manifest it
fetches at run time, so it never runs on a machine that matters.

Every arm reads the same 150 URLs, with the same per-origin locking and shuffled arm order as
scripts/toolbench.py, and is judged by the same content checks. The shardx arm loads each page
in one ShardX browser over CDP (Playwright connect_over_cdp), then hands the HTML to FrankenSurf's
own challenge check and parser, so its pages are judged exactly as a FrankenSurf browser's are.

  PYTHONPATH=.:src python scripts/shardbench.py --out rows.jsonl [--sample 20]
  PYTHONPATH=.:src python scripts/shardbench.py --out rows.jsonl --set akamai --frankensurf

--set akamai reads shardbench-akamai-candidates.json, written before any tool read those pages.
Each URL first gets one plain request; only those where FrankenSurf's wall_vendor() says Akamai
answered are read by the arms, and the probe results are saved next to the rows.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))
import toolbench  # noqa: E402

BASELINE = ["local", "camoufox", "scrapling", "patchright"]
AKAMAI = Path(__file__).with_name("shardbench-akamai-candidates.json")
SETTLE_MS = 2500
GOTO_TIMEOUT_MS = 45_000


class ShardArm:
    """One ShardX browser for the whole run; a fresh page per read."""

    def __init__(self, headless):
        self.headless = headless
        self.session = self.playwright = self.browser = None

    async def start(self):
        from playwright.async_api import async_playwright
        from shardx import ShardX
        sdk = ShardX()
        profile = sdk.create_profile(platform="Windows")
        # As the Reddit suggestion had it: timezone, language and location follow the exit address.
        if hasattr(profile, "with_override"):
            profile = profile.with_override(timezone="auto", navigator={"language": "auto"},
                                            geolocation={"mode": "auto"})
        self.session = await asyncio.to_thread(
            sdk.launch, profile, cdp=True, headless=self.headless,
            extra_args=["--no-sandbox", "--disable-dev-shm-usage"])
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.connect_over_cdp(self.session.cdp_url)

    async def stop(self):
        for step in (lambda: self.browser and self.browser.close(),
                     lambda: self.playwright and self.playwright.stop()):
            try:
                await step()
            except Exception:
                pass
        if self.session:
            await asyncio.to_thread(self.session.stop)

    async def read(self, case):
        from frankensurf.runtime import _challenge, _status_failure, parse_content
        started = time.monotonic()
        context = self.browser.contexts[0] if self.browser.contexts else await self.browser.new_context()
        page = await context.new_page()
        status, failure, result = None, None, {}
        try:
            response = await page.goto(case["url"], wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
            await page.wait_for_timeout(SETTLE_MS)
            html = await page.content()
            status = response.status if response else None
            failure = _status_failure(status) if status else None
            if not failure and _challenge(html):
                failure = "CHALLENGE"
            if not failure:
                result = parse_content(html, "text/html", page.url, None)
                result.update(url=page.url, content=html)
        except Exception as error:
            failure = type(error).__name__
        finally:
            await page.close()
        observed = failure is None
        result["receipt"] = {"status": "observed" if observed else "failed",
                             **({"failure": {"code": failure}} if failure else {})}
        valid, reason = toolbench.check(case, result) if observed else (False, None)
        return {"arm": "shardx", "set": case["set"], "site": case["site"], "wall": case["wall"],
                "kind": case["kind"], "url": case["url"], "login": False, "status": result["receipt"]["status"],
                "valid": valid, "invalid_reason": reason, "failure": failure, "provider": "shardx",
                "attempts": 1, "latency_ms": round((time.monotonic() - started) * 1000), "cost_usd": 0,
                "judge_cost_usd": 0, "agent_retries": [], "escalations": [], "hedge_won": None,
                "rescued": False, "http_status": status}


async def akamai_cases(out: Path) -> list[dict]:
    """The candidates where Akamai answers a plain request, by the fixed rule in the candidates file."""
    import httpx
    from frankensurf.runtime import _user_agent, wall_vendor
    spec = json.loads(AKAMAI.read_text())
    candidates = [{**c, "origin": "known"} for c in spec["known"]] + [{**c, "origin": "fresh"} for c in spec["fresh"]]
    probes, cases = [], []
    async with httpx.AsyncClient(follow_redirects=True, timeout=20, headers={"User-Agent": _user_agent()}) as client:
        for candidate in candidates:
            try:
                response = await client.get(candidate["url"])
                vendor, status = wall_vendor(response.headers, response.text), response.status_code
                cookies = " ".join(response.headers.get_list("set-cookie"))
                bot_manager = any(name in cookies for name in ("_abck=", "bm_sz=", "ak_bmsc="))
            except Exception as error:
                vendor, status, bot_manager = None, type(error).__name__, False
            probes.append({"site": candidate["site"], "url": candidate["url"], "origin": candidate["origin"],
                           "vendor": vendor, "status": status, "bot_manager_cookie": bot_manager})
            print(json.dumps(probes[-1]), flush=True)
            if vendor == "akamai":
                cases.append({"set": "akamai", "kind": "article", "expect_min": 3, "wall": "akamai",
                              "site": candidate["site"], "url": candidate["url"], "expect": candidate["expect"]})
            await asyncio.sleep(1.0)
    out.with_name("shardbench-akamai-probe.json").write_text(json.dumps(probes, indent=1))
    return cases


async def run(cases, out, workers, gap, headless, frankensurf=False):
    from frankensurf import Runtime
    rng = random.Random(7)
    baseline = [*BASELINE, *(["stitched_free"] if frankensurf else [])]
    arms = ["shardx", *baseline]
    state = Path(out).parent / "shardbench-state"
    runtimes = {arm: Runtime(state_dir=state / arm) for arm in baseline}
    for web in runtimes.values():
        await web.__aenter__()
    shard = ShardArm(headless)
    await shard.start()
    queue: asyncio.Queue = asyncio.Queue()
    for case in cases:
        queue.put_nowait((case, rng.sample(arms, len(arms))))
    locks: dict[str, asyncio.Lock] = {}
    rows = []

    async def worker():
        while True:
            try:
                case, order = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            origin = (urlparse(case["url"]).hostname or "").removeprefix("www.")
            async with locks.setdefault(origin, asyncio.Lock()):
                for arm in order:
                    if arm == "shardx":
                        row = await shard.read(case)
                    else:
                        row = await toolbench.read(runtimes[arm], arm, case)
                    rows.append(row)
                    with open(out, "a", encoding="utf-8") as stream:
                        stream.write(json.dumps(row, sort_keys=True) + "\n")
                    print(json.dumps({k: row[k] for k in ("arm", "site", "valid", "invalid_reason", "failure",
                                                          "latency_ms")}), flush=True)
                    await asyncio.sleep(gap)
    try:
        await asyncio.gather(*(worker() for _ in range(workers)))
    finally:
        await shard.stop()
        for web in runtimes.values():
            await web.__aexit__(None, None, None)
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--gap", type=float, default=2.0)
    parser.add_argument("--sample", type=int, help="read only this many sites, spread over the set")
    parser.add_argument("--headless", action="store_true", help="ShardX headless (default: headed, under xvfb)")
    parser.add_argument("--set", choices=["heldout7", "akamai"], default="heldout7")
    parser.add_argument("--frankensurf", action="store_true", help="also read with FrankenSurf, free tools only")
    args = parser.parse_args(argv)
    if args.set == "akamai":
        cases = asyncio.run(akamai_cases(args.out))
    else:
        cases = [case for case in toolbench.heldout() if case["set"] == "heldout7"]
    if args.sample and args.sample < len(cases):
        step = len(cases) / args.sample
        cases = [cases[int(index * step)] for index in range(args.sample)]
    rows = asyncio.run(run(cases, args.out, args.workers, args.gap, args.headless, args.frankensurf))
    summary = toolbench.summarize(rows)
    print(json.dumps({"rows": str(args.out), "sites": len(cases), "summary": summary}, indent=2))


if __name__ == "__main__":
    main()
