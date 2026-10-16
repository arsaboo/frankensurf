"""Read every held-out set 7 URL with GPT Researcher's own Scraper (default SCRAPER=bs), as it ships."""
import asyncio, json, sys, time, logging
from gpt_researcher.scraper.scraper import Scraper
from gpt_researcher.utils.workers import WorkerPool

logging.basicConfig(level=logging.WARNING)
import os
SCRAPER = os.environ.get("ICP_SCRAPER", "bs")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/128.0.0.0 Safari/537.36 Edg/128.0.0.0")  # GPT Researcher's default USER_AGENT

async def one(case, pool):
    s = Scraper([case["url"]], UA, SCRAPER, pool)
    t = time.monotonic()
    raw = await s.extract_data_from_url(case["url"], s.session)
    return {"site": case["site"], "url": case["url"], "latency_ms": round((time.monotonic() - t) * 1000),
            "text": raw.get("raw_content"), "title": raw.get("title")}

async def main(cases_path, out):
    cases = json.load(open(cases_path))["cases"]
    pool = WorkerPool(8)
    sem = asyncio.Semaphore(8)
    async def guarded(c):
        async with sem:
            try:
                return await asyncio.wait_for(one(c, pool), float(os.environ.get("ICP_TIMEOUT", "90")))
            except Exception as e:
                return {"site": c["site"], "url": c["url"], "text": None, "error": type(e).__name__}
    rows = await asyncio.gather(*(guarded(c) for c in cases))
    with open(out, "w") as f:
        for r in rows: f.write(json.dumps(r) + "\n")
    print("gptr rows", len(rows), "with content", sum(1 for r in rows if r.get("text")))

asyncio.run(main(sys.argv[1], sys.argv[2]))
