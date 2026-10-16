"""Map a site's URLs, and crawl a site: Firecrawl's map and crawl, read through Frankensurf.

map: what the site says it has (robots.txt's sitemaps, or /sitemap.xml, walking sitemap indexes)
plus the links on the start page, one site, in order, once each.

crawl: a breadth-first walk from a start URL over same-site links (and, by default, the site's
sitemap), up to max_depth link hops and limit pages. Every page is a normal Frankensurf read: paced
per site, checked, climbing to a stronger tool when walled, with a receipt. robots.txt is honoured
unless the caller turns it off. Web standards only; nothing here knows a site.
"""
from __future__ import annotations

import asyncio
import re
import time
from urllib import robotparser
from urllib.parse import urlparse, urlunparse

import httpx

from .site_feeds import parse_sitemap, robots_sitemaps

# Files a crawl never reads as pages.
_NOT_PAGES = re.compile(r"(?i)\.(jpe?g|png|gif|webp|avif|svg|ico|css|js|mjs|json|xml|zip|gz|tgz|rar|7z|"
                        r"mp4|webm|mov|mp3|wav|ogg|woff2?|ttf|eot|exe|dmg|apk|iso)$")


def _same_site(host: str, root: str, subdomains: bool) -> bool:
    host, root = (host or "").lower().removeprefix("www."), (root or "").lower().removeprefix("www.")
    return host == root or (subdomains and host.endswith("." + root))


def _normal(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc.lower(), parsed.path or "/", "", parsed.query, ""))


async def _fetch_text(web, url: str) -> str | None:
    """A small paced GET for robots.txt and sitemaps (not pages): the text on 200, else None."""
    from .runtime import _refuse_private, _validate_url
    _validate_url(url)
    if web.block_private_network:
        await _refuse_private(url)
    host = urlparse(url).netloc
    delay = web._domain_next.get(host, 0) - time.monotonic()
    if delay > 0:
        await asyncio.sleep(delay)
    web._domain_next[host] = time.monotonic() + max(web._domain_delay, 0.5)
    try:
        response = await web._http.get(url, timeout=20)
    except httpx.HTTPError:
        return None
    if response.status_code != 200 or len(response.content) > 50_000_000:
        return None
    return response.text


async def _robots(web, origin: str):
    text = await _fetch_text(web, origin + "/robots.txt")
    rules = robotparser.RobotFileParser()
    rules.parse((text or "").splitlines())
    return rules, robots_sitemaps(text or "", origin)


async def _sitemap_urls(web, sitemaps: list[str], limit: int, max_sitemaps: int) -> list[str]:
    queue, seen, found = list(sitemaps), set(), []
    while queue and len(seen) < max_sitemaps and len(found) < limit:
        sitemap = queue.pop(0)
        if sitemap in seen:
            continue
        seen.add(sitemap)
        body = await _fetch_text(web, sitemap)
        parsed = parse_sitemap(body) if body else None
        if not parsed:
            continue
        queue.extend(child["url"] for child in parsed["sitemaps"] if child.get("url"))
        found.extend(page["url"] for page in parsed["pages"] if page.get("url"))
    return found


# Where sites keep a sitemap when robots.txt doesn't name one.
SITEMAP_PATHS = ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml")


def _default_sitemaps(origin: str) -> list[str]:
    return [origin + path for path in SITEMAP_PATHS]


def _rank(urls: list[str], search: str | None) -> list[str]:
    if not search:
        return urls
    words = [word for word in re.findall(r"\w+", search.lower()) if len(word) > 1]
    scored = [(sum(word in url.lower() for word in words), index, url) for index, url in enumerate(urls)]
    return [url for score, _, url in sorted(scored, key=lambda row: (-row[0], row[1])) if score]


async def site_map(web, url: str, *, limit: int = 5000, search: str | None = None,
                   include_subdomains: bool = False, use_sitemap: bool = True, use_page_links: bool = True,
                   max_sitemaps: int = 20, policy_overrides: dict | None = None) -> dict:
    """{"url", "links": [urls], "sources": {"sitemap": n, "page": n}}: a site's URLs, without reading them."""
    parsed = urlparse(url)
    origin, root = f"{parsed.scheme}://{parsed.netloc}", parsed.hostname or ""
    found, sources = [], {"sitemap": 0, "page": 0}
    if use_sitemap:
        _, sitemaps = await _robots(web, origin)
        from_maps = await _sitemap_urls(web, sitemaps or _default_sitemaps(origin), limit * 2, max_sitemaps)
        sources["sitemap"] = len(from_maps)
        found += from_maps
    if use_page_links:
        page = await web.read(url, policy_overrides={**(policy_overrides or {}), "include_links": True,
                                                     "completeness_escalation": False})
        links = [link["url"] for link in page.get("links") or []]
        sources["page"] = len(links)
        found += [url] + links
    seen, links = set(), []
    for candidate in found:
        host = urlparse(candidate).hostname
        key = _normal(candidate)
        if key in seen or not _same_site(host, root, include_subdomains) or _NOT_PAGES.search(urlparse(candidate).path):
            continue
        seen.add(key)
        links.append(key)
    return {"url": origin, "links": _rank(links, search)[:limit], "sources": sources}


