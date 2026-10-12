"""Search benchmark: which search source (or the router) finds the right page?

Every arm runs the same queries from scripts/searchbench-queries.json through
FrankenSurf's Python search API, one source forced per arm, plus the router
("frankensurf": no source named). The top 5 results are scored:

  RIGHT    on-query (every entity term in the title or snippet, or in the page
           after reading it), inside the recency window when one is set (the
           result's own date, else the page's published date; unknown does not
           count), not a homepage or listing when needs_specific, and not a
           challenge or error page
  hit@5    at least one of the top 5 is RIGHT
  prec@5   RIGHT results over the results returned
  dom@5    an expect_domains domain is among the top 5 (queries that give one)

Scoring needs no network: page reads are cached on disk by URL under the output
directory, paced per origin and run a few at a time.

Arms: every free id in search.REGISTRY (a self-hosted searxng only when it is
listening), "frankensurf", and with --paid the keyed exa and firecrawl_search
(skipped when not installed or not configured). Sources that are unknown are
skipped, not failed.

  PYTHONPATH=src python scripts/searchbench.py --set heldout --out /tmp/sb
  PYTHONPATH=src python scripts/searchbench.py --rows /tmp/sb/rows.jsonl --compare frankensurf bing_rss
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import itertools
import json
import logging
import math
import re
import socket
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from frankensurf.search import REGISTRY, RECENCY, matches_domain, parse_date

EVALS = Path.home() / ".local/share/frankensurf/evals/searchbench"
QUERIES = Path(__file__).with_name("searchbench-queries.json")
ROUTER = "frankensurf"
# The router with paid sources allowed as its last rungs.
ROUTER_PAID = "frankensurf_paid"
PAID = ("exa", "firecrawl_search")
TOP = 5
READ_OVERRIDES = {"origin_cooldown_seconds": 0, "freshness": "now", "lightning": False,
                  "allow_real_browser": False, "allow_paid_fallbacks": False}
# Public endpoints get a pause between one arm's queries; GitHub's search API is capped near 10 a minute.
SOURCE_GAP = {"github": 7.0, "stack_exchange": 2.0, "wikipedia": 1.0, "arxiv": 3.5}
DEFAULT_GAP = 3.0
PAGE_TEXT_CHARS = 20000

META_DATES = {"article:published_time", "og:article:published_time", "datepublished", "date", "dc.date",
              "dc.date.issued", "dcterms.date", "citation_publication_date", "citation_date",
              "citation_online_date", "publish-date", "pubdate", "parsely-pub-date", "sailthru.date",
              "publication_date", "article.published", "og:published_time"}
LISTING_WORDS = {"news", "blog", "blogs", "press", "newsroom", "articles", "archive", "archives", "category",
                 "categories", "tag", "tags", "topic", "topics", "search", "index", "latest", "releases",
                 "stories", "updates", "announcements", "press-releases", "media", "insights", "events",
                 "home", "papers", "trending", "main_page", "list", "feed", "all"}
LISTING_PARENTS = {"tag", "tags", "category", "categories", "topic", "topics", "archive", "archives",
                   "search", "page", "author", "authors", "label", "labels", "feed"}
LISTING_QUERY = {"q", "s", "query", "search", "page", "p", "paged"}
LOCALE = re.compile(r"[a-z]{2}[-_][a-zA-Z]{2}")
TITLE_CHALLENGE = re.compile(
    r"just a moment|attention required|are you a robot|robot check|captcha|access denied|verify you are human"
    r"|page not found|security check|pardon our interruption|request blocked|^\W*(not found|forbidden|error)\W*$"
    r"|\b(?:40[0-9]|410|50[0-9])\b.{0,20}(?:not found|forbidden|error|unavailable)", re.I)
BODY_CHALLENGE = (
    "verify you are human", "verifying you are human", "checking your browser", "enable javascript and cookies",
    "unusual traffic from your", "access to this page has been denied", "pardon our interruption",
    "press & hold", "captcha", "page not found", "404 not found", "this page doesn't exist",
    "this page does not exist", "request blocked", "are you a robot", "just a moment")


# --- scoring -----------------------------------------------------------------

def term_pattern(entry: str) -> re.Pattern:
    """One entity term: 'a|b' is either, 'stem*' any word starting so; plurals match."""
    alternatives = [re.escape(part.strip()).replace(r"\*", r"\w*") for part in entry.split("|") if part.strip()]
    return re.compile(r"(?<!\w)(?:" + "|".join(alternatives) + r")(?:e?s)?(?!\w)", re.I)


def terms_match(terms: list[str], text: str) -> bool:
    """Every entity term appears in the text."""
    return bool(terms) and all(term_pattern(entry).search(text or "") for entry in terms)


def is_listing(url: str) -> bool:
    """A homepage, section front or search/tag/archive listing rather than a page about one thing."""
    parsed = urlparse(url)
    segments = [part for part in parsed.path.split("/") if part and not LOCALE.fullmatch(part)]
    if not segments:
        return True
    if LISTING_QUERY & {key.split("=")[0].lower() for key in parsed.query.split("&") if key}:
        return True
    last = segments[-1].rsplit(".", 1)[0].lower()
    if last in LISTING_WORDS or (last.isdigit() and len(segments) > 1 and segments[-2].lower() == "page"):
        return True
    return len(segments) == 2 and segments[0].lower() in LISTING_PARENTS


def is_challenge(title: str, text: str) -> bool:
    """A bot wall, consent wall or error page, judged from the title and the start of the text."""
    if TITLE_CHALLENGE.search(title or ""):
        return True
    head = (text or "")[:800].lower()
    return any(phrase in head for phrase in BODY_CHALLENGE)


def extract_date(html: str) -> tuple[datetime | None, str | None]:
    """A page's published date and where it came from: meta tag, JSON-LD, itemprop or <time>."""
    if not isinstance(html, str) or "<" not in html:
        return None, None
    soup = BeautifulSoup(html[:600_000], "html.parser")
    for tag in soup.find_all("meta"):
        key = (tag.get("property") or tag.get("name") or tag.get("itemprop") or "").lower()
        if key in META_DATES:
            when = parse_date(tag.get("content"))
            if when:
                return when, "meta:" + key
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except ValueError:
            continue
        stack = [data]
        while stack:
            node = stack.pop(0)
            if isinstance(node, dict):
                when = parse_date(node.get("datePublished"))
                if when:
                    return when, "jsonld"
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
    for tag in soup.find_all(attrs={"itemprop": "datePublished"}):
        when = parse_date(tag.get("content") or tag.get("datetime") or tag.get_text(strip=True))
        if when:
            return when, "itemprop"
    for tag in soup.find_all("time"):
        when = parse_date(tag.get("datetime"))
        if when:
            return when, "time"
    return None, None


