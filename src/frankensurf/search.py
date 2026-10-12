"""Validated free search adapters. Search observations are indexed discovery only.

Web sources (SearXNG, DuckDuckGo, Bing) answer general queries. Vertical
sources are official, keyless public APIs for one kind of result: news (Bing
News RSS), reference (Wikipedia), discussions (Hacker News via Algolia), Q&A
(Stack Exchange), code (GitHub), papers (arXiv) and books (Open Library). The
site vertical (site_watch.py) reads one organisation's own feeds and sitemaps.
Results carry an ISO ``published`` date when the source returns one.

Search options (normalize_options) travel with every request: site,
exclude_domains, recency and region. Each source applies what it can natively
or as query operators; the router filters every result list afterwards, so an
excluded domain never comes back from any source.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import html
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlencode, urlparse, urljoin, parse_qs, quote

from bs4 import BeautifulSoup

REGISTRY = {
    "searxng": {"kind": "json", "base_url": "http://127.0.0.1:8088", "path": "/search", "endpoint_scope": "loopback self-hosted", "vertical": "web"},
    "duckduckgo_html": {"kind": "html", "base_url": "https://html.duckduckgo.com", "path": "/html/", "endpoint_scope": "public search", "vertical": "web"},
    "bing_rss": {"kind": "rss", "base_url": "https://www.bing.com", "path": "/search", "endpoint_scope": "public search", "vertical": "web"},
    "bing_news_rss": {"kind": "rss", "base_url": "https://www.bing.com", "path": "/news/search", "endpoint_scope": "public search", "vertical": "news"},
    "wikipedia": {"kind": "json", "base_url": "https://{lang}.wikipedia.org", "path": "/w/api.php", "endpoint_scope": "official public API", "vertical": "reference"},
    "hacker_news": {"kind": "json", "base_url": "https://hn.algolia.com", "path": "/api/v1/search", "endpoint_scope": "official public API", "vertical": "discussions"},
    "stack_exchange": {"kind": "json", "base_url": "https://api.stackexchange.com", "path": "/2.3/search/advanced", "endpoint_scope": "official public API", "vertical": "qa"},
    "github": {"kind": "json", "base_url": "https://api.github.com", "path": "/search/repositories", "endpoint_scope": "official public API", "vertical": "code"},
    "arxiv": {"kind": "rss", "base_url": "https://export.arxiv.org", "path": "/api/query", "endpoint_scope": "official public API", "vertical": "papers"},
    "open_library": {"kind": "json", "base_url": "https://openlibrary.org", "path": "/search.json", "endpoint_scope": "official public API", "vertical": "books"},
}
VERTICALS = ("web", "news", "reference", "discussions", "qa", "code", "papers", "books", "site")
RECENCY = {"day": 1, "week": 7, "month": 31, "year": 366}
_OPTION_KEYS = {"site", "exclude_domains", "recency", "region"}
# Most -site: operators put in a query (WebPolicy.search_exclude_operators); the
# full exclude list is enforced on the results by filter_results.
MAX_EXCLUDE_OPERATORS = 3
_HOST = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
_REGION = re.compile(r"[a-z]{2}(?:-[A-Z]{2})?")


def _host(value):
    text = str(value or "").strip().lower()
    if "://" in text:
        text = urlparse(text).hostname or ""
    text = text.split("/")[0].removeprefix("www.").rstrip(".")
    if not _HOST.fullmatch(text):
        raise ValueError("Invalid domain in search options: " + str(value)[:80])
    return text


def normalize_options(site=None, exclude_domains=None, recency=None, region=None) -> dict:
    """Validated search options; empty ones are left out."""
    options = {}
    if site:
        options["site"] = _host(site)
    if exclude_domains:
        if isinstance(exclude_domains, str):
            exclude_domains = [exclude_domains]
        if not isinstance(exclude_domains, (list, tuple)) or len(exclude_domains) > 200:
            raise ValueError("exclude_domains is a list of up to 200 domains")
        options["exclude_domains"] = sorted({_host(item) for item in exclude_domains})
    if recency:
        if recency not in RECENCY:
            raise ValueError("recency must be day, week, month or year")
        options["recency"] = recency
    if region:
        if not isinstance(region, str) or not _REGION.fullmatch(region):
            raise ValueError("region looks like en-AU or de")
        options["region"] = region
    return options


def matches_domain(url, domain) -> bool:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    return host == domain or host.endswith("." + domain)


def filter_results(results, options, now=None):
    """Apply options after the fact: excluded domains and other sites never
    come back, and dated results older than the recency window are dropped
    (undated ones stay; the receipt says how many were undated)."""
    now = now or datetime.now(timezone.utc)
    kept, dropped, undated = [], {"excluded": 0, "off_site": 0, "too_old": 0}, 0
    cutoff = now - timedelta(days=RECENCY[options["recency"]]) if options.get("recency") else None
    for item in results:
        url = item.get("url") or ""
        if any(matches_domain(url, domain) for domain in options.get("exclude_domains", ())):
            dropped["excluded"] += 1
            continue
        if options.get("site") and not matches_domain(url, options["site"]):
            dropped["off_site"] += 1
            continue
        if cutoff is not None:
            when = parse_date(item.get("published") or item.get("indexed_date"))
            if when is None:
                undated += 1
            elif when < cutoff:
                dropped["too_old"] += 1
                continue
        kept.append(item)
    return kept, {**dropped, "undated": undated}


def parse_date(value):
    """A result date as an aware datetime: ISO 8601, RFC 2822 or a Unix time."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(float(value), timezone.utc)
        text = str(value).strip()
        if re.fullmatch(r"\d{9,11}", text):
            return datetime.fromtimestamp(int(text), timezone.utc)
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            parsed = parsedate_to_datetime(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _operators(query, options, *, exclude=True, max_exclude=MAX_EXCLUDE_OPERATORS):
    """site: and -site: operators, for engines that read them in the query."""
    parts = [query]
    if options.get("site"):
        parts.append("site:" + options["site"])
    if exclude:
        # Engines return nothing for a long -site: list, so only the first few go
        # in the query; the router's filter drops every excluded domain afterwards.
        budget = 1500 - len(query)
        for domain in list(options.get("exclude_domains", ()))[:max_exclude]:
            term = "-site:" + domain
            if budget - len(term) - 1 < 0:
                break
            parts.append(term)
            budget -= len(term) + 1
    return " ".join(parts)


def build_search(query: str, source: str, config: dict | None = None, *, now=None):
    if source not in REGISTRY: raise ValueError("Unknown search adapter")
    if not isinstance(query, str) or not query.strip() or len(query) > 2000: raise ValueError("Invalid search query")
    config = config or {}
    if set(config) - {"base_url", "engines", "language", "max_exclude_operators"} - _OPTION_KEYS: raise ValueError("Unknown engine configuration")
    options = normalize_options(**{key: config.get(key) for key in _OPTION_KEYS})
    max_exclude = config.get("max_exclude_operators", MAX_EXCLUDE_OPERATORS)
    if type(max_exclude) is not int or not 0 <= max_exclude <= 20: raise ValueError("max_exclude_operators is 0 to 20")
    definition = REGISTRY[source]
    region = options.get("region") or config.get("language") or "en-AU"
    lang, _, country = region.partition("-")
    default_base = definition["base_url"].replace("{lang}", lang)
    base = config.get("base_url") or default_base
    parsed = urlparse(base)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password: raise ValueError("Invalid search endpoint")
    if source == "searxng" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}: raise ValueError("This release uses self-hosted loopback SearXNG only")
    if source != "searxng" and base != default_base: raise ValueError("Public search endpoints are fixed by validated adapter")
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=RECENCY[options["recency"]]) if options.get("recency") else None
    recency = options.get("recency")
    if source == "searxng":
        params = {"q": _operators(query, options, max_exclude=max_exclude), "format": "json", "language": region}
        if config.get("engines"): params["engines"] = config["engines"]
        if recency: params["time_range"] = recency
    elif source == "duckduckgo_html":
        params = {"q": _operators(query, options, max_exclude=max_exclude)}
        if recency: params["df"] = recency[0]
        if country: params["kl"] = country.lower() + "-" + lang
    elif source in ("bing_rss", "bing_news_rss"):
        params = {"q": _operators(query, options, max_exclude=max_exclude), "format": "rss"}
        if country: params.update(cc=country, setlang=lang)
        if recency and source == "bing_rss":
            params["filters"] = 'ex1:"ez%d"' % {"day": 1, "week": 2, "month": 3}.get(recency, 5)
        elif recency:
            params["qft"] = 'interval="%d"' % {"day": 7, "week": 8, "month": 9}.get(recency, 9)
    elif source == "wikipedia":
        params = {"action": "query", "list": "search", "srsearch": query, "format": "json",
                  "srlimit": 20, "srprop": "snippet|timestamp"}
    elif source == "hacker_news":
        params = {"query": _operators(query, {}, exclude=False), "tags": "story", "hitsPerPage": 30}
        if since: params["numericFilters"] = "created_at_i>%d" % since.timestamp()
    elif source == "stack_exchange":
        # Any Stack Exchange site by its API name (config "engines"), Stack Overflow by default.
        params = {"order": "desc", "sort": "relevance", "q": query,
                  "site": config.get("engines") or "stackoverflow", "pagesize": 30}
        if since: params["fromdate"] = int(since.timestamp())
    elif source == "github":
        terms = query + (" pushed:>" + since.date().isoformat() if since else "")
        params = {"q": terms, "per_page": 30}
    elif source == "arxiv":
        # Relevance within the window: sorted by date, any paper that mentions the
        # words anywhere outranks the ones about the topic.
        terms = _arxiv_query(query)
        if since:
            terms += " AND submittedDate:[%s TO %s]" % (since.strftime("%Y%m%d%H%M"), now.strftime("%Y%m%d%H%M"))
        params = {"search_query": terms, "max_results": 30, "sortBy": "relevance", "sortOrder": "descending"}
    else:  # open_library
        params = {"q": query, "limit": 30}
    url = base.rstrip("/") + definition["path"] + "?" + urlencode(params, quote_via=quote)
    return url, {**definition, "options": options}


