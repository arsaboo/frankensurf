"""Article-quality evaluation (Jump 7): collect news/company article URLs, read each
with the given FrankenSurf tree (free tools, main_content on), score the main text.

  python article_eval.py collect urls.json
  python article_eval.py read urls.json out.jsonl STATE_DIR
  python article_eval.py compare old.jsonl new.jsonl
"""
import asyncio
import json
import statistics
import sys
import time

QUERIES = ["quarterly results", "acquisition announced", "appoints chief executive", "raises funding",
           "data breach", "product recall", "opens new factory", "layoffs", "share price falls",
           "partnership agreement", "ceo resigns", "annual report", "merger talks", "new store opening",
           "profit warning", "court ruling company", "supply chain", "electric vehicle maker", "bank fined",
           "airline cancels", "mining company", "retailer sales", "startup launches", "chip maker",
           "insurance company", "telecom outage", "pharmaceutical approval", "real estate developer",
           "hotel group", "shipping company", "media company", "university research", "council approves",
           "energy company", "logistics firm", "fintech", "biotech", "construction company", "winery", "brewery"]


_LINK = __import__("re").compile(r"!?\[([^\]]*)\]\(([^)]*)\)")


def paragraphs(text):
    """Prose lines (80+ characters, under a third links): same rule in every version."""
    count = 0
    for line in (text or "").splitlines():
        plain = _LINK.sub(lambda m: m.group(1), line).strip(" *#>-|")
        linked = sum(len(m.group(1)) for m in _LINK.finditer(line))
        if len(plain) >= 80 and linked < 0.35 * max(len(plain), 1):
            count += 1
    return count


def score(main_text):
    text = main_text or ""
    head = text[:4000]
    return {"main_chars": len(text), "paragraphs": paragraphs(text), "head_paragraphs": paragraphs(head),
            "article": len(text) >= 600 and paragraphs(text) >= 2}


async def collect(path):
    from frankensurf import Runtime, WebPolicy
    urls, seen = [], set()
    async with Runtime("/tmp/eval-collect") as web:
        for query in QUERIES:
            found = await web.search(query, vertical="news", limit=6, policy=WebPolicy(provider="http"))
            for item in found["results"]:
                host = item["url"].split("/")[2]
                if host in seen or "msn.com" in host:
                    continue
                seen.add(host)
                urls.append(item["url"])
    json.dump(urls, open(path, "w"), indent=1)
    print(len(urls), "article URLs, one per site")


async def read(path, out, state):
    from frankensurf import Runtime, WebPolicy
    urls = json.load(open(path))
    done = {json.loads(line)["url"] for line in open(out)} if __import__("os").path.exists(out) else set()
    sem = asyncio.Semaphore(6)
    async with Runtime(state) as web:
        async def one(url):
            async with sem:
                start = time.monotonic()
                try:
                    result = await web.read(url, WebPolicy(main_content=True))
                except Exception as error:  # noqa: BLE001 - evaluation
                    return {"url": url, "status": "error", "error": type(error).__name__}
                receipt = result["receipt"]
                row = {"url": url, "status": receipt["status"], "method": receipt.get("method"),
                       "latency": round(time.monotonic() - start, 1),
                       "quality": (receipt.get("quality") or {}).get("grade"),
                       "escalations": [s["provider"] for s in (receipt.get("completeness") or {}).get("escalations") or []],
                       **score(result.get("main_text"))}
                with open(out, "a") as handle:
                    handle.write(json.dumps(row) + "\n")
                return row
        await asyncio.gather(*(one(url) for url in urls if url not in done))


def compare(old_path, new_path):
    old = {r["url"]: r for r in map(json.loads, open(old_path))}
    new = {r["url"]: r for r in map(json.loads, open(new_path))}
    urls = sorted(set(old) & set(new))
    for name, rows in (("v0.28", [old[u] for u in urls]), ("jump7", [new[u] for u in urls])):
        got = [r for r in rows if r["status"] == "observed"]
        print(f"{name}: {len(urls)} pages, read {len(got)}, article present {sum(r['article'] for r in rows)}, "
              f"2+ paragraphs in first 4k {sum(r['head_paragraphs'] >= 2 for r in rows)}, "
              f"median latency {statistics.median(r.get('latency', 0) for r in got):.1f}s")
    print("grades (jump7):", {g: sum(new[u].get("quality") == g for u in urls) for g in ("good", "partial", "poor", None)})
    gained = [u for u in urls if new[u]["article"] and not old[u]["article"]]
    lost = [u for u in urls if old[u]["article"] and not new[u]["article"]]
    print("article gained:", len(gained), gained[:8])
    print("article lost:", len(lost), lost[:8])
    poor_ok = [u for u in urls if new[u].get("quality") == "poor" and new[u]["article"]]
    good_bad = [u for u in urls if new[u].get("quality") == "good" and not new[u]["article"]]
    print("graded poor but article present:", len(poor_ok), poor_ok[:5])
    print("graded good but no article:", len(good_bad), good_bad[:5])


if __name__ == "__main__":
    command = sys.argv[1]
    if command == "collect":
        asyncio.run(collect(sys.argv[2]))
    elif command == "read":
        asyncio.run(read(sys.argv[2], sys.argv[3], sys.argv[4]))
    else:
        compare(sys.argv[2], sys.argv[3])