def in_window(when: datetime | None, recency: str | None, now: datetime) -> bool | None:
    """None when no window is set; False when the date is unknown or outside it."""
    if not recency:
        return None
    return when is not None and now - timedelta(days=RECENCY[recency]) <= when <= now + timedelta(days=1)


def judge(query: dict, item: dict, page: dict | None, now: datetime) -> dict:
    """The verdict on one result. page is a cached read ({"status", "title", "text", "date", ...}) or None."""
    url, title, snippet = item.get("url") or "", item.get("title") or "", item.get("snippet") or ""
    page_ok = bool(page) and page.get("status") == "observed"
    why = []
    on_query = "snippet" if terms_match(query["entity_terms"], title + " " + snippet) else None
    if on_query is None and page_ok and terms_match(query["entity_terms"],
                                                    (page.get("title") or "") + " " + (page.get("text") or "")):
        on_query = "page"
    if on_query is None:
        why.append("off_query")
    when, date_source = parse_date(item.get("published")), "result"
    if when is None:
        when, date_source = (parse_date(page.get("date")), "page") if page_ok and page.get("date") else (None, None)
    window = in_window(when, query.get("recency"), now)
    if window is False:
        why.append("outside_window" if when else "date_unverified")
    listing = is_listing(url)
    if query.get("needs_specific") and listing:
        why.append("listing")
    challenge = is_challenge(title, "")
    if page_ok:
        challenge = challenge or is_challenge(page.get("title") or "", page.get("text") or "")
    elif page and (page.get("http_status") in (404, 410) or "NOT_FOUND" in str(page.get("failure") or "")):
        challenge = True
    if challenge:
        why.append("challenge")
    return {"url": url, "title": title[:160], "snippet": snippet[:200], "on_query": on_query,
            "date": when.isoformat() if when else None, "date_source": date_source, "in_window": window,
            "listing": listing, "challenge": challenge,
            "read": (page.get("status") if page else None), "right": not why, "why": why}