async def crawl(web, url: str, *, limit: int = 100, max_depth: int = 2, include: list[str] | None = None,
                exclude: list[str] | None = None, include_subdomains: bool = False, use_sitemap: bool = True,
                respect_robots: bool = True, entire_domain: bool = False, concurrency: int = 4,
                policy_overrides: dict | None = None, on_page=None) -> dict:
    """Read a site breadth-first. Like Firecrawl, only pages under the start URL's path are read
    (https://a.example/docs/ reads /docs/...) unless entire_domain is set. include/exclude are regular
    expressions matched against each URL's path; on_page(page) is called (awaited if it is a
    coroutine function) as each page finishes."""
    parsed = urlparse(url)
    origin, root = f"{parsed.scheme}://{parsed.netloc}", parsed.hostname or ""
    scope = "" if entire_domain else (parsed.path or "/").rstrip("/")
    wanted = [re.compile(pattern) for pattern in include or []]
    unwanted = [re.compile(pattern) for pattern in exclude or []]
    rules, sitemaps = await _robots(web, origin) if (respect_robots or use_sitemap) else (None, [])
    overrides = {**(policy_overrides or {}), "include_links": True, "markdown_output": True}
    skipped = {"robots": 0, "filtered": 0}

    def allowed(candidate: str, start: bool = False) -> bool:
        target = urlparse(candidate)
        if not _same_site(target.hostname, root, include_subdomains) or _NOT_PAGES.search(target.path):
            return False
        path = target.path or "/"
        if scope and not start and path.rstrip("/") != scope and not path.startswith(scope + "/"):
            return False
        if not start and (any(p.search(target.path) for p in unwanted)
                          or (wanted and not any(p.search(target.path) for p in wanted))):
            skipped["filtered"] += 1
            return False
        if respect_robots and rules is not None and not rules.can_fetch("FrankenSurf", candidate):
            skipped["robots"] += 1
            return False
        return True

    queue: list[tuple[str, int]] = []
    seen: set[str] = set()

    def enqueue(candidate: str, depth: int, start: bool = False):
        key = _normal(candidate)
        if key not in seen and allowed(key, start):
            seen.add(key)
            queue.append((key, depth))

    enqueue(url, 0, start=True)
    if use_sitemap:
        for candidate in await _sitemap_urls(web, sitemaps or _default_sitemaps(origin), limit * 2, 20):
            if max_depth >= 1:
                enqueue(candidate, 1)
    pages, gate = [], asyncio.Semaphore(max(1, concurrency))

    async def visit(candidate: str, depth: int):
        async with gate:
            result = await web.read(candidate, policy_overrides=overrides)
        receipt = result.get("receipt") or {}
        page = {"url": candidate, "final_url": result.get("url"), "depth": depth,
                "status": receipt.get("status"), "title": result.get("title"),
                "markdown": result.get("markdown") or result.get("text") or "",
                "failure": receipt.get("failure"), "trace_id": receipt.get("trace_id"),
                "cost_usd": receipt.get("cost_usd") or 0}
        if depth < max_depth:
            for link in result.get("links") or []:
                enqueue(link["url"], depth + 1)
        return page

    while queue and len(pages) < limit:
        batch = []
        while queue and len(pages) + len(batch) < limit:
            batch.append(queue.pop(0))
        for page in await asyncio.gather(*(visit(candidate, depth) for candidate, depth in batch)):
            pages.append(page)
            if on_page is not None:
                outcome = on_page(page)
                if asyncio.iscoroutine(outcome):
                    await outcome
    return {"url": url, "pages": pages, "completed": len(pages),
            "read": sum(1 for page in pages if page["status"] == "observed"),
            "failed": sum(1 for page in pages if page["status"] != "observed"),
            "left_in_queue": len(queue), "skipped": skipped,
            "cost_usd": round(sum(page["cost_usd"] for page in pages), 6)}
