"""site_watch: recent items from one organisation's own site.

A free search source for the "site" vertical. It reads what the site publishes
about itself: feeds advertised on the homepage (<link rel="alternate">), common
feed paths, robots.txt Sitemap lines, /sitemap.xml and news sitemaps. Items are
filtered by the query's key terms and the recency window, newest first.

The site comes from the ``site`` (or ``domain``) option, or from a domain-like
token in the query ("anthropic.com model release"). Nothing here knows a site.
Every fetch goes through the search services' read, so the project's HTTP
client, per-origin pacing and cool-downs apply, and the number of requests is
small and bounded.
"""
from __future__ import annotations

import math
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

from .search import RECENCY, _host, _iso, _text, matches_domain, parse_date
from .site_feeds import (CONVENTIONAL_FEEDS, find_feeds, page_key, parse_feed, parse_sitemap,
                         pick_child_sitemaps, robots_sitemaps)

MAX_READS = 8
# Feed paths worth a probe when the homepage advertises none; each miss costs a paced request.
EXTRA_FEED_PATHS = ("/blog/feed", "/news/feed")
PROBE_FEEDS = 4
SITEMAP_GUESSES = ("/sitemap.xml", "/sitemap_index.xml")
# Not content words: they describe the request, not the page.
_STOP = frozenset("a an the of on for and or in to from with by about at is are new newest latest recent "
                  "recently news post posts blog update updates site website page pages".split())
_CC_TLDS = frozenset("io ai co me tv fm ly to us uk au nz ca de fr jp in eu nl se no ch es it br mx kr cn "
                     "ru pl dk fi ie be at sg hk za".split())
_FILE_TLDS = frozenset("html htm json yaml yml toml txt pdf png jpg jpeg gif svg docx xlsx pptx zip tar "
                       "gzip md csv xml php asp aspx jsp exe dll bat java rust lock conf cfg ini log".split())


def _clean_token(token):
    return token.strip("\"'()[]{}<>,;!?").rstrip(".:")


def _domain_like(token):
    """A host if the token plainly names one (URL, site:, www., or a known-looking TLD)."""
    text = _clean_token(token)
    explicit = False
    if text.lower().startswith("site:"):
        text, explicit = text[5:], True
    if "://" in text:
        explicit = True
    elif text.lower().startswith("www."):
        explicit = True
    try:
        host = _host(text)
    except ValueError:
        return None
    tld = host.rsplit(".", 1)[1]
    if explicit:
        return host
    if tld in _FILE_TLDS:
        return None
    if len(tld) == 2 and tld not in _CC_TLDS:
        return None
    return host


def infer_site(query):
    """(host, query without the domain token), or (None, query)."""
    words = query.split()
    for index, word in enumerate(words):
        host = _domain_like(word)
        if host:
            return host, " ".join(words[:index] + words[index + 1:])
    return None, query


def key_terms(query):
    terms = []
    for word in re.findall(r"[^\W_]+", query.lower()):
        if len(word) >= 2 and word not in _STOP and word not in terms:
            terms.append(word)
    return terms


def _matches(item, terms):
    if not terms:
        return True
    haystack = " ".join((item.get("title") or "", item.get("snippet") or "",
                         urlparse(item["url"]).path.replace("-", " ").replace("_", " "))).lower()
    found = sum(1 for term in terms if term in haystack)
    return found >= (len(terms) if len(terms) <= 2 else math.ceil(len(terms) * 0.6))


def _slug_title(url):
    slug = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    slug = re.sub(r"\.[a-z0-9]{2,5}$", "", slug)
    return " ".join(slug.replace("-", " ").replace("_", " ").split()).capitalize()


