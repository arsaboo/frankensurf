"""Checks on search results, the way the read path checks pages: is each result
real, on the query and inside the date window, and does the whole set deserve
to end the search. Pure functions over normalised results (url, title,
snippet, indexed_date); the router in Runtime.search decides what to do next.

A result fails when it is provably outside the date window, shares no key term
with the query, is a homepage, search page, tag listing or challenge page where
the query asked for specific items, or repeats another result's page. An
unknown date is "unknown", not a failure; it only ranks below dated results,
and enrich_dates reads the pages of undated results to find out.
"""
from __future__ import annotations
import asyncio
import calendar
import json
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, unquote, urlparse

from .completeness import _STOPWORDS, _WORD
from .search import RECENCY, parse_date

# Words that say how fresh or what kind of page, never what the query is about.
_QUERY_FILLER = frozenset({
    "news", "latest", "recent", "recently", "today", "tonight", "yesterday", "update", "updates",
    "week", "weeks", "month", "months", "year", "years", "day", "days", "this", "that", "these",
    "those", "what", "which", "who", "whom", "when", "where", "why", "how", "are", "was", "were",
    "has", "have", "had", "does", "did", "can", "will", "about", "into", "over", "after", "before",
    "between", "your", "you", "their", "there", "them", "than", "then", "also", "just", "some",
    "more", "most", "very", "now", "current", "currently", "top", "breaking", "report", "reports",
    "headlines", "happening", "happened", "past", "last", "should", "would", "could", "not"})
_OPERATOR = re.compile(r"\S+:\S*|(?<!\S)-\S+")
_YEAR = re.compile(r"(?:19|20)\d\d")

# Pages that are not the thing asked for.
_SEARCH_SEGMENTS = frozenset({"search", "searchresults", "results", "find", "query"})
_SEARCH_KEYS = frozenset({"q", "query", "search", "s", "keyword", "keywords", "term", "search_query"})
_LISTING_SEGMENTS = frozenset({"tag", "tags", "tagged", "category", "categories", "cat", "topic", "topics",
                               "label", "labels", "archive", "archives", "author", "authors"})
_SECTION_SEGMENTS = frozenset({"news", "blog", "blogs", "articles", "posts", "products", "shop", "store",
                               "stories", "videos", "reviews", "forum", "forums", "community", "latest",
                               "trending", "popular", "all", "index", "home", "default"})
# Words that name an index of items, not an item: the last path segment of a listing.
_INDEX_SEGMENTS = frozenset({
    "new", "news", "newsroom", "press", "press-releases", "press-release-archive", "releases",
    "release-notes", "changelog", "announcements", "updates", "headlines", "blog", "blogs", "articles",
    "stories", "archive", "archives", "category", "categories", "tag", "tags", "topics", "latest",
    "whats-new", "insights", "media", "events", "news-types"})
# Hyphenated runs of these ("news-press-releases", "press-release-archive") are listings
# too, when every word is one and at least one is a strong (plural or section) word:
# "new-release" can be a title, "news-types" cannot.
_STRONG_TOKENS = frozenset({
    "news", "newsroom", "press", "releases", "notes", "changelog", "announcements", "updates", "headlines",
    "blog", "blogs", "articles", "stories", "archive", "archives", "categories", "tags", "topics", "latest",
    "insights", "media", "events", "types"})
_INDEX_TOKENS = _STRONG_TOKENS | {"new", "release", "note", "announcement", "update", "headline", "article",
                                  "story", "category", "tag", "topic", "whats", "insight", "event", "type", "and"}
# Ids, page numbers and years that can follow an index word: /c/announcements/22.
_SHORT_ID = re.compile(r"\d{1,4}")
# An aggregator's entity page: /news/<one or two letters-only words>.
_ENTITY_SLUG = re.compile(r"[a-z]+(?:-[a-z]+)?")
_ID_KEYS = frozenset({"id", "p", "v", "item", "story", "article", "post", "tid", "t", "pid", "sku"})
_LOCALE = re.compile(r"[a-z]{2}(?:[-_][a-z]{2})?")
_CHALLENGE = ("just a moment", "attention required", "access denied", "verify you are human",
              "are you a robot", "captcha", "security check", "checking your browser", "403 forbidden",
              "404 not found", "page not found", "enable javascript", "ddos protection", "robot check",
              "pardon our interruption", "unusual traffic", "request blocked")