_ARXIV_FILLER = frozenset({"a", "an", "the", "of", "on", "in", "for", "to", "and", "or", "with", "by", "from", "paper", "papers", "preprint", "preprints", "arxiv", "research", "study", "studies"})


def _arxiv_query(query):
    """arXiv applies a field prefix to one term only, so "all:recent papers on X"
    matches almost anything and, sorted by date, returns the newest papers. Every
    topic word gets its own prefix and all must match; the words as a phrase rank first."""
    from .completeness import _STOPWORDS
    from .search_checks import _QUERY_FILLER
    words = [word for word in re.findall(r"[\w][\w\-]*", query.lower())
             if word not in _STOPWORDS and word not in _QUERY_FILLER and word not in _ARXIV_FILLER
             and not re.fullmatch(r"(19|20)\d\d", word)]
    words = words[:8]
    if not words:
        return "all:" + query
    every = " AND ".join("all:" + word for word in words)
    return '(all:"%s" OR (%s))' % (" ".join(words), every) if len(words) > 1 else every


def _iso(value):
    when = parse_date(value)
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if when else None


def _text(value):
    return " ".join(BeautifulSoup(html.unescape(str(value or "")), "html.parser").get_text(" ").split())


def normalize_results(content: str, structured: object, source: str, limit: int, request_url: str | None = None):
    upstream_failures = []
    found = []
    if source == "searxng":
        if not isinstance(structured, dict) or not isinstance(structured.get("results"), list):
            raise ValueError("Expected SearXNG search results array")
        upstream_failures = structured.get("unresponsive_engines", [])
        for item in structured["results"]:
            if not isinstance(item, dict): continue
            found.append({"url": item.get("url"), "title": item.get("title") or "", "snippet": item.get("content") or "", "engines": item.get("engines") or ([item["engine"]] if item.get("engine") else []), "indexed_date": item.get("publishedDate"), "published": _iso(item.get("publishedDate"))})
    elif source in ("bing_rss", "bing_news_rss"):
        try: root = ET.fromstring(content)
        except ET.ParseError as exc: raise ValueError("Expected search RSS") from exc
        if root.tag != "rss": raise ValueError("Expected RSS root")
        for item in root.findall("./channel/item"):
            link = item.findtext("link") or ""
            # News links go through a Bing click URL that names the article.
            target = parse_qs(urlparse(link).query).get("url") if "apiclick" in link else None
            entry = {"url": target[0] if target else link, "title": item.findtext("title") or "",
                     "snippet": item.findtext("description") or "",
                     "engines": ["bing_news" if source == "bing_news_rss" else "bing"],
                     "indexed_date": item.findtext("pubDate"), "published": _iso(item.findtext("pubDate"))}
            publisher = next((child.text for child in item if child.tag.endswith("}Source")), None)
            if publisher:
                entry["extra"] = {"publisher": publisher}
            found.append(entry)
    elif source == "wikipedia":
        rows = ((structured or {}).get("query") or {}).get("search") if isinstance(structured, dict) else None
        if not isinstance(rows, list): raise ValueError("Expected Wikipedia search results")
        parsed = urlparse(request_url or "https://en.wikipedia.org")
        host = "https://" + (parsed.hostname if (parsed.hostname or "").endswith(".wikipedia.org")
                             else "en.wikipedia.org")
        for row in rows:
            if isinstance(row, dict) and row.get("title"):
                found.append({"url": host + "/wiki/" + quote(str(row["title"]).replace(" ", "_")),
                              "title": row["title"], "snippet": _text(row.get("snippet")),
                              "engines": ["wikipedia"], "indexed_date": row.get("timestamp")})
    elif source == "hacker_news":
        rows = structured.get("hits") if isinstance(structured, dict) else None
        if not isinstance(rows, list): raise ValueError("Expected Hacker News hits")
        for row in rows:
            if not isinstance(row, dict) or not row.get("objectID"): continue
            discussion = "https://news.ycombinator.com/item?id=" + str(row["objectID"])
            found.append({"url": row.get("url") or discussion, "title": row.get("title") or "",
                          "snippet": _text(row.get("story_text"))[:300], "engines": ["hacker_news"],
                          "indexed_date": row.get("created_at"), "published": _iso(row.get("created_at")),
                          "extra": {"discussion_url": discussion, "points": row.get("points"),
                                    "comments": row.get("num_comments")}})
    elif source == "stack_exchange":
        rows = structured.get("items") if isinstance(structured, dict) else None
        if not isinstance(rows, list): raise ValueError("Expected Stack Exchange items")
        for row in rows:
            if isinstance(row, dict) and row.get("link"):
                found.append({"url": row["link"], "title": _text(row.get("title")),
                              "snippet": " ".join(str(tag) for tag in row.get("tags") or [])[:200],
                              "engines": ["stack_exchange"],
                              "indexed_date": _iso(row.get("last_activity_date")),
                              "published": _iso(row.get("creation_date")),
                              "extra": {"answered": row.get("is_answered"), "score": row.get("score")}})
    elif source == "github":
        rows = structured.get("items") if isinstance(structured, dict) else None
        if not isinstance(rows, list): raise ValueError("Expected GitHub items")
        for row in rows:
            if isinstance(row, dict) and row.get("html_url"):
                found.append({"url": row["html_url"], "title": row.get("full_name") or "",
                              "snippet": row.get("description") or "", "engines": ["github"],
                              "indexed_date": row.get("pushed_at"),
                              "published": _iso(row.get("pushed_at") or row.get("created_at")),
                              "extra": {"stars": row.get("stargazers_count"), "created_at": _iso(row.get("created_at"))}})
    elif source == "arxiv":
        try: root = ET.fromstring(content)
        except ET.ParseError as exc: raise ValueError("Expected arXiv Atom") from exc
        atom = "{http://www.w3.org/2005/Atom}"
        if root.tag != atom + "feed": raise ValueError("Expected Atom feed")
        for entry in root.findall(atom + "entry"):
            found.append({"url": (entry.findtext(atom + "id") or "").strip(),
                          "title": " ".join((entry.findtext(atom + "title") or "").split()),
                          "snippet": " ".join((entry.findtext(atom + "summary") or "").split())[:400],
                          "engines": ["arxiv"], "indexed_date": entry.findtext(atom + "published"),
                          "published": _iso(entry.findtext(atom + "published"))})
    elif source == "open_library":
        rows = structured.get("docs") if isinstance(structured, dict) else None
        if not isinstance(rows, list): raise ValueError("Expected Open Library docs")
        for row in rows:
            if isinstance(row, dict) and isinstance(row.get("key"), str) and row["key"].startswith("/"):
                authors = ", ".join(str(name) for name in (row.get("author_name") or [])[:3])
                year = row.get("first_publish_year")
                found.append({"url": "https://openlibrary.org" + row["key"], "title": row.get("title") or "",
                              "snippet": " · ".join(part for part in (authors, str(year) if year else "") if part),
                              "engines": ["open_library"], "indexed_date": None})
    else:
        soup = BeautifulSoup(content, "html.parser")
        for item in soup.select(".result"):
            link = item.select_one(".result__a")
            if not link: continue
            url = urljoin("https://html.duckduckgo.com", link.get("href") or "")
            redirected = parse_qs(urlparse(url).query).get("uddg")
            if redirected: url = redirected[0]
            snippet = item.select_one(".result__snippet")
            found.append({"url": url, "title": link.get_text(" ", strip=True), "snippet": snippet.get_text(" ", strip=True) if snippet else "", "engines": ["duckduckgo"], "indexed_date": None})
        if not found and "no results" not in soup.get_text(" ", strip=True).lower():
            raise ValueError("Search selectors absent; empty result unverified")
    results, seen = [], set()
    for item in found:
        url = item["url"]
        if not isinstance(url, str): continue
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or url in seen: continue
        seen.add(url)
        if item.get("published") is None:
            item.pop("published", None)
        results.append(item)
        if len(results) >= limit: break
    return results, upstream_failures