class SiteWatchPlugin:
    def __init__(self):
        from .search_plugins import SearchManifest
        # Feeds and sitemaps are raw XML: ask for the plain HTTP bytes, like the bundled RSS sources.
        self.manifest = SearchManifest("site_watch", "1", transport_provider="http", vertical="site")

    async def search(self, request, services):
        from .runtime import WebFailure, _total_cost
        config = request.config or {}
        query = request.query
        explicit = config.get("site") or config.get("domain")
        if explicit:
            try:
                site = _host(explicit)
            except ValueError:
                raise WebFailure("INVALID_URL", "site_watch: invalid site option; pass a domain such as example.com") from None
            inferred, remainder = infer_site(query)
            if inferred == site:
                query = remainder
        else:
            site, query = infer_site(query)
        if not site:
            raise WebFailure("INVALID_URL", "site_watch needs a site: pass site=example.com or name "
                             "a domain in the query (for example 'example.com pricing')")
        terms = key_terms(query)
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=RECENCY[config["recency"]]) if config.get("recency") else None
        policy = request.policy
        deadline = time.monotonic() + max(1.0, float(policy.timeout_seconds)) * 0.7
        origin = "https://" + site
        receipts, acquisitions, failures = [], [], []
        hosts = {site}

        async def fetch(url, adapter):
            if len(receipts) >= MAX_READS or time.monotonic() > deadline:
                return None
            try:
                acquisition = await services.read(url, policy, adapter=adapter)
            except WebFailure as error:
                if error.code in ("POLICY_DENIED", "BUDGET_EXHAUSTED"):
                    raise
                failures.append([url, error.code])
                return None
            receipt = acquisition["receipt"]
            receipts.append(receipt)
            acquisitions.append(acquisition)
            if receipt.get("status") != "observed":
                code = (receipt.get("failure") or {}).get("code") or "PROVIDER_DOWN"
                if code != "NOT_FOUND":
                    failures.append([url, code])
                return None
            return acquisition

        def entries(feed_entries, via):
            out = []
            for entry in feed_entries:
                link = (entry.get("url") or "").strip()
                if not link or urlparse(link).scheme not in ("http", "https"):
                    continue
                if not any(matches_domain(link, host) for host in hosts):
                    continue
                when = entry.get("published")
                parsed = parse_date(when)
                if cutoff is not None and parsed is not None and parsed < cutoff:
                    continue
                out.append({"url": link, "title": entry.get("title") or _slug_title(link),
                            "snippet": _text(entry.get("summary"))[:300], "engines": ["site_watch"],
                            "indexed_date": when, "published": when,
                            "extra": {"discovered_via": via}, "_when": parsed})
            return out

        found, produced, discovered = [], None, False
        # 1. The homepage names its feeds.
        home = await fetch(origin + "/", None)
        base = origin + "/"
        if home is not None:
            base = home.get("url") or base
            final = (urlparse(base).hostname or "").lower().removeprefix("www.")
            if final:
                hosts.add(final)
            advertised = find_feeds(home.get("content") or "", base)
        else:
            advertised = []
        # 2. Advertised feeds, then (only if none was advertised) conventional paths.
        probes = advertised[:2] or [urljoin(base, path) for path in
                                    (CONVENTIONAL_FEEDS + EXTRA_FEED_PATHS)][:PROBE_FEEDS]
        for url in probes:
            acquisition = await fetch(url, "rss")
            parsed_feed = parse_feed(acquisition.get("content") or "") if acquisition else None
            if parsed_feed is None:
                continue
            discovered = True
            produced = produced or acquisition
            found += entries(parsed_feed, "feed")
            if not advertised and found:
                break
        # 3. Sitemaps, when no feed gave anything.
        if not found:
            robots = await fetch(origin + "/robots.txt", None)
            maps = robots_sitemaps(robots.get("content") or "", origin + "/") if robots else []
            maps = maps or [urljoin(origin, path) for path in SITEMAP_GUESSES]
            queue = list(maps[:3])
            while queue and not found:
                url = queue.pop(0)
                acquisition = await fetch(url, "rss")
                parsed_map = parse_sitemap(acquisition.get("content") or "") if acquisition else None
                if parsed_map is None:
                    continue
                discovered = True
                if parsed_map["sitemaps"]:
                    queue = pick_child_sitemaps(parsed_map["sitemaps"], 2) + queue
                    continue
                produced = produced or acquisition
                found += entries(parsed_map["pages"], "sitemap")

        results, seen = [], set()
        for item in sorted(found, key=lambda item: item["_when"] or datetime.min.replace(tzinfo=timezone.utc),
                           reverse=True):
            key = page_key(item["url"])
            if key in seen or not _matches(item, terms):
                continue
            seen.add(key)
            item.pop("_when")
            item["indexed_date"] = item["published"] = _iso(item["published"])
            if item["published"] is None:
                item.pop("published")
            results.append(item)
        # Undated entries sort last (the sort above puts them at the end).
        results = results[:request.limit]

        if not receipts:
            raise WebFailure("TIMEOUT", "site_watch ran out of time before reading " + site)
        observed = [item for item in acquisitions if item["receipt"].get("status") == "observed"]
        primary = produced or (observed[0] if observed else acquisitions[-1])
        receipt = primary["receipt"]
        receipt["attempts"] = [attempt for item in receipts for attempt in item.get("attempts", [])]
        receipt["cost_usd"] = _total_cost([item.get("cost_usd") for item in receipts])
        receipt["operation"] = "search"
        receipt["search_source"] = self.manifest.id
        receipt["search_source_version"] = self.manifest.version
        url = primary.get("url") or origin + "/"
        attribution = {"query": request.query, "adapter": self.manifest.id, "source": self.manifest.id,
                       "source_version": self.manifest.version, "request_url": url}
        response = {"query": request.query, "source": self.manifest.id, "results": [],
                    "receipt": receipt, "query_attribution": attribution,
                    "coverage": "unknown", "upstream_failures": failures[:20]}
        if receipt.get("status") == "observed" and not discovered:
            receipt.update(status="failed", failure={"code": "SEARCH_UNAVAILABLE",
                "message": "No feed or sitemap found for " + site})
        if receipt.get("status") == "observed":
            response["coverage"] = "partial" if failures else "returned-results"
            response["results"] = [{**item, "listing_state": "unknown", "verification": "indexed_discovery",
                                    "query_attribution": attribution} for item in results]
        if receipt.get("failure") and receipt["attempts"]:
            receipt["attempts"][-1].update(status="failed", failure=receipt["failure"]["code"])
        return {"response": response, "acquisition": primary}