_TRACKING = re.compile(r"(?i)(utm_|fbclid$|gclid$|mc_|ref$|ref_|igshid$|cmpid$|spm$)")

_MONTHS = {name: number for number, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_MONTH = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_ISO = re.compile(r"(?<!\d)((?:19|20)\d\d)-(\d\d)-(\d\d)(?!\d)")
_DMY = re.compile(r"(?<!\d)(\d{1,2})(?:st|nd|rd|th)?\s+" + _MONTH + r",?\s+((?:19|20)\d\d)(?!\d)", re.I)
_MDY = re.compile(_MONTH + r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+((?:19|20)\d\d)(?!\d)", re.I)
_MD = re.compile(r"(?<![\w])" + _MONTH + r"\s+(\d{1,2})(?:st|nd|rd|th)?(?![\d:])(?!,?\s*(?:19|20)\d\d)", re.I)
_RELATIVE = re.compile(r"(?<!\w)(?:(\d{1,3})|an?|one)\s*(second|sec|minute|min|hour|hr|day|week|wk|month|mo|year|yr)s?\s+ago\b",
                       re.I)
_UNIT_DAYS = {"second": 0, "sec": 0, "minute": 0, "min": 0, "hour": 0, "hr": 0, "day": 1, "week": 7,
              "wk": 7, "month": 30, "mo": 30, "year": 365, "yr": 365}
_URL_DATE = re.compile(r"(?<!\d)((?:19|20)\d\d)([/-])(\d{1,2})\2(\d{1,2})(?!\d)")
_URL_MONTH = re.compile(r"/((?:19|20)\d\d)/(0[1-9]|1[0-2])(?:/|$)")


def key_terms(query: str) -> list[str]:
    """What the query is about: its words without operators, stopwords, years and
    freshness words (news, latest, this week), lightly stemmed."""
    terms = []
    for word in _WORD.findall(_OPERATOR.sub(" ", str(query or "")).lower()):
        if word in _STOPWORDS or word in _QUERY_FILLER or _YEAR.fullmatch(word):
            continue
        if len(word) < 3 and word.isascii():
            continue
        if word.endswith("es") and len(word) >= 5:
            word = word[:-2]
        elif word.endswith("s") and len(word) >= 4:
            word = word[:-1]
        if word not in terms:
            terms.append(word)
    return terms[:8]


def canonical_url(url: str) -> str:
    """One page, one key: scheme, www., default ports, fragments, trailing slashes,
    index files and tracking parameters don't make a different page."""
    parsed = urlparse(str(url or ""))
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = re.sub(r"/index\.(?:html?|php)$", "", parsed.path).rstrip("/")
    query = "&".join(sorted(part for part in parsed.query.split("&") if part and not _TRACKING.match(part.split("=")[0])))
    return host + path + ("?" + query if query else "")


def _is_index_segment(part):
    if part in _INDEX_SEGMENTS:
        return True
    tokens = re.split(r"[-_]", part)
    return (2 <= len(tokens) <= 4 and all(token in _INDEX_TOKENS for token in tokens)
            and any(token in _STRONG_TOKENS for token in tokens))


def _is_listing_path(segments):
    """An index word ends the path (short numeric ids aside), or it is an
    aggregator's /news/<one short entity>. Real article slugs have three or more
    title words or sit under a date path."""
    trimmed = list(segments)
    while trimmed and _SHORT_ID.fullmatch(trimmed[-1]):
        trimmed.pop()
    if trimmed and _is_index_segment(trimmed[-1]):
        return True
    return (len(segments) >= 2 and segments[-2] == "news" and len(segments[-1]) <= 24
            and _ENTITY_SLUG.fullmatch(segments[-1]) is not None)


def url_kind(url: str) -> str:
    """item, homepage, search_page or tag_listing, from the URL alone."""
    parsed = urlparse(str(url or ""))
    segments = [unquote(part).lower() for part in parsed.path.split("/") if part]
    query = {key.lower() for key, _ in parse_qsl(parsed.query)}
    if query & _SEARCH_KEYS and len(segments) < 3:
        return "search_page"
    if any(part in _SEARCH_SEGMENTS for part in segments) and len(segments) <= 2:
        return "search_page"
    if query & _ID_KEYS:
        return "item"
    index = [part for part in segments if not _LOCALE.fullmatch(part) and not part.startswith(("index.", "default."))]
    if not index:
        return "homepage"
    if "tag" in query or "category" in query:
        return "tag_listing"
    for position, part in enumerate(segments):
        if part in _LISTING_SEGMENTS and position >= len(segments) - 2:
            return "tag_listing"
    if len(index) == 1 and index[0].split(".")[0] in _SECTION_SEGMENTS:
        return "tag_listing"
    if _is_listing_path([re.sub(r"\.(?:html?|php)$", "", part) for part in index]):
        return "tag_listing"
    return "item"


def _day_end(year, month, day):
    return datetime(year, month, day, 23, 59, 59, tzinfo=timezone.utc)


def _month_number(name):
    return _MONTHS[name[:3].lower()]


def _text_date(text, now):
    """A date a snippet or title leads with: 2026-10-12, 12 Oct 2026, Oct 12, 2026,
    Oct 12 or 3 days ago. Dates deep in the text are about something else."""
    lead = (text or "").strip()[:70]
    try:
        if match := _ISO.search(lead):
            return _day_end(int(match[1]), int(match[2]), int(match[3]))
        if match := _DMY.search(lead):
            return _day_end(int(match[3]), _month_number(match[2]), int(match[1]))
        if match := _MDY.search(lead):
            return _day_end(int(match[3]), _month_number(match[1]), int(match[2]))
        if match := _RELATIVE.search(lead):
            unit = _UNIT_DAYS[match[2].lower()]
            count = int(match[1]) if match[1] else 1
            return now - timedelta(days=unit * count) if unit else now
        if re.match(r"yesterday\b", lead, re.I):
            return now - timedelta(days=1)
        if match := _MD.search(lead):
            when = _day_end(now.year, _month_number(match[1]), int(match[2]))
            return when if when <= now + timedelta(days=1) else _day_end(now.year - 1, when.month, when.day)
    except ValueError:
        return None
    return None


def _url_date(url):
    path = urlparse(str(url or "")).path
    try:
        if match := _URL_DATE.search(path):
            return _day_end(int(match[1]), int(match[3]), int(match[4]))
        if match := _URL_MONTH.search(path):
            year, month = int(match[1]), int(match[2])
            return _day_end(year, month, calendar.monthrange(year, month)[1])
    except ValueError:
        return None
    return None


def published(item: dict, now=None):
    """(when, where it came from): the source's own date first, then a date the
    snippet or title leads with, then a date path in the URL. (None, None) when
    nothing says. Dates known only to the day count as the day's last moment,
    so a result is out of a window only when it provably is."""
    now = now or datetime.now(timezone.utc)
    # indexed_date is not a publication date (Wikipedia gives the last edit there).
    for key in ("published", "published_date", "date"):
        when = parse_date(item.get(key))
        if when is not None and when <= now + timedelta(days=2):
            return when, "source"
    for field in ("snippet", "title"):
        when = _text_date(item.get(field), now)
        if when is not None:
            return when, field
    when = _url_date(item.get("url"))
    return (when, "url") if when is not None else (None, None)


def window_bounds(recency=None, since=None, until=None, now=None):
    """(since, until) as aware datetimes; recency is day, week, month or year."""
    now = now or datetime.now(timezone.utc)
    if since is None and recency:
        since = now - timedelta(days=RECENCY[recency])
    return since, until


def _asks_specific(terms, recency):
    # A one-word query may well want a site's front page; a longer one or a
    # dated one wants pages about something.
    return len(terms) >= 2 or bool(recency)


def check_result(item: dict, terms, *, since=None, until=None, specific=True, now=None) -> dict:
    """One result's verdict: passed, the reasons it did not, and the check fields."""
    now = now or datetime.now(timezone.utc)
    url, title = str(item.get("url") or ""), str(item.get("title") or "")
    reasons = []
    when, origin = published(item, now)
    in_window = None
    if (since is not None or until is not None) and when is not None:
        in_window = not ((since is not None and when < since) or (until is not None and when > until))
        if not in_window:
            reasons.append("out_of_window")
    blob = " ".join((title, str(item.get("snippet") or ""), unquote(url))).lower()
    on_query = not terms or any(term in blob for term in terms)
    if not on_query:
        reasons.append("off_query")
    kind = url_kind(url)
    if kind != "item" and (specific or kind == "search_page"):
        reasons.append(kind)
    lowered = title.lower()
    if any(word in lowered for word in _CHALLENGE):
        reasons.append("challenge_page")
    return {"url": url, "passed": not reasons, "reasons": reasons, "in_window": in_window,
            "on_query": on_query, "published": when.strftime("%Y-%m-%dT%H:%M:%SZ") if when else None,
            "date_source": origin, "kind": kind}


def counted(dated: int, undated: int, need: int, weigh_undated: bool) -> int:
    """How many passing results count toward "good". An undated result is weaker
    than a dated one, so with weigh_undated they count at most half the quota
    (need // 2): a set of undated filler cannot end the search alone."""
    return dated + (min(undated, need // 2) if weigh_undated else undated)


def check_results(query: str, results, *, limit: int = 10, recency=None, since=None, until=None,
                  now=None, min_good: int = 3, weigh_undated: bool = False) -> dict:
    """Verdict per result and for the set. good: at least min(min_good, limit)
    results pass; weak: some do; empty: none do. Repeats of a page already seen
    fail as duplicates. With weigh_undated, passing results of a windowed query
    whose date is unknown count at most half the quota (see counted)."""
    now = now or datetime.now(timezone.utc)
    since, until = window_bounds(recency, since, until, now)
    terms = key_terms(query)
    specific = _asks_specific(terms, recency)
    verdicts, seen, counts = [], set(), {}
    for item in results:
        verdict = check_result(item, terms, since=since, until=until, specific=specific, now=now)
        key = canonical_url(verdict["url"])
        if key in seen:
            verdict["reasons"].append("duplicate")
            verdict["passed"] = False
        else:
            seen.add(key)
        for reason in verdict["reasons"]:
            counts[reason] = counts.get(reason, 0) + 1
        verdicts.append(verdict)
    passed = sum(1 for verdict in verdicts if verdict["passed"])
    need = max(1, min(min_good, limit))
    windowed = since is not None or until is not None
    undated = sum(1 for verdict in verdicts if verdict["passed"] and verdict["in_window"] is None)
    weighed = counted(passed - undated, undated, need, weigh_undated and windowed)
    return {"verdict": "good" if weighed >= need else "weak" if passed else "empty",
            "passed": passed, "counted": weighed, "need": need, "total": len(verdicts), "terms": terms,
            "reasons": counts, "windowed": windowed,
            "unknown_dates": sum(1 for verdict in verdicts if windowed and verdict["in_window"] is None),
            "results": verdicts}


def _tier(item):
    # Dated and in the window first; unknown dates after them.
    return 0 if (item.get("check") or {}).get("in_window") is True else 1


def annotate(results, report: dict) -> list[dict]:
    """Every result with its check fields under "check" (passed, reasons,
    in_window, on_query, published, date_source, kind). When a window was asked
    for and the source gave no date, a date found in the snippet or URL is also
    set as the result's "published"."""
    rows = []
    for item, verdict in zip(results, report["results"]):
        row = {**item, "check": {key: verdict[key] for key in
                                 ("passed", "reasons", "in_window", "on_query", "published", "date_source", "kind")}}
        if report["windowed"] and verdict["published"] and not item.get("published"):
            row["published"] = verdict["published"]
        rows.append(row)
    return rows


def accepted(results, report: dict) -> list[dict]:
    """The results that passed, each carrying its check fields. When a window was
    asked for, dated in-window results come before those with unknown dates."""
    kept = [item for item in annotate(results, report) if item["check"]["passed"]]
    return sorted(kept, key=_tier) if report["windowed"] else kept


def merge_results(groups, *, windowed=False, limit=None) -> list[dict]:
    """Passing result lists from several sources as one: deduplicated by canonical
    URL, fused by reciprocal rank (k=60) so a page several sources agree on rises,
    dated in-window results ahead of unknown dates when a window was asked for.
    groups is [(source, [result, ...]), ...]."""
    fused, order = {}, []
    for source, items in groups:
        for rank, item in enumerate(items):
            key = canonical_url(item.get("url") or "")
            if key not in fused:
                fused[key] = {**item, "engines": list(item.get("engines") or []),
                              "fusion": {"score": 0.0, "sources": []}}
                order.append(key)
            else:
                for engine in item.get("engines") or []:
                    if engine not in fused[key]["engines"]:
                        fused[key]["engines"].append(engine)
                known = fused[key].get("check") or {}
                if known.get("published") is None and (item.get("check") or {}).get("published"):
                    fused[key]["check"] = item["check"]
            fused[key]["fusion"]["score"] += 1.0 / (60 + rank + 1)
            fused[key]["fusion"]["sources"].append({"source": source, "rank": rank + 1})
    merged = sorted((fused[key] for key in order), key=lambda item: (_tier(item) if windowed else 0,
                                                                    -item["fusion"]["score"]))
    for item in merged:
        item["fusion"]["score"] = round(item["fusion"]["score"], 5)
    return merged[:limit] if limit else merged


def good_count(items, need: int, windowed: bool, weigh_undated: bool = True) -> int:
    """counted() over annotated results: dated passing ones in full, undated ones
    at most half the quota."""
    undated = sum(1 for item in items if windowed and (item.get("check") or {}).get("in_window") is None)
    return counted(len(items) - undated, undated, need, weigh_undated and windowed)


# --- reading a page for its publication date

# Meta tags that name when a page was first published; modified dates are never used.
_META_DATES = frozenset({
    "article:published_time", "og:article:published_time", "datepublished", "date", "dc.date",
    "dc.date.issued", "dcterms.date", "citation_publication_date", "citation_date", "citation_online_date",
    "publish-date", "pubdate", "parsely-pub-date", "sailthru.date", "publication_date", "article.published",
    "og:published_time"})
_PAGE_CHARS = 400_000


def extract_published(html, now=None):
    """(when, where) from a page's own metadata: a published-time meta tag, JSON-LD
    datePublished, an itemprop or a <time datetime>. (None, None) when the page
    gives none or one in the future. Never a modified date."""
    if not isinstance(html, str) or "<" not in html:
        return None, None
    from bs4 import BeautifulSoup
    latest = (now or datetime.now(timezone.utc)) + timedelta(days=2)

    def found(value, origin):
        when = parse_date(value)
        return (when, origin) if when is not None and when <= latest else None

    soup = BeautifulSoup(html[:_PAGE_CHARS], "html.parser")
    for tag in soup.find_all("meta"):
        key = str(tag.get("property") or tag.get("name") or tag.get("itemprop") or "").lower()
        if key in _META_DATES and (hit := found(tag.get("content"), "meta:" + key)):
            return hit
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except ValueError:
            continue
        stack = [data]
        while stack:
            node = stack.pop(0)
            if isinstance(node, dict):
                if hit := found(node.get("datePublished"), "jsonld"):
                    return hit
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
    for tag in soup.find_all(attrs={"itemprop": "datePublished"}):
        if hit := found(tag.get("content") or tag.get("datetime") or tag.get_text(strip=True), "itemprop"):
            return hit
    for tag in soup.find_all("time"):
        if hit := found(tag.get("datetime"), "time"):
            return hit
    return None, None


def date_targets(items, limit: int, budget: int, memo: dict) -> list[int]:
    """Indexes (into the annotated, accepted results) of the undated ones that
    would land in the top `limit` and have not been read yet, at most `budget`."""
    chosen = []
    for position, item in enumerate(items[:limit]):
        if len(chosen) >= budget:
            break
        if (item.get("check") or {}).get("in_window") is None and canonical_url(item["url"]) not in memo:
            chosen.append(position)
    return chosen


async def read_dates(urls, read, *, timeout: float, gap: float, memo: dict) -> dict:
    """Read these pages at once and record each one's publication date (or None)
    in memo, by canonical URL. read(url) is the read path (paced per origin there);
    a failed or slow read is just an undated page. Returns {tried, dated, cost_usd}."""
    ahead = {}

    async def one(url):
        host = urlparse(url).hostname or ""
        # Reads to one origin are spaced by the read path; allow for the wait.
        allowance = timeout + gap * ahead.get(host, 0)
        ahead[host] = ahead.get(host, 0) + 1
        try:
            result = await asyncio.wait_for(read(url), allowance + 1)
        except Exception:
            return None, 0.0
        receipt = result.get("receipt") or {} if isinstance(result, dict) else {}
        if receipt.get("status") != "observed":
            return None, 0.0
        cost = receipt.get("cost_usd")
        return extract_published(result.get("content"))[0], float(cost) if isinstance(cost, (int, float)) else 0.0

    outcomes = await asyncio.gather(*(one(url) for url in urls))
    cost = 0.0
    for url, (when, spent) in zip(urls, outcomes):
        memo[canonical_url(url)] = when
        cost += spent
    return {"tried": len(urls), "dated": sum(1 for when, _ in outcomes if when is not None), "cost_usd": cost}


async def enrich_dates(query, items, report, read, *, limit, budget, memo, timeout, gap, recency=None,
                       since=None, until=None, now=None, min_good=3, weigh_undated=True):
    """Give undated results that would make the top `limit` a verified date by
    reading their pages (see read_dates), then check the set again, so results
    that are provably outside the window drop out. Returns (items, report, stats)
    where stats is {tried, dated, dropped, cost_usd}; items are the raw results
    (with "published" and the verdict's date_source "page" set on the dated)."""
    stats = {"tried": 0, "dated": 0, "dropped": 0, "cost_usd": 0.0}
    rows = annotate(items, report)
    keep = [(item, row) for item, row in zip(items, rows) if row["check"]["passed"]]
    ordered = sorted(keep, key=lambda pair: _tier(pair[1])) if report["windowed"] else keep
    chosen = date_targets([row for _, row in ordered], limit, budget, memo)
    urls = [ordered[position][0]["url"] for position in chosen]
    if urls:
        stats.update(await read_dates(urls, read, timeout=timeout, gap=gap, memo=memo))
    # Every result a page date is now known for, this read or an earlier one.
    pages = {}
    revised = []
    for item in items:
        when = memo.get(canonical_url(item.get("url") or ""))
        if when is not None and published(item, now)[0] is None:
            pages[canonical_url(item["url"])] = True
            item = {**item, "published": when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        revised.append(item)
    if not pages:
        return items, report, stats
    again = check_results(query, revised, limit=limit, recency=recency, since=since, until=until, now=now,
                          min_good=min_good, weigh_undated=weigh_undated)
    for verdict in again["results"]:
        if canonical_url(verdict["url"]) in pages and verdict["published"]:
            verdict["date_source"] = "page"
            stats["dropped"] += 1 if verdict["in_window"] is False else 0
    return revised, again, stats
