"""Checks on search results, the way the read path checks pages: is each result
real, on the query and inside the date window, and does the whole set deserve
to end the search. Pure functions over normalised results (url, title,
snippet, indexed_date); the router in Runtime.search decides what to do next.

A result fails when it is provably outside the date window, shares no key term
with the query, is a homepage, search page, tag listing or challenge page where
the query asked for specific items, or repeats another result's page. An
unknown date is "unknown", not a failure; it only ranks below dated results.
"""
from __future__ import annotations
import calendar
import re
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
    for key in ("indexed_date", "published", "published_date", "date"):
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


def check_results(query: str, results, *, limit: int = 10, recency=None, since=None, until=None,
                  now=None, min_good: int = 3) -> dict:
    """Verdict per result and for the set. good: at least min(min_good, limit)
    results pass; weak: some do; empty: none do. Repeats of a page already seen
    fail as duplicates."""
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
    return {"verdict": "good" if passed >= need else "weak" if passed else "empty",
            "passed": passed, "need": need, "total": len(verdicts), "terms": terms,
            "reasons": counts, "windowed": windowed,
            "unknown_dates": sum(1 for verdict in verdicts if windowed and verdict["in_window"] is None),
            "results": verdicts}


def _tier(item):
    # Dated and in the window first; unknown dates after them.
    return 0 if (item.get("check") or {}).get("in_window") is True else 1


def annotate(results, report: dict) -> list[dict]:
    """Every result with its check fields under "check" (passed, reasons,
    in_window, on_query, published, date_source, kind)."""
    return [{**item, "check": {key: verdict[key] for key in
                               ("passed", "reasons", "in_window", "on_query", "published", "date_source", "kind")}}
            for item, verdict in zip(results, report["results"])]


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