def needs_read(query: dict, item: dict, now: datetime) -> bool:
    """Read a result unless the cheap checks already rule it out: it would otherwise be RIGHT or unresolved."""
    verdict = judge(query, item, None, now)
    return not ({"listing", "challenge"} & set(verdict["why"])) and "outside_window" not in verdict["why"]


def score(query: dict, results: list[dict], pages: dict, now: datetime) -> dict:
    """Score one result list: per-result verdicts, hit@5, precision@5 and the expect_domain hit."""
    top = results[:TOP]
    verdicts = [judge(query, item, pages.get(item.get("url")), now) for item in top]
    right = sum(v["right"] for v in verdicts)
    domains = query.get("expect_domains")
    return {"verdicts": verdicts, "hit": right > 0, "right": right,
            "precision": round(right / len(top), 3) if top else 0.0,
            "domain_hit": any(matches_domain(item.get("url") or "", d) for item in top for d in domains)
            if domains else None}


def mcnemar_exact(only_a: int, only_b: int) -> float:
    """Two-sided exact binomial McNemar p-value from the discordant pairs."""
    n = only_a + only_b
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, k) for k in range(min(only_a, only_b) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


# --- reading pages -----------------------------------------------------------

class Reader:
    """Page reads for scoring: cached on disk by URL, paced per origin, a few at a time."""

    def __init__(self, web, cache: Path, gap: float, concurrency: int, provider: str | None,
                 timeout: float = 60.0, enabled: bool = True):
        self.web, self.cache, self.gap, self.provider, self.timeout = web, cache, gap, provider, timeout
        self.enabled = enabled
        self.sem = asyncio.Semaphore(concurrency)
        self.locks: dict[str, asyncio.Lock] = {}
        self.last: dict[str, float] = {}
        self.inflight: dict[str, asyncio.Future] = {}
        cache.mkdir(parents=True, exist_ok=True)

    def path(self, url: str) -> Path:
        return self.cache / (hashlib.sha1(url.encode()).hexdigest() + ".json")

    async def get(self, url: str) -> dict | None:
        if not self.enabled:
            return None
        path = self.path(url)
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        if url not in self.inflight:
            self.inflight[url] = asyncio.ensure_future(self.fetch(url, path))
        return await self.inflight[url]

    async def fetch(self, url: str, path: Path) -> dict:
        origin = (urlparse(url).hostname or "").removeprefix("www.")
        async with self.locks.setdefault(origin, asyncio.Lock()):
            await asyncio.sleep(max(0.0, self.last.get(origin, 0) + self.gap - time.monotonic()))
            async with self.sem:
                overrides = {**READ_OVERRIDES, **({"provider": self.provider} if self.provider else {})}
                try:
                    result = await asyncio.wait_for(self.web.read(url, policy_overrides=overrides), self.timeout)
                except Exception as error:  # a read crash is that page's failure, not the run's
                    result = {"receipt": {"status": "failed", "failure": {"code": type(error).__name__}}}
            self.last[origin] = time.monotonic()
        receipt = result.get("receipt") or {}
        when, source = extract_date(result.get("content")) if "html" in str(result.get("content_type")) else (None, None)
        page = {"url": url, "status": receipt.get("status"), "failure": (receipt.get("failure") or {}).get("code"),
                "http_status": receipt.get("http_status"), "title": (result.get("title") or "")[:200],
                "text": (result.get("text") or "")[:PAGE_TEXT_CHARS],
                "date": when.isoformat() if when else None, "date_source": source}
        path.write_text(json.dumps(page, sort_keys=True), encoding="utf-8")
        return page


# --- running -----------------------------------------------------------------

def load_queries(which: str) -> list[dict]:
    queries = json.loads(QUERIES.read_text(encoding="utf-8"))["queries"]
    return [q for q in queries if which == "all" or q["set"] == which]


def interleave(queries: list[dict]) -> list[dict]:
    """Round-robin over the categories, so a short run still touches every one."""
    groups: dict[str, list] = {}
    for q in queries:
        groups.setdefault(q["category"], []).append(q)
    return [q for row in itertools.zip_longest(*groups.values()) for q in row if q]


def listening(base_url: str) -> bool:
    parsed = urlparse(base_url)
    try:
        with socket.create_connection((parsed.hostname, parsed.port or 80), timeout=1):
            return True
    except OSError:
        return False


def pick_arms(web, requested: list[str] | None, paid: bool) -> tuple[list[str], dict[str, str]]:
    """The arms that can run, and why each skipped one did not."""
    wanted = requested or [*REGISTRY, ROUTER, *((*PAID, ROUTER_PAID) if paid else ())]
    arms, skipped = [], {}
    for arm in wanted:
        plugin = getattr(web.searches, "_plugins", {}).get(arm)
        if arm == ROUTER:
            arms.append(arm)
        elif arm == ROUTER_PAID:
            (arms.append(arm) if paid else skipped.__setitem__(arm, "paid; pass --paid"))
        elif not web.searches.contains(arm):
            skipped[arm] = "not installed"
        elif arm in PAID and not paid:
            skipped[arm] = "paid; pass --paid"
        elif arm == "searxng" and not listening(REGISTRY[arm]["base_url"]):
            skipped[arm] = "no local SearXNG listening"
        elif hasattr(plugin, "available") and not plugin.available():
            skipped[arm] = "not configured"
        else:
            arms.append(arm)
    return arms, skipped


async def search_once(web, arm: str, query: dict, timeout: float) -> dict:
    from frankensurf import WebPolicy
    policy = (WebPolicy(allow_paid_fallbacks=True) if arm in (*PAID, ROUTER_PAID)
              else WebPolicy(provider="http"))
    started = time.monotonic()
    try:
        response = await asyncio.wait_for(web.search(
            query["query"], source=None if arm in (ROUTER, ROUTER_PAID) else arm, limit=TOP, policy=policy,
            recency=query.get("recency"), vertical=query.get("vertical") or "web"), timeout)
    except Exception as error:  # an arm crash is that arm's failure, not the run's
        return {"results": [], "status": "failed", "failure": type(error).__name__, "source": None,
                "cost_usd": 0.0, "latency_ms": round((time.monotonic() - started) * 1000)}
    receipt = response.get("receipt") or {}
    attempts = receipt.get("search_attempts") or [{}]
    return {"results": response.get("results") or [], "status": receipt.get("status"),
            "failure": (receipt.get("failure") or {}).get("code"), "source": attempts[-1].get("source"),
            "cost_usd": receipt.get("cost_usd") or 0.0,
            "latency_ms": round((time.monotonic() - started) * 1000)}


async def run_arm(web, reader: Reader, arm: str, queries: list[dict], out: Path, done: set, now: datetime,
                  timeout: float, rows: list[dict]):
    gap = SOURCE_GAP.get(arm, DEFAULT_GAP)
    for query in queries:
        if (arm, query["id"]) in done:
            continue
        found = await search_once(web, arm, query, timeout)
        results = [{k: item.get(k) for k in ("url", "title", "snippet", "published")} for item in found["results"]]
        top = results[:TOP]
        pages = {}
        for item in top:
            if item.get("url") and needs_read(query, item, now):
                pages[item["url"]] = await reader.get(item["url"])
        scored = score(query, results, pages, now)
        row = {"query_id": query["id"], "set": query["set"], "category": query["category"], "arm": arm,
               "query": query["query"], "status": found["status"], "failure": found["failure"],
               "source": found["source"], "n_results": len(top), "results": scored["verdicts"],
               "hit": scored["hit"], "right": scored["right"], "precision": scored["precision"],
               "domain_hit": scored["domain_hit"], "latency_ms": found["latency_ms"],
               "cost_usd": found["cost_usd"], "scored_at": now.isoformat()}
        rows.append(row)
        with out.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, sort_keys=True) + "\n")
        print(json.dumps({k: row[k] for k in ("arm", "query_id", "n_results", "hit", "precision", "failure",
                                              "latency_ms")}), flush=True)
        await asyncio.sleep(gap)


async def run(queries, args, out: Path, previous: list[dict]) -> list[dict]:
    from frankensurf import Runtime
    now = datetime.now(timezone.utc)
    done = {(row["arm"], row["query_id"]) for row in previous}
    rows: list[dict] = []
    async with Runtime(state_dir=out.parent / "state") as web:
        arms, skipped = pick_arms(web, args.arms, args.paid)
        for arm, reason in skipped.items():
            print(f"skip {arm}: {reason}", file=sys.stderr)
        reader = Reader(web, out.parent / "pages", args.read_gap, args.read_concurrency,
                        None if args.read_provider == "auto" else args.read_provider, enabled=not args.no_reads)
        await asyncio.gather(*(run_arm(web, reader, arm, queries, out, done, now, args.search_timeout, rows)
                               for arm in arms))
    return rows


# --- reporting ---------------------------------------------------------------

def table(header: list[str], body: list[list[str]]) -> str:
    widths = [max(len(str(row[i])) for row in [header, *body]) for i in range(len(header))]
    lines = ["  ".join(str(cell).ljust(width) if i == 0 else str(cell).rjust(width)
                       for i, (cell, width) in enumerate(zip(row, widths))) for row in [header, *body]]
    return "\n".join([lines[0], "  ".join("-" * width for width in widths), *lines[1:]])


def summarize(rows: list[dict]) -> dict:
    """Per arm and per arm x category: hit@5, precision@5, expect_domain hit, failures, latency, cost."""
    def block(group):
        latency = sorted(r["latency_ms"] for r in group)
        domain = [r["domain_hit"] for r in group if r["domain_hit"] is not None]
        return {"queries": len(group), "hit": round(statistics.mean(r["hit"] for r in group), 3),
                "precision": round(statistics.mean(r["precision"] for r in group), 3),
                "domain_hit": round(statistics.mean(domain), 3) if domain else None,
                "domain_queries": len(domain),
                "failed": sum(r["status"] != "observed" for r in group),
                "empty": sum(r["status"] == "observed" and not r["n_results"] for r in group),
                "p50_ms": latency[len(latency) // 2], "cost_usd": round(sum(r["cost_usd"] or 0 for r in group), 4)}
    arms = list(dict.fromkeys(r["arm"] for r in rows))
    return {"arms": {arm: block([r for r in rows if r["arm"] == arm]) for arm in arms},
            "by_category": {arm: {cat: block([r for r in rows if r["arm"] == arm and r["category"] == cat])
                                  for cat in sorted({r["category"] for r in rows})
                                  if any(r["arm"] == arm and r["category"] == cat for r in rows)} for arm in arms}}


def compare(rows: list[dict], a: str, b: str) -> dict:
    """Paired exact McNemar on hit@5 over the queries both arms ran."""
    hit = {arm: {r["query_id"]: r["hit"] for r in rows if r["arm"] == arm} for arm in (a, b)}
    shared = sorted(hit[a].keys() & hit[b].keys())
    only_a = sum(hit[a][q] and not hit[b][q] for q in shared)
    only_b = sum(hit[b][q] and not hit[a][q] for q in shared)
    return {"a": a, "b": b, "paired": len(shared), "both": sum(hit[a][q] and hit[b][q] for q in shared),
            "only_a": only_a, "only_b": only_b, "neither": sum(not hit[a][q] and not hit[b][q] for q in shared),
            "hit_a": round(sum(hit[a][q] for q in shared) / len(shared), 3) if shared else None,
            "hit_b": round(sum(hit[b][q] for q in shared) / len(shared), 3) if shared else None,
            "p_value": round(mcnemar_exact(only_a, only_b), 4)}


def report(rows: list[dict], pairs: list[list[str]]):
    summary = summarize(rows)
    print("\nPer arm (top 5; failed searches count as misses)")
    print(table(["arm", "n", "hit@5", "prec@5", "dom@5", "failed", "empty", "p50 ms", "cost $"],
                [[arm, s["queries"], f"{100 * s['hit']:5.1f}%",
                  f"{100 * s['precision']:5.1f}%",
                  f"{100 * s['domain_hit']:5.1f}%" if s["domain_hit"] is not None else "    - ",
                  s["failed"], s["empty"], s["p50_ms"], s["cost_usd"]] for arm, s in summary["arms"].items()]))
    categories = sorted({c for per in summary["by_category"].values() for c in per})
    print("\nhit@5 by category")
    print(table(["arm", *categories], [[arm, *[f"{100 * per[c]['hit']:5.1f}% ({per[c]['queries']})"
                                                 if c in per else "-" for c in categories]]
                                       for arm, per in summary["by_category"].items()]))
    print("\nprecision@5 by category")
    print(table(["arm", *categories], [[arm, *[f"{100 * per[c]['precision']:5.1f}%" if c in per else "-"
                                                 for c in categories]]
                                       for arm, per in summary["by_category"].items()]))
    comparisons = [compare(rows, a, b) for a, b in pairs]
    for c in comparisons:
        print(f"\n{c['a']} vs {c['b']}: {c['paired']} paired queries, hit@5 {c['hit_a']} vs {c['hit_b']}; "
              f"only {c['a']} {c['only_a']}, only {c['b']} {c['only_b']}, both {c['both']}, neither {c['neither']}; "
              f"exact McNemar p = {c['p_value']}")
    return {"summary": summary, "comparisons": comparisons}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--set", choices=["dev", "heldout", "blind2", "blind3", "blind4", "blind5", "all"], default="dev")
    parser.add_argument("--arms", nargs="*", help="default: every free source, the router, and with --paid exa and firecrawl_search")
    parser.add_argument("--paid", action="store_true", help="also run the keyed paid arms (they cost money)")
    parser.add_argument("--limit-queries", type=int, help="run only the first N queries, spread over the categories")
    parser.add_argument("--only-category", nargs="*")
    parser.add_argument("--out", type=Path, help="output directory; an existing rows.jsonl in it is resumed")
    parser.add_argument("--rows", type=Path, help="do not search: summarize and compare an existing rows file")
    parser.add_argument("--compare", nargs=2, action="append", default=[], metavar=("A", "B"),
                        help="paired exact McNemar on hit@5 between two arms; repeatable")
    parser.add_argument("--read-provider", default="http", help="provider for page reads; 'auto' lets the router choose")
    parser.add_argument("--read-gap", type=float, default=2.0, help="seconds between page reads of one origin")
    parser.add_argument("--read-concurrency", type=int, default=3)
    parser.add_argument("--no-reads", action="store_true", help="score from titles, snippets and result dates only")
    parser.add_argument("--search-timeout", type=float, default=120.0)
    args = parser.parse_args(argv)
    logging.getLogger("pypdf").setLevel(logging.ERROR)  # PDF results are read for scoring; font warnings are noise
    if args.rows:
        rows = [json.loads(line) for line in args.rows.read_text(encoding="utf-8").splitlines() if line]
        rows = [r for r in rows if args.set == "all" or r["set"] == args.set]
    else:
        queries = load_queries(args.set)
        if args.only_category:
            queries = [q for q in queries if q["category"] in args.only_category]
        if args.limit_queries and args.limit_queries < len(queries):
            queries = interleave(queries)[:args.limit_queries]
        out_dir = (args.out or EVALS / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / "rows.jsonl"
        previous = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line] \
            if out.exists() else []
        wanted = {q["id"] for q in queries}
        rows = [r for r in previous if r["query_id"] in wanted] + asyncio.run(run(queries, args, out, previous))
        print(f"\nrows: {out}")
    if not rows:
        sys.exit("no rows")
    result = report(rows, args.compare)
    if not args.rows:
        (out.parent / "summary.json").write_text(json.dumps(result, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
