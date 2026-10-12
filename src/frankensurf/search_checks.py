"""Checks on search results, the way the read path checks pages: is each result
real, on the query and inside the date window, and does the whole set deserve
to end the search. Pure functions over normalised results (url, title,
snippet, indexed_date); the router in Runtime.search decides what to do next.

A result fails when it is provably outside the date window, is not about the
query (whole-word matches only; a named company or product must be in the title,
URL or domain), is a homepage, search page, tag listing or challenge page where
the query asked for specific items, or repeats another result's page. An
unknown date is "unknown", not a failure; it only ranks below dated results,
and enrich_dates reads the pages of undated results to find out.
"""
from __future__ import annotations
import asyncio
import calendar
import json
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, unquote, urlparse

from .completeness import _STOPWORDS, _WORD
from .search import RECENCY, _wiki_key, github_parts, parse_date, reference_topic

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
                               "trending", "popular", "all", "index", "home", "default", "about", "about-us", "company"})
# Words that name an index of items, not an item: the last path segment of a listing.
_INDEX_SEGMENTS = frozenset({
    "new", "news", "newsroom", "press", "press-releases", "press-release-archive", "releases",
    "release-notes", "changelog", "announcements", "updates", "headlines", "blog", "blogs", "articles",
    "stories", "archive", "archives", "category", "categories", "tag", "tags", "topics", "latest",
    "whats-new", "insights", "media", "events", "news-types",
    # A site's own sections and account pages: an index of things, never one dated item.
    "overview", "financials", "filings", "sec-filings", "quarterly-results", "results", "presentations",
    "governance", "careers", "pricing", "platform", "services", "products", "solutions", "signin", "sign-in",
    "login", "account", "support", "help", "c"})
# Query keys that page or size a listing (?l=100, ?page=2); an article path keeps its page numbers.
_LISTING_KEYS = frozenset({"l", "page", "pagenum", "page_num", "per_page", "perpage", "offset", "limit"})
_FORGE_HOSTS = frozenset({"github.com", "gitlab.com", "bitbucket.org", "codeberg.org"})
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
_LOCALE = re.compile(r"[a-z]{2}(?:[-_](?:[a-z]{2}|[a-z]{4}|\d{3}))?")
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
# /2026/sep/0915-title and /2026/september/ (a month name, then maybe MMDD).
_URL_MONTH_NAME = re.compile(r"/((?:19|20)\d\d)/(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*(?:/(?:(\d\d)(\d\d)(?!\d))?|$)", re.I)
# A compact stamp opening a path segment: 20261006 or 20261006057924.
_URL_STAMP = re.compile(r"/((?:19|20)\d\d)(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?:\d{6})?(?!\d)")
# A new-style arXiv identifier, YYMM.NNNNN (/abs/2410.04941, /pdf/2410.04941v2): the month it was submitted.
_URL_ARXIV = re.compile(r"/(\d\d)(0[1-9]|1[0-2])\.(\d{4,5})(?:v\d+)?(?:\.pdf)?(?:/|$)")


def _stem(word):
    if word.endswith("es") and len(word) >= 5:
        return word[:-2]
    if word.endswith("s") and len(word) >= 4:
        return word[:-1]
    return word


# Where a thing lives, not what it is: "pydantic github repo" is about pydantic. These
# count only when the query has nothing else; written as a name ("GitHub latest
# announcement"), the word is the topic.
_PLATFORM_WORDS = frozenset({"github", "gitlab", "repo", "repos", "repository", "repositories", "library",
                             "libraries", "package", "packages", "crate", "crates", "module", "modules", "source",
                             "code", "sdk", "docs", "documentation", "official", "website", "site", "homepage",
                             "pypi", "npm"})


# Action verbs, auxiliaries and quality words: how the question is put, not what it is about
# ("how to use regex lookahead" is about regex lookahead; every dictionary has "use"). Like the
# platform words they count only when nothing else is left. Checked on the word as written and
# stemmed (writes, guides).
_WEAK_WORDS = frozenset({
    "use", "uses", "using", "used", "make", "makes", "making", "made", "create", "creates", "creating",
    "created", "get", "gets", "getting", "set", "sets", "setting", "do", "does", "doing", "write", "writes",
    "writing", "add", "adds", "adding", "run", "runs", "running", "find", "finds", "need", "needs", "want",
    "wants", "work", "works", "working", "best", "good", "simple", "easy", "example", "examples", "tutorial",
    "tutorials", "guide", "guides", "way", "ways", "paper", "papers", "preprint", "preprints", "arxiv"})


def key_terms(query: str) -> list[str]:
    """What the query is about: its words without operators, stopwords, years and
    freshness words (news, latest, this week), lightly stemmed. Two-character names
    written in capitals or with a digit (BP, GE, 3M) stay; other short words go.
    Platform words and action or quality words in lower case (github, repo, library; use,
    make, best, example) count only when nothing else is left."""
    return _terms(query, True)


def raw_terms(query: str) -> list[str]:
    """key_terms without the stemming: the words as written, lowercased (pandas, not panda)."""
    return _terms(query, False)


def _terms(query, stem):
    # "home page", "official site": where the query wants to go, not what it is about (kept when
    # nothing else is left).
    text = _OPERATOR.sub(" ", str(query or ""))
    return _terms_of(_NAVIGATIONAL.sub(" ", text), stem) or _terms_of(text, stem)


def _terms_of(text, stem):
    terms, platform = [], []
    for raw in _WORD.findall(text):
        word = raw.lower()
        if word in _STOPWORDS or word in _QUERY_FILLER or _YEAR.fullmatch(word):
            continue
        if len(word) < 3 and word.isascii() and not (len(word) == 2 and (raw.isupper() or any(c.isdigit() for c in raw))):
            continue
        word = _stem(word) if stem and len(word) > 2 else word
        # Written as a name (GitHub, Docs), a platform word is the topic after all.
        weak = word in _PLATFORM_WORDS or word in _WEAK_WORDS or raw.lower() in _WEAK_WORDS
        bucket = platform if weak and raw.islower() else terms
        if word not in bucket:
            bucket.append(word)
    return (terms or platform)[:8]


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
    # A locale root (/en/, /en-us/, /zh-hans/) is a homepage; only a leading segment is a locale.
    index = [part for position, part in enumerate(segments)
             if not (position == 0 and _LOCALE.fullmatch(part)) and not part.startswith(("index.", "default."))]
    if not index:
        return "homepage"
    if "tag" in query or "category" in query:
        return "tag_listing"
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if host in _FORGE_HOSTS and len(segments) == 2:
        return "item"  # owner/name: a repository, whatever the name is (platform, help, c)
    if query & _LISTING_KEYS and not article_like(url):
        return "tag_listing"
    for position, part in enumerate(segments):
        if (part in _LISTING_SEGMENTS or part == "c") and position >= len(segments) - 2:
            return "tag_listing"
    if len(index) == 1 and index[0].split(".")[0] in _SECTION_SEGMENTS:
        return "tag_listing"
    if _is_listing_path([re.sub(r"\.(?:html?|php|aspx?)$", "", part) for part in index]):
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
        if match := _URL_MONTH_NAME.search(path):
            year, month = int(match[1]), _month_number(match[2])
            if match[3] and int(match[3]) == month:
                return _day_end(year, month, int(match[4]))
            return _day_end(year, month, calendar.monthrange(year, month)[1])
        if match := _URL_STAMP.search(path):
            return _day_end(int(match[1]), int(match[2]), int(match[3]))
        if match := _URL_ARXIV.search(path):
            # arXiv started in 2007; the identifier's month is the date, taken as the month's last day.
            year, month = 2000 + int(match[1]), int(match[2])
            # YYMM.NNNNN since 2015; the first eight years numbered papers with four digits.
            if (len(match[3]) == 5 and year >= 2015) or (len(match[3]) == 4 and 2007 <= year < 2015):
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
            return min(when, now), field
    when = _url_date(item.get("url"))
    # A month-only date (/2026/10/) counts as the month's last day for the window,
    # but is never reported as a day still to come.
    return (min(when, now), "url") if when is not None else (None, None)


# Path segments that hold dated items: what follows one of them, a slug or an id, is an item.
_ARTICLE_SECTIONS = frozenset({
    "news", "newsroom", "press", "press-releases", "press-release", "news-releases", "blog", "blogs", "stories",
    "story", "announcements", "announcement", "releases", "release", "articles", "article", "updates", "update",
    "insights", "media"})


def _slug_words(segment):
    return [word for word in segment.split("-") if word]


def _is_item_id(segment):
    return bool(re.fullmatch(r"\d{5,}", segment)
                or re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", segment)
                or (re.fullmatch(r"[a-z0-9]{8,}", segment) and re.search(r"\d", segment)
                    and re.search(r"[a-z]", segment)))


def article_like(url) -> bool:
    """Whether the URL's path looks like one article, release or post, from the path alone: a
    date in it (2026/10/12, 20261012, an arXiv id), a news, press, blog, stories, announcements,
    releases, articles, updates, insights or media segment followed by a slug of three or more
    hyphenated words or an id, or a long slug of four or more hyphenated words anywhere. Product,
    pricing, careers, platform, services, sign-in, account, overview, financials, filings, results,
    events, governance, category and listing pages, profiles and homepages are not."""
    parsed = urlparse(str(url or ""))
    if _url_date(url) is not None:
        return True
    segments = [re.sub(r"\.[a-z0-9]{2,5}$", "", unquote(part).lower()) for part in parsed.path.split("/") if part]
    keys = {key.lower() for key, _ in parse_qsl(parsed.query)}
    for position, part in enumerate(segments):
        if part in _ARTICLE_SECTIONS:
            if keys & _ID_KEYS:
                return True
            if any(len(_slug_words(later)) >= 3 or _is_item_id(later) for later in segments[position + 1:]):
                return True
    return any(len(_slug_words(part)) >= 4 for part in segments)


_MARKETING_SEGMENTS = frozenset({"brc", "resources", "resource", "learn", "business-resources", "help", "support",
                                 "guides", "guide", "academy"})


def marketing_path(url) -> bool:
    """Whether the path sits under a marketing or help segment (brc, resources, learn,
    business-resources, help, support, guides, academy, blog/tips): how-to and explainer content,
    not news."""
    segments = [unquote(part).lower() for part in urlparse(str(url or "")).path.split("/") if part]
    return (any(part in _MARKETING_SEGMENTS for part in segments)
            or any(first == "blog" and second == "tips" for first, second in zip(segments, segments[1:])))


def window_bounds(recency=None, since=None, until=None, now=None):
    """(since, until) as aware datetimes; recency is day, week, month or year."""
    now = now or datetime.now(timezone.utc)
    if since is None and recency:
        since = now - timedelta(days=RECENCY[recency])
    return since, until


# Query words that ask for dated or newsy items, never for a site's front page.
_NEWSY = frozenset({
    "news", "latest", "recent", "recently", "today", "tonight", "yesterday", "announcement", "announcements",
    "announce", "announced", "announces", "release", "releases", "released", "paper", "papers", "post", "posts",
    "article", "articles", "launch", "launches", "launched", "headline", "headlines", "update", "updates",
    "breaking", "published", "preprint", "preprints"})
_NAVIGATIONAL = re.compile(r"\b(?:home\s?page|(?:official|main|web)\s?(?:site|website|page)|website)\b", re.I)
# Capitalised first words that start a phrase without naming anything.
_GENERIC_CAPS = frozenset({
    "best", "top", "new", "free", "cheap", "good", "easy", "simple", "fast", "how", "install", "compare",
    "comparison", "fix", "build", "create", "learn", "use", "using", "get", "find", "make", "setup", "set",
    "configure", "run", "write", "read", "buy", "download", "guide", "tutorial", "review", "reviews", "list",
    "tips", "ideas", "why", "is", "does", "do", "can", "should", "the", "a", "an", "show", "give", "tell"})
_CONNECTORS = frozenset({"of", "the", "de", "la"})
_SPELLING_CONNECTORS = _CONNECTORS | {"and"}


# Words that make a company name formal without naming it: "Arm Holdings" is Arm.
_CORPORATE_SUFFIXES = frozenset({"inc", "corp", "corporation", "ltd", "llc", "holdings", "group", "technologies",
                                 "labs", "ai", "co", "plc", "se", "ag"})
# Second-level labels under a country TLD (example.co.uk): the name is one label further left.
_SECOND_LEVEL = frozenset({"co", "com", "org", "net", "gov", "edu", "ac", "or", "ne"})


def entities(query: str) -> list[list[str]]:
    """The named things a query is about: runs of capitalised words (Hugging Face,
    Arm Holdings, Nintendo), each as its words lowercased, minus filler words and
    generic capitalised openers (Best, Install). A query written all in lowercase
    or in Title Case throughout names none."""
    return [phrase for phrase, _ in _entity_runs(query)]


def _entity_runs(query: str) -> list[tuple[list[str], list[str]]]:
    """Each entity as (its significant words lowercased, every word of its run as
    written, connectors and short words included: the raw material for aliases)."""
    words = _WORD.findall(_OPERATOR.sub(" ", str(query or "")))
    meaningful = [w for w in words if w.lower() not in _STOPWORDS and w.lower() not in _QUERY_FILLER
                  and w.lower() not in _GENERIC_CAPS and not _YEAR.fullmatch(w) and (len(w) >= 3 or not w.isascii())]
    if len(meaningful) >= 3 and all(w[:1].isupper() or w[:1].isdigit() for w in meaningful):
        return []
    runs, run, full = [], [], []
    for position, word in enumerate(words):
        low = word.lower()
        capital = word[:1].isupper() or (bool(run) and word[:1].isdigit())
        if capital and low in _GENERIC_CAPS and not run:
            capital = False
        if capital and low not in _QUERY_FILLER and low not in _STOPWORDS:
            run.append(word)
            full.append(word)
        elif run and low in _CONNECTORS and position + 1 < len(words) and words[position + 1][:1].isupper():
            full.append(word)
            continue
        else:
            runs.append((run, full))
            run, full = [], []
    runs.append((run, full))
    found, seen = [], []
    for run, full in runs:
        phrase = [w.lower() for w in run if (len(w) >= 3 or not w.isascii()) and not _YEAR.fullmatch(w)]
        if phrase and phrase not in seen:
            seen.append(phrase)
            found.append((phrase, full))
    return found


def _asks_specific(terms, windowed, query="", named=()):
    """Whether the query wants pages about something, so a homepage or section
    index is no answer. Dated or newsy queries always do, whatever the term count.
    Only navigational queries accept a front page: "X homepage", "X official site",
    "X website", or a bare entity name with no window."""
    if windowed:
        return True
    if _NAVIGATIONAL.search(str(query or "")):
        return False
    if {word.lower() for word in _WORD.findall(_OPERATOR.sub(" ", str(query or "")))} & _NEWSY:
        return True
    if _FACT_LEAD.search(str(query or "")):
        # A question wants the page that answers it, which may well be a topic page or an index
        # entry (an encyclopedia's /topic/<name>), not a list of items.
        return False
    if named:
        bare = {_stem(word) for phrase in named for word in phrase}
        if terms and set(terms) <= bare:
            return False
    return len(terms) >= 2


def _term_pattern(term):
    if not term.isascii():
        return re.compile(re.escape(term))
    if len(term) >= 11 and term.endswith("ation") and term[:-5].isalpha():
        # serialization is also serialize, serializing, serializer; configuration, configure, configured.
        return re.compile(r"(?<![^\W_])" + re.escape(term[:-5]) +
                          r"(?:ations?|ing|ed|es?|ers?|ors?|ions?|s)(?![^\W_])")
    if len(term) >= 7 and term.endswith("ing") and len(term) - 3 >= 4 and term[:-3].isalpha():
        # modeling is also model, models, modelling; learning, learn, learned, learner.
        return re.compile(r"(?<![^\W_])" + re.escape(term[:-3]) + r"(?:ings?|ed|es?|s|ers?|ling)?(?![^\W_])")
    suffix = "(?:es|s)?" if len(term) <= 4 else "(?:e|es|s|ed|d|ing|ers?)?"
    return re.compile(r"(?<![^\W_])" + re.escape(term) + suffix + r"(?![^\W_])")


_PATTERNS: dict = {}


def _has(term, text):
    pattern = _PATTERNS.get(term)
    if pattern is None:
        pattern = _PATTERNS[term] = _term_pattern(term)
    return pattern.search(text) is not None


def _domain_names(host, words):
    """A host label that is the entity run together (huggingface.co for Hugging
    Face) or hyphenated into it."""
    joined = "".join(words)
    for label in host.removeprefix("www.").split("."):
        if label.replace("-", "") == joined or set(words) <= set(label.split("-")):
            return True
    return False


def _core_words(tokens):
    """An entity's words without connectors and corporate suffixes (Arm Holdings
    gives Arm); the suffixes stay when nothing else would."""
    significant = [t for t in tokens if t.lower() not in _CONNECTORS]
    return [t for t in significant if t.lower() not in _CORPORATE_SUFFIXES] or significant


def _acronyms(tokens):
    """Exact spellings an entity's initials can take: AWS for Amazon Web Services,
    IBM, GE; with a connector between its words also BoA, BofA and BOA for Bank of
    America. Two letters at least, and two words at least."""
    words = [t for t in tokens if t.lower() not in _CORPORATE_SUFFIXES]
    core = [t for t in words if t.lower() not in _CONNECTORS]
    if len(core) < 2:
        return set()
    found = {"".join(t[0] for t in core).upper()}
    if len(core) < len(words):
        found.add("".join(t[0].lower() if t.lower() in _CONNECTORS else t[0].upper() for t in words))
        found.add("".join(t.lower() if t.lower() in _CONNECTORS else t[0].upper() for t in words))
        found.add("".join(t[0] for t in words).upper())
    return {a for a in found if len(a) >= 2 and a.isascii()}


def _expands(acronym, title):
    """Whether the title has capitalised words whose initials spell the acronym
    (Amazon Web Services for AWS); connectors may sit between them."""
    tokens = _WORD.findall(title)
    size = len(acronym)
    for start in range(len(tokens)):
        initials, position = "", start
        while position < len(tokens) and len(initials) < size:
            token = tokens[position]
            if token[:1].isupper() or token[:1].isdigit():
                initials += token[0].upper()
            elif not (initials and token.lower() in _SPELLING_CONNECTORS):
                break
            position += 1
        if initials == acronym:
            return True
    return False


def _registrable_label(host):
    """The name label of a host: huggingface in huggingface.co, bbc in news.bbc.co.uk."""
    labels = host.removeprefix("www.").split(".")
    if len(labels) < 2:
        return ""
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL:
        return labels[-3]
    return labels[-2]


def _alias_match(tokens, title, url, host):
    """An acronym of the entity in the title (uppercase, whole token) or URL (any
    case; two-letter ones only as a host label), or the registrable domain being
    the entity run together, with or without its corporate suffixes."""
    low_url = unquote(url).lower()
    labels = host.removeprefix("www.").split(".")
    for acronym in _acronyms(tokens):
        if re.search(r"(?<![^\W_])" + re.escape(acronym) + r"(?![^\W_])", title):
            return True
        if len(acronym) == 2:
            if acronym.lower() in labels:
                return True
        elif _has(acronym.lower(), low_url):
            return True
    label = _registrable_label(host).replace("-", "")
    if label:
        joined = {"".join(t.lower() for t in tokens if t.lower() not in _CONNECTORS),
                  "".join(t.lower() for t in _core_words(tokens)), "".join(t.lower() for t in tokens)}
        return label in joined
    return False


_PHRASE_GAP = r"[\W_]+(?:(?:of|and|with|for|in|on|the|a|an|to|by)[\W_]+)?"
_PHRASES: dict = {}


def _rarer(terms):
    """The rarer of the query's terms, by a simple proxy: the longer one, a non-dictionary-looking
    one (digits, dots, dashes) before a plain one of the same length."""
    return max(terms, key=lambda term: (len(term), not term.isalpha()))


def _phrase_in(pair, text):
    """Two query terms next to each other in the text, with at most a connector between."""
    pattern = _PHRASES.get(pair)
    if pattern is None:
        pattern = _PHRASES[pair] = re.compile(_term_pattern(pair[0]).pattern + _PHRASE_GAP + _term_pattern(pair[1]).pattern)
    return pattern.search(text) is not None


def _phrase_hit(terms, text):
    """Whether two terms that are next to each other in the query are next to each other in the text."""
    return any(_phrase_in(pair, text) for pair in zip(terms, terms[1:]))


def enough_terms(terms, head, snippet="", entity=False):
    """Whether the text (title + URL = head, and snippet) covers enough of the query's terms.
    One term: it. Two: both, or the rarer one together with the entity. Three or more: at least
    half of them (rounded up), or the entity, or two of them as a phrase. The entity is whether the
    result names the query's entity, which a caller with a named entity has already settled."""
    if not terms:
        return True
    text = head + " " + snippet
    hits = [term for term in terms if _has(term, text)]
    if len(terms) == 1:
        return bool(hits)
    if len(terms) == 2:
        return len(hits) == 2 or (entity and _rarer(terms) in hits)
    return entity or len(hits) >= math.ceil(len(terms) / 2) or _phrase_hit(terms, text)


def _on_query(terms, named, item, cased=(), entity=False):
    """Whether a result is about the query. Terms match whole words (light
    stemming), never inside other words. When the query names something, the
    result must too: all of an entity's words in the title or URL, or its domain
    (a snippet-only mention only counts for a multi-word name, or alongside
    another query term). The entity also goes by aliases: its words minus
    corporate suffixes (Arm for Arm Holdings), its acronym (AWS for Amazon Web
    Services), the registrable domain, and, for an all-caps query entity
    (AWS), the capitalised words that spell it in the title. Without a named
    entity the result must cover the key terms (see enough_terms): the one term; both of two (or the
    rarer one, when entity says the page is of the kind asked for: a library page for a "library
    for X" query); at least half of three or more, or two of them as a phrase."""
    if not terms:
        return True
    url = str(item.get("url") or "")
    title = str(item.get("title") or "")
    head = (title + " " + unquote(url)).lower()
    snippet = str(item.get("snippet") or "").lower()
    if not named:
        return enough_terms(terms, head, snippet, entity)
    host = (urlparse(url).hostname or "").lower()
    named_terms = {_stem(word) for phrase in named for word in phrase}
    others = [term for term in terms if term not in named_terms]
    for index, phrase in enumerate(named):
        tokens = cased[index] if index < len(cased) else []
        stems = [_stem(word) for word in phrase]
        if all(_has(stem, head) for stem in stems) or _domain_names(host, phrase):
            return True
        if tokens:
            if _alias_match(tokens, title, url, host):
                return True
            if len(tokens) == 1 and 2 <= len(tokens[0]) <= 6 and tokens[0].isupper() and tokens[0].isalpha() \
                    and _expands(tokens[0], title):
                return True
        candidates = [stems]
        core = [_stem(w.lower()) for w in _core_words(tokens) if w.lower() in phrase] if tokens else []
        if core and core != stems:
            candidates.append(core)
            if all(_has(stem, head) for stem in core):
                return True
        for stems in candidates:
            if all(_has(stem, snippet) or _has(stem, head) for stem in stems) and (
                    len(stems) > 1 or any(_has(term, head) or _has(term, snippet) for term in others)):
                return True
    return False


def _names_the_entity(url, named):
    """A profile page: the whole path is one segment that is just the entity's
    name (example.com/nintendo), a front page of sorts, not an item."""
    segments = [unquote(part).lower() for part in urlparse(str(url or "")).path.split("/") if part]
    if segments and _LOCALE.fullmatch(segments[0]):
        segments = segments[1:]
    if len(segments) != 1 or not named:
        return False
    slug = re.sub(r"[-_.\s]", "", segments[0])
    return slug in ["".join(phrase) for phrase in named]


# --- what kind of query it is, and which result answers it best

# Question openers and phrasings that ask for a fact, not for pages about a topic.
_FACT_LEAD = re.compile(r"^\W*(?:who|whom|whose|what|when|where|which|why|how (?:many|much|old|tall|long|far|big|"
                        r"large|high|often)|is|was|are|were|did|does|do)\b", re.I)
_FACT_PHRASE = re.compile(r"\b(?:smallest|largest|biggest|tallest|highest|longest|deepest|oldest|youngest|fastest|"
                          r"capital of|population of|president of|prime minister of|founder of|inventor of|"
                          r"author of|meaning of|definition of|height of|born in|died in)\b", re.I)
_CODE_WORDS = frozenset({"github", "gitlab", "bitbucket", "repo", "repos", "repository", "repositories",
                         "library", "libraries", "package", "packages", "crate", "crates", "pypi", "npm", "sdk"})
_ANNOUNCE_WORDS = frozenset({"news", "latest", "announcement", "announcements", "announce", "announced",
                             "announces", "launch", "launches", "launched", "headline", "headlines", "breaking",
                             "press", "today", "yesterday", "recent", "recently"})
_PAPER_WORDS = frozenset({"paper", "papers", "preprint", "preprints", "arxiv", "research", "study", "studies",
                          "survey", "surveys"})
_ASKS = {
    "policy": frozenset({"policy", "policies", "terms", "privacy", "legal", "tos", "gdpr", "cookie", "cookies",
                         "license", "licence"}),
    "stock": frozenset({"stock", "stocks", "share", "shares", "price", "prices", "quote", "quotes", "ticker"}),
    "docs": frozenset({"docs", "documentation", "release", "releases", "changelog", "notes", "api", "reference",
                       "guide", "tutorial", "manual", "readme", "version", "versions"}),
    "security": frozenset({"security", "vulnerability", "vulnerabilities", "cve", "advisory", "advisories",
                           "exploit", "patch"}),
}
_FORGES = frozenset({"github.com", "gitlab.com", "bitbucket.org", "codeberg.org"})


def _query_words(query):
    return {word.lower() for word in _WORD.findall(_OPERATOR.sub(" ", str(query or "")))}


def query_kinds(query: str, *, vertical=None, recency=None) -> frozenset:
    """Which kinds of query this is, for the work that suits each: "news" (the news
    vertical), "announcement" (a recency window was asked for), "fact" (a question or
    superlative, or the reference vertical) and "code" (the code vertical, or words like
    github, repo or library). From the words and options alone, never the names."""
    words = _query_words(query)
    text = str(query or "")
    kinds = set()
    if vertical == "news":
        kinds.add("news")
    if recency:
        kinds.add("announcement")
    if vertical == "reference" or _FACT_LEAD.search(text) or _FACT_PHRASE.search(text):
        kinds.add("fact")
    if vertical == "code" or words & _CODE_WORDS:
        kinds.add("code")
    return frozenset(kinds)


# --- which vertical a plain query belongs to

_LANGS = (r"(?:python|rust|golang|go|javascript|typescript|node\.js|nodejs|java|ruby|php|c\+\+|c#|swift|"
          r"kotlin)")
_LANG_LIB = re.compile(r"(?<![\w.+#])" + _LANGS + r"\s+(?:libraries|library|packages?|crates?|modules?|gems?|"
                       r"frameworks?|sdks?)\b|(?<![\w.+#])" + _LANGS + r"(?:\s+[\w.+#-]+){1,2}\s+frameworks?\b", re.I)
# Languages whose name is only a language, and those whose name is also a word (python, rust,
# go, java, ruby, swift): the second kind only counts beside an error word or after "in".
_LANG_SURE = re.compile(r"(?<![\w.+#])(?:javascript|typescript|golang|node\.js|nodejs|php|c\+\+|c#|kotlin)"
                        r"(?![\w+#]|\.\w)", re.I)
_LANG_AMBIGUOUS = re.compile(r"(?<![\w.+#])(?:python|rust|go|java|ruby|swift)(?![\w+#]|\.\w)", re.I)
_IN_LANG = re.compile(r"\bin\s+(?:python|rust|go|java|ruby|swift)\b", re.I)
_DISCOVERY = re.compile(r"\b(?:library|libraries|framework|frameworks|sdk|sdks)\s+for\b", re.I)
_NOT_SOFTWARE = frozenset({"public", "school", "university", "city", "county", "national", "local", "digital",
                           "state", "college", "town"})
_PLATFORM = re.compile(r"(?<![\w.])(?:github|gitlab|bitbucket|codeberg|npm|pypi|crates?|cargo|repo|repos|"
                       r"repositor(?:y|ies)|source\s+code)(?![\w.])", re.I)
_PAPERS = re.compile(r"\b(?:papers?\s+(?:on|about)|research\s+on|arxiv|preprints?|survey\s+of)\b", re.I)
_ANNOUNCE = re.compile(r"\b(?:announce[sd]?|announcements?|press\s+releases?|launch(?:es|ed)?)\b", re.I)
_NEWS_WORD = re.compile(r"\b(?:news|headlines?|latest\s+on)\b", re.I)
_WINDOW_PHRASE = re.compile(r"\b(?:this\s+week|today|tonight)\b", re.I)
_QA_TRIGGER = re.compile(r"\b(?:how\s+to|how\s+do\s+(?:i|you|we)|how\s+can\s+i|errors?|exceptions?|tracebacks?|"
                         r"fix(?:ing|es)?|stack\s*trace|segfault|not\s+working|fail(?:s|ed)?\s+(?:to|with)|"
                         r"cannot\s+(?:read|find|resolve)|is\s+not\s+(?:defined|a\s+function))\b", re.I)
_QA_STRONG = re.compile(r"\b(?:errors?|exceptions?|tracebacks?|stack\s*trace|segfault|cannot\s+(?:read|find|resolve)|"
                        r"is\s+not\s+(?:defined|a\s+function))\b", re.I)
_EXCEPTION_CLASS = re.compile(r"\b[A-Z]\w*(?:Error|Exception)\b")
# Code-shaped tokens: snake_case, camelCase, call(), ::, --flag, a file extension, a dotted name,
# an error code, a backtick, and a few words that only mean one thing.
_CODEISH = re.compile(r"[a-z0-9]_[a-z0-9]|[a-z][A-Z]|\(\)|::|(?<!\s)--?[a-z]{2}|\b\w+\.(?:py|js|ts|rs|go|rb|php|"
                      r"java|kt|swift|cpp|h|cs|json|ya?ml|toml|sh|sql|css|html)\b|\b[a-z]+\.[a-z]+\.[a-z]+\b|"
                      r"\b(?:E\d{4}|ERR_\w+|0x[0-9a-f]+)\b|`|\bnull\b|\bundefined\b|\bnpm\b|\bgit\b|\bdocker\b|"
                      r"\bsql\b|\bcss\b|\bhtml\b|\bapi\b|\bcompile[dr]?\b|\bsyntax\b|\bruntime\b|\bpip\b")
_REF_LEAD = re.compile(r"^\W*(?:who(?:'s|\s+is|\s+was|\s+are|\s+were|m)?|whose|what\s+(?:is|was|are|were)|"
                       r"what\s+year|what\s+date|when\s+(?:was|did|is|were)|how\s+(?:tall|long|many|much|old|far|"
                       r"big|high|deep|wide|heavy))\b", re.I)
_REF_PHRASE = re.compile(r"\b(?:capital\s+of|population\s+of|currency\s+(?:of|in)|what\s+currency|"
                         r"(?:founded|invented|wrote|painted|composed|discovered))\b", re.I)
_OPINION = frozenset({"best", "top", "vs", "versus", "review", "reviews", "cheap", "cheapest", "buy", "deals",
                      "deal", "recommend", "recommended", "alternatives", "alternative", "tutorial", "course",
                      "courses", "coupon", "discount"})
_FIRST_PERSON = re.compile(r"\b(?:i|i'm|i've|my|we|our)\b", re.I)


def _discovery(text):
    """"library for ...", "framework for ...", "sdk for ...", unless it is a public library."""
    found = _DISCOVERY.search(text)
    if not found:
        return False
    before = text[:found.start()].split()
    return not (before and before[-1].lower() in _NOT_SOFTWARE)


def _is_codeish(text, strong):
    """Code-shaped tokens, a language that is only a language, or an ambiguous name
    (python, rust) beside an error word or after "in"."""
    return bool(_CODEISH.search(text) or _LANG_SURE.search(text) or _EXCEPTION_CLASS.search(text)
                or (_LANG_AMBIGUOUS.search(text) and (strong or _IN_LANG.search(text))))


def infer_vertical(query: str, recency=None) -> tuple[str, str]:
    """Which vertical a plain query belongs to, as (vertical, reason). Generic and
    deterministic: sentence shape and common words only, no site or company lists. A
    query that matches nothing stays "web". The order is the tie-breaking:

    1. qa: an error or a how-to together with code-shaped tokens or a language name.
       Stack Overflow holds the answered version of that question, and with no specialist
       race for qa the ladder climbs on to the web engines when its set is weak, so a
       miss costs one rung. A how-to without code ("how to boil an egg") stays web:
       Stack Exchange would only be a detour. A discovery phrase ("library for ...",
       "python library") is code even beside a how-to.
    2. papers, 3. code (platform words, language libraries, discovery phrases).
    4. announcement phrasing stays web: it is already handled as windowed web plus
       paid parallel.
    5. news, 6. reference (fact questions, unless they ask for opinions or buying advice
       and a bare past-tense verb about the speaker ("I wrote") is not a fact question).

    recency only matters for "latest <Entity>" with a day or week window."""
    text = str(query or "").strip()
    if not text:
        return "web", "empty query"
    discovery, lang_lib = _discovery(text), _LANG_LIB.search(text)
    if ((_QA_TRIGGER.search(text) or _EXCEPTION_CLASS.search(text))
            and _is_codeish(text, bool(_QA_STRONG.search(text) or _EXCEPTION_CLASS.search(text)))
            and not (discovery or lang_lib)):
        return "qa", "error or how-to question with code tokens"
    if _PAPERS.search(text):
        return "papers", "asks for papers or research"
    if platform := _PLATFORM.search(text):
        return "code", "platform word: " + " ".join(platform.group(0).lower().split())
    if lang_lib:
        return "code", "language library: " + " ".join(lang_lib.group(0).lower().split())
    if discovery:
        return "code", "library, framework or sdk for a need"
    if _ANNOUNCE.search(text):
        return "web", "announcement phrasing stays windowed web"
    if _NEWS_WORD.search(text):
        return "news", "news words"
    named = entities(text)
    if named and _WINDOW_PHRASE.search(text):
        return "news", "entity with a today or this-week window"
    if named and re.search(r"\blatest\b", text, re.I) and recency in ("day", "week"):
        return "news", "latest entity with a day or week window"
    words = {word.lower() for word in _WORD.findall(text)}
    lead = _REF_LEAD.search(text)
    if ((lead or _REF_PHRASE.search(text)) and not words & _OPINION
            and not (_FIRST_PERSON.search(text) and not lead)):
        return "reference", "fact question"
    return "web", "no vertical signal"


@dataclass(frozen=True)
class QueryContext:
    """What ranking needs to know about a query, worked out once."""
    terms: tuple
    named: tuple
    cased: tuple
    names: frozenset      # what a canonical result is called: the entity or project, run together
    announce: bool        # asks for news or announcements (a window, or words like latest, press)
    papers: bool
    asks: frozenset       # the kinds of page the query asks for outright: policy, stock, docs, security
    words: frozenset = frozenset()   # the query's words, lowercased
    code: bool = False            # a code query: the code vertical, or words like github, repo, library
    repo: bool = False            # asks for the repository itself (github, repo, repository), not for docs
    lookup: bool = False          # a code query for a project by name ("httpx python github")
    need: bool = False            # a code query for something that does a job ("library for X", "X framework")
    languages: tuple = ()         # programming languages the query names, lowercased
    lookup_terms: tuple = ()      # a name lookup's project words, as written


_REPO_WORDS = frozenset({"github", "gitlab", "bitbucket", "codeberg", "repo", "repos", "repository",
                         "repositories"})


def _joined_names(named, cased, raw):
    names = set()
    for phrase in named:
        names.add("".join(phrase))
    for tokens in cased:
        if tokens:
            names.add("".join(t.lower() for t in _core_words(tokens)))
            names.add("".join(t.lower() for t in tokens if t.lower() not in _CONNECTORS))
    if len(raw) == 1:
        names.add(raw[0])
    elif 2 <= len(raw) <= 3:
        names.add("".join(raw))
    return frozenset(name.replace("-", "") for name in names if len(name) >= 2)


def query_context(query: str, *, recency=None, vertical=None) -> QueryContext:
    runs = _entity_runs(query)
    named = tuple(phrase for phrase, _ in runs)
    cased = tuple(tokens for _, tokens in runs)
    words = _query_words(query)
    terms = tuple(key_terms(query))
    names = _joined_names(named, cased, raw_terms(query))
    asks = frozenset(name for name, group in _ASKS.items() if words & group)
    code =vertical == "code" or bool(words & _CODE_WORDS)
    lookup = need = False
    languages, lookup_terms = (), ()
    if code:
        found = github_parts(query)
        lookup, need = found["name_lookup"], not found["name_lookup"]
        languages = tuple(found["languages"])
        if found["name_lookup"]:
            lookup_terms = tuple(found["terms"])
            # A project by name ("httpx python github"): the project is the topic, the language
            # and the platform are not, and the repository carrying its name is canonical.
            terms = tuple(_stem(word) if len(word) > 2 else word for word in found["terms"])
            names = names | {word.replace("-", "") for word in found["terms"]} | {"".join(found["terms"]).replace("-", "")}
    return QueryContext(
        terms=terms, named=named, cased=cased,
        names=frozenset(name for name in names if len(name) >= 2),
        announce=bool(recency) or bool(words & _ANNOUNCE_WORDS) or vertical == "news",
        papers=vertical == "papers" or bool(words & _PAPER_WORDS),
        asks=asks, words=frozenset(words), code=code, repo=bool(words & _REPO_WORDS) and code and not asks,
        lookup=lookup, need=need, languages=languages, lookup_terms=lookup_terms)


# Generic patterns for pages that are not what a search is after.
_STOCK_URL = re.compile(r"/(?:quotes?|stocks?|stock-?quotes?|stock-?prices?|share-?prices?|live-?blog|stock-market)"
                        r"(?:/|-|$)|(?:share|stock)-?price|liveblog")
_STOCK_TITLE = re.compile(r"\b(?:share|stock) prices?\b|\bstock quotes?\b|\blive ?blog\b|\blive blog\b|"
                          r"\b(?:stocks?|shares?) (?:live|today)\b|\bstock market today\b", re.I)
# Auto-generated market blurbs: an all-caps ticker in parentheses and a market phrase in one title.
_TICKER = re.compile(r"\((?:[A-Z]{2,8}\s*:\s*)?[A-Z]{1,6}(?:[.-][A-Z])?\)")
_MARKET_PHRASE = re.compile(r"\b(?:outperform(?:s|ed|ing)?|underperform(?:s|ed|ing)?|trending stocks?|"
                            r"investors heavily search|stock moves?|(?:shares?|stocks?) (?:rise|rises|rose|fall|falls|fell|"
                            r"jump|jumps|jumped|drop|drops|dropped|surge|surges|surged|slip|slips|slipped)|"
                            r"price targets?|broader market|here(?:'s| is) what you need to know)\b", re.I)


# With a ticker in the title, any trading word marks a market blurb ("(GE) stock sinks as market
# gains", "Stock (GE) Moved Up by 3.07%"); some phrases mark one even without a ticker.
_TRADING_WORD = re.compile(r"\b(?:stocks?|shares?|investors?|market|trading|moved (?:up|down)|"
                           r"here(?:'s| is) why|why it'?s moving|\d+(?:\.\d+)?%)", re.I)
_BLURB_PHRASE = re.compile(r"\b(?:buying opportunity|attracting investor attention|why it'?s moving|"
                           r"stock (?:sinks|soars|plunges|climbs|slides|tumbles|rallies)|"
                           r"shares (?:sink|soar|plunge|climb|slide|tumble|rally)|moved (?:up|down) by \d)", re.I)


def stock_blurb(title) -> bool:
    """Whether the title is an auto-generated stock blurb: a ticker in capitals in parentheses
    ("(VALE)", "(NYSE: X)") together with a market or trading word (outperforms, trending stock,
    shares fall, stock sinks, moved up by 3%, here is why), or a phrase only such blurbs use
    ("is this a buying opportunity", "attracting investor attention", "why it's moving")."""
    title = str(title or "")
    if _BLURB_PHRASE.search(title):
        return True
    return bool(_TICKER.search(title) and (_MARKET_PHRASE.search(title) or _TRADING_WORD.search(title)))


_POLICY_URL = re.compile(r"/(?:legal|policy|policies|terms|terms-of-service|terms-of-use|privacy|privacy-policy|tos|"
                         r"cookies?|gdpr|legal-notice)(?:/|$)")
_POLICY_TITLE = re.compile(r"\bterms (?:of (?:service|use)|and conditions|& conditions)\b|"
                           r"\bprivacy (?:policy|notice|statement)\b|\bpolicy updates?\b|\bcookie (?:policy|notice)\b|"
                           r"\blegal notices?\b|\bpolicies\b", re.I)
_DOCS_URL = re.compile(r"/(?:release-?notes?|releases|changelog|change-?log|whats-?new|docs?|documentation|api|"
                       r"reference|developers?|help|support|kb|knowledge-?base|status|guides?)(?:/|$)")
_DOCS_HOST = frozenset({"docs", "developer", "developers", "support", "help", "status", "api", "learn"})
_DOCS_TITLE = re.compile(r"\brelease notes?\b|\bchange-?log\b|\bapi reference\b|\bdocumentation\b|"
                         r"\bhelp cent(?:er|re)\b|\buser guide\b", re.I)
_ADVISORY_URL = re.compile(r"/(?:security-)?advisor(?:y|ies)(?:/|$)|/cve-\d")
_ADVISORY_TITLE = re.compile(r"\bsecurity advisor(?:y|ies)\b|\bcve-\d{4}-\d+", re.I)
_AGGREGATOR_TITLE = re.compile(r"\b(?:latest|breaking|top|all|today'?s)\s+(?:news|headlines|stories|updates)\b|"
                               r"\bnews(?:\s+and|,)\s+(?:updates|analysis|photos|videos)\b|\bnews archive\b|"
                               r"\bnews today\b", re.I)
_LIST_URL = re.compile(r"/awesome-|/(?:paper|reading)-?lists?(?:/|$)")
_LIST_TITLE = re.compile(r"\bawesome[- ]|\b(?:paper|reading|curated|resource) lists?\b|"
                         r"\blist of (?:papers|publications)\b|\bcollection of papers\b|"
                         r"\bpapers? (?:digest|roundup|weekly)\b|\bdaily papers\b", re.I)
_PRESS_URL = re.compile(r"/(?:news|press|newsroom|press-releases?|blog|stories|media|announcements?)/")


def _entity_domain(named, cased, host):
    """Whether the host is the entity's own: a label that is the name run together
    (huggingface.co), hyphenated into it, or its name without corporate suffixes."""
    label = _registrable_label(host).replace("-", "")
    for index, phrase in enumerate(named):
        if _domain_names(host, phrase):
            return True
        tokens = cased[index] if index < len(cased) else []
        if tokens and label:
            joined = {"".join(t.lower() for t in tokens if t.lower() not in _CONNECTORS),
                      "".join(t.lower() for t in _core_words(tokens)), "".join(t.lower() for t in tokens)}
            if label in joined:
                return True
    return False


def _canonical(ctx, url, host, item):
    """How much the result is the thing named: a repository whose name (and owner) is the
    project, or a site whose domain is the entity. 0 when it is neither."""
    if not ctx.names:
        return 0.0
    segments = [unquote(part).lower().replace("-", "") for part in urlparse(url).path.split("/") if part]
    registrable = ".".join(host.removeprefix("www.").split(".")[-2:])
    if registrable in _FORGES:
        owner = segments[0] if segments else ""
        repo = segments[1] if len(segments) >= 2 else ""
        if repo and repo in ctx.names:
            stars = ((item.get("extra") or {}).get("stars")) if isinstance(item.get("extra"), dict) else None
            # Among repositories of the name, the one people use: stars break the tie with forks.
            bonus = min(0.9, math.log10(stars + 1) / 7) if isinstance(stars, (int, float)) and stars > 0 else 0.0
            return 3.0 + (1.0 if owner in ctx.names else 0.0) + bonus
        if owner in ctx.names:
            return 2.0 if not repo else 1.5
        return 0.0
    return 3.0 if _registrable_label(host).replace("-", "") in ctx.names or _entity_domain(
        ctx.named, ctx.cased, host) else 0.0


# Repositories that are about using or measuring something else, not the thing itself.
_EXAMPLE_TOKENS = frozenset({"demo", "demos", "example", "examples", "sample", "samples", "template", "templates",
                             "starter", "starters", "boilerplate", "tutorial", "tutorials", "benchmark",
                             "benchmarks", "awesome"})
_GUIDE_TITLE = re.compile(r"\b(?:how to|how do|guide to|a guide|beginner'?s?|tutorial|step[- ]by[- ]step|"
                          r"getting started|introduction to|learn(?:ing)?)\b", re.I)
_LIBRARY_TITLE = re.compile(r"\b(?:libraries|library|packages?|crates?|frameworks?|modules?|sdks?|toolkits?)\b", re.I)
_LIBRARY_URL = re.compile(r"/(?:packages?|project|projects|crates?|libraries|library|pkg)(?:/|$)|"
                          r"^(?:pypi\.org|crates\.io|npmjs\.com|pkg\.go\.dev|libraries\.io|rubygems\.org|"
                          r"mvnrepository\.com|packagist\.org|nuget\.org)$")
_GUIDE_ASKED = frozenset({"guide", "tutorial", "tutorials", "how", "learn", "beginner", "beginners", "intro",
                          "introduction"})


# What a "library for X" query asks for, and what else a repository may call itself.
_NEED_NOUNS = frozenset({"library", "libraries", "lib", "libs", "package", "packages", "module", "modules",
                         "crate", "crates", "sdk", "sdks", "framework", "frameworks", "toolkit", "toolkits",
                         "client", "clients"})
_NEED_NOUN = re.compile(r"(?<![^\W_])(?:librar(?:y|ies)|libs?|packages?|modules?|crates?|sdks?|frameworks?|"
                        r"toolkits?|clients?)(?![^\W_])", re.I)


def _squash(text):
    return re.sub(r"[-_.\s]", "", str(text).lower())


def _need_fit(item, ctx, name=""):
    """Whether a repository answers a need query ("go library for configuration files"): when the
    query asks for a kind of thing (library, package, crate, module, sdk, framework, toolkit,
    client) its description (or name) says it is one, library or a synonym; a repository that only
    talks about the topic is a tool. A need that names no kind needs every topic word."""
    text = str(item.get("snippet") or "") + " " + str(name)
    if ctx.words & _NEED_NOUNS:
        return _NEED_NOUN.search(text) is not None
    topic = [term for term in ctx.terms if term not in _NEED_NOUNS]
    return all(_has(term, text.lower()) for term in topic)


def _code_adjustment(item, ctx, url, host, title):
    """How a result suits a code query. A query for the repository (github, repo) prefers
    repository pages (owner/name) to an organisation's page or a project's website; demos, examples,
    templates, starters, tutorials, benchmarks and awesome-lists go last unless asked for; for a
    name lookup the repository of that name leads and repositories of any other name (add-ons,
    plugins, siblings) rank below the web pages about the project; for a need ("library for X")
    repositories whose description says they are a library (or package, crate, module, sdk, framework,
    toolkit, client) lead, popular ones first, with the pages that name libraries, and how-to guides
    follow them; a repository that is only a tool on the topic goes below those pages."""
    parts = _repo_parts(item)
    delta = 0.0
    segments = [part for part in urlparse(url).path.split("/") if part]
    if parts:
        owner, name = parts
        if ctx.repo:
            delta += 2.0
        if _EXAMPLE_TOKENS & set(re.split(r"[-_.\s]+", name.lower())) and not ctx.words & _EXAMPLE_TOKENS:
            delta -= 20.0
        if ctx.lookup and ctx.names:
            # The repository of the project's name leads; add-ons, plugins and siblings
            # (pytest_httpx, httpx-sse, tokio-core) follow every web page about the project.
            if _squash(name) not in {_squash(known) for known in ctx.names}:
                delta -= 20.0   # more than the 12 a project's front page loses when the repository was asked for
        if ctx.need:
            if _need_fit(item, ctx, name):
                stars = _stars(item)
                if stars and stars > 0:
                    delta += min(3.0, math.log10(stars + 1) / 1.5)
            else:
                # A tool, not the library asked for: below the web pages that name libraries.
                delta -= 10.0
        return delta
    if ctx.repo and len(segments) <= 1:
        # An organisation's page or a project's front page, when the repository was asked for.
        delta -= 12.0
    if ctx.need and "docs" not in ctx.asks:
        if _LIBRARY_TITLE.search(title) or _LIBRARY_URL.search(unquote(urlparse(url).path.lower())) \
                or _LIBRARY_URL.search(host):
            delta += 1.5
        if _GUIDE_TITLE.search(title) and not ctx.words & _GUIDE_ASKED:
            delta -= 3.0
    return delta


def _compact(terms, title):
    """Whether the terms sit close together in the title: all within a stretch of the title's words
    only a few words longer than the terms themselves."""
    words = re.findall(r"[a-z0-9]+", title)
    spots = []
    for term in terms:
        found = [index for index, word in enumerate(words) if _has(term, word)]
        if not found:
            return False
        spots.append(found)
    # the narrowest stretch holding one spot of every term, anchored on each spot of the first term
    narrowest = len(words)
    for anchor in spots[0]:
        chosen = [anchor] + [min(other, key=lambda index: abs(index - anchor)) for other in spots[1:]]
        narrowest = min(narrowest, max(chosen) - min(chosen) + 1)
    return narrowest <= len(terms) + 3


def relevance(item: dict, ctx: QueryContext) -> float:
    """How well a result answers the query, for ordering results of the same tier. Strongest
    first: the entity (or every key term) in the title; the result being the thing itself (its
    domain, or its GitHub owner/repo, is the entity or project name); the entity's own site for
    a news or announcement query; key terms only in the snippet. Pages that are not what a
    search is after count against it, from their URL and title alone: stock quotes and share
    prices, policy and legal pages, release notes and docs (for announcements), security
    advisory listings, news aggregators, and awesome-lists (for paper queries) -- each unless
    the query asks for that kind of page."""
    url, title = str(item.get("url") or ""), str(item.get("title") or "")
    snippet = str(item.get("snippet") or "").lower()
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = unquote(parsed.path).lower()
    low = title.lower()
    score = 0.0
    if ctx.terms:
        hits = sum(1 for term in ctx.terms if _has(term, low))
        entity_in_title = False
        for index, phrase in enumerate(ctx.named):
            tokens = ctx.cased[index] if index < len(ctx.cased) else []
            if all(_has(_stem(word), low) for word in phrase):
                entity_in_title = True
            elif tokens and any(re.search(r"(?<![^\W_])" + re.escape(a) + r"(?![^\W_])", title)
                                for a in _acronyms(tokens)):
                entity_in_title = True
        if entity_in_title or hits == len(ctx.terms):
            # A paper's topic is a phrase: all the terms scattered across a long title are half a match.
            score += 4.0 if not (ctx.papers and hits == len(ctx.terms) > 1 and not _compact(ctx.terms, low)) else 2.0
        elif hits * 2 >= len(ctx.terms):
            score += 2.0
        elif any(_has(term, snippet) for term in ctx.terms):
            score += 1.0
    score += _canonical(ctx, url, host, item)
    if ctx.papers and len(ctx.terms) >= 2:
        # A paper is about what its title says: under half the topic in the title ranks below any
        # paper with more.
        covered = sum(1 for term in ctx.terms if _has(term, low))
        score += covered / len(ctx.terms) if covered * 2 >= len(ctx.terms) else -6.0
        # Terms that are next to each other in the query and in the title (or abstract) are the topic.
        pairs = list(zip(ctx.terms, ctx.terms[1:]))
        score += (0.75 * min(2, sum(1 for pair in pairs if _phrase_in(pair, low)))
                  + 0.5 * min(2, sum(1 for pair in pairs if _phrase_in(pair, snippet))))
    if ctx.announce:
        if ctx.named and _entity_domain(ctx.named, ctx.cased, host):
            score += 2.0
        if _PRESS_URL.search(path) and url_kind(url) == "item":
            score += 1.0
    if ctx.code:
        score += _code_adjustment(item, ctx, url, host, title)
    # The penalties outweigh everything above (at most about 11), so a page of an unwanted
    # kind ranks below every ordinary passing result, even on the entity's own domain.
    asks = ctx.asks
    if "stock" not in asks and (_STOCK_URL.search(path) or _STOCK_TITLE.search(title)):
        score -= 20.0
    if "policy" not in asks and (_POLICY_URL.search(path) or _POLICY_TITLE.search(title)):
        score -= 20.0
    if ctx.announce:
        if "stock" not in asks and stock_blurb(title):
            score -= 12.0
        if "docs" not in asks and (_DOCS_URL.search(path) or host.split(".")[0] in _DOCS_HOST
                                   or _DOCS_TITLE.search(title)):
            score -= 12.0
        if "security" not in asks and (_ADVISORY_URL.search(path) or _ADVISORY_TITLE.search(title)):
            score -= 12.0
        if _AGGREGATOR_TITLE.search(title):
            score -= 6.0
    if ctx.papers and (_LIST_URL.search(path) or _LIST_TITLE.search(title)):
        score -= 20.0
    return round(score, 3)


# Words next to an entity that make no new name of it: "Latest Block news", "Block Inc".
_SAFE_NEIGHBOURS = (_QUERY_FILLER | _NEWSY | _GENERIC_CAPS | _CORPORATE_SUFFIXES | _CONNECTORS | _STOPWORDS)


def _wrong_entity(named, cased, item) -> bool:
    """A result about another thing that shares the query entity's name. Only for an entity
    of one word (Block, Unity, Arm), and only when the result's domain is not the entity's:
    the title has the word as part of a longer proper-noun phrase, either led by a capitalised
    article ("The Block") or with another capitalised word directly before it ("Cinder
    Block") in a title that is not all capitalised words. Possessives ("Dorsey's Block"),
    corporate suffixes and freshness words don't make a new name."""
    if len(named) != 1 or len(named[0]) != 1:
        return False
    # The entity as written: "GE Aerospace" keeps "GE" out of `named` (short word) but is a
    # two-word name, not a single common word, and its own words are never a different entity.
    written = [str(w) for w in (cased[0] if cased else [])]
    own = {named[0][0]} | {w.lower() for w in written}
    if len([w for w in written if w.lower() not in _CONNECTORS]) > 1:
        return False
    host = (urlparse(str(item.get("url") or "")).hostname or "").lower()
    if _entity_domain(named, cased, host):
        return False
    title = str(item.get("title") or "")
    pattern = re.compile(r"(?<![^\W_])" + re.escape(named[0][0]) + r"(?:s)?(?![^\W_])", re.I)
    found = list(pattern.finditer(title))
    if not found:
        return False
    words = _WORD.findall(title)
    mixed = any(word[:1].islower() and word.lower() not in _SAFE_NEIGHBOURS for word in words)
    for match in found:
        before = title[:match.start()]
        # "The Block" anywhere in the title, or "A Block" / "An Arm" opening it.
        if re.search(r"(?:^|\W)The\s+$", before) or re.fullmatch(r"\W*(?:A|An)\s+", before):
            continue
        previous = re.search(r"([^\W\d_][\w'\u2019.-]*)\s+$", before)
        if mixed and previous:
            token = previous.group(1)
            if (token[:1].isupper() and token.lower() not in _SAFE_NEIGHBOURS
                    and token.lower() not in own
                    and not token.lower().endswith(("'s", "\u2019s"))):
                continue
        return False
    return True


def unrelated_set(query: str, results) -> bool:
    """Whether a source's whole result set is off the query: it has results and none of them is
    on the query by the same rule the checks use (_on_query: enough of the key terms in the
    title, URL and snippet, not one word that fits any page). A source that answers every
    query with the same pages looks like this, and is failing, not finding nothing."""
    terms = key_terms(query)
    if not terms or not results:
        return False
    runs = _entity_runs(query)
    named, cased = [phrase for phrase, _ in runs], [tokens for _, tokens in runs]
    return not any(_on_query(terms, named, item, cased) for item in results)


def _repo_parts(item):
    """(owner, name) of a result on a code forge that is a repository page (exactly two path
    segments), else None."""
    parsed = urlparse(str(item.get("url") or ""))
    host = (parsed.hostname or "").lower().removeprefix("www.")
    segments = [unquote(part) for part in parsed.path.split("/") if part]
    if host in _FORGE_HOSTS and len(segments) == 2:
        return segments[0], segments[1]
    return None


def _stars(item):
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    for key in ("stargazers_count", "stars"):
        if isinstance(extra.get(key), (int, float)) and not isinstance(extra.get(key), bool):
            return extra[key]
    return None


# Reasons that say the repository itself is not wanted, wherever its page was found.
REPO_UNWANTED = frozenset({"fork", "archived", "wrong_language", "low_stars"})

# A repository of the wrong kind or size for what the query wants.
MIN_STARS_NEED = 500
MIN_STARS_LOOKUP = 100


def _repo_reasons(item, ctx):
    """Why a GitHub repository result (one that carries the source's own fork, archived,
    language and stars) is not what a code query wants: a fork or an archived repository; a
    language that is known and is not the one the query names; too few stars for a need
    ("library for X", "X framework": 500) or for a name lookup that is not the repository of that
    name (100). Fields the source did not give are not held against it."""
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    reasons = []
    if extra.get("fork") is True:
        reasons.append("fork")
    if extra.get("archived") is True:
        reasons.append("archived")
    if ctx is None or not ctx.code:
        return reasons
    language = extra.get("language")
    if ctx.languages and isinstance(language, str) and language.strip() \
            and language.strip().lower() not in ctx.languages:
        reasons.append("wrong_language")
    stars = _stars(item)
    if stars is not None:
        if ctx.need and stars < MIN_STARS_NEED:
            reasons.append("low_stars")
        elif ctx.lookup and stars < MIN_STARS_LOOKUP:
            parts = _repo_parts(item)
            if not (parts and parts[1].lower().replace("-", "") in ctx.names):
                reasons.append("low_stars")
    return reasons


def check_result(item: dict, terms, *, since=None, until=None, specific=True, now=None, named=(),
                 cased=(), article=False, ctx=None) -> dict:
    """One result's verdict: passed, the reasons it did not, and the check fields. With
    article (an announcement query in a window), a result passes only with a verified date in
    the window or an article-like path (see article_like); anything else fails as not_article, and so
    does an article-like path under a marketing or help segment on the entity's own domain
    (see marketing_path) without a verified date."""
    now = now or datetime.now(timezone.utc)
    url, title = str(item.get("url") or ""), str(item.get("title") or "")
    reasons = []
    when, origin = published(item, now)
    in_window = None
    if (since is not None or until is not None) and when is not None:
        in_window = not ((since is not None and when < since) or (until is not None and when > until))
        if not in_window:
            reasons.append("out_of_window")
    on_query = _on_query(terms, named, item, cased,
                         entity=bool(ctx is not None and ctx.need and _LIBRARY_TITLE.search(title)))
    if not on_query:
        reasons.append("off_query")
    elif _wrong_entity(named, cased, item):
        on_query = False
        reasons.append("wrong_entity")
    kind = url_kind(url)
    if kind == "item" and specific and _names_the_entity(url, named):
        kind = "homepage"
    if kind != "item" and (specific or kind == "search_page"):
        reasons.append(kind)
    lowered = title.lower()
    if any(word in lowered for word in _CHALLENGE):
        reasons.append("challenge_page")
    if article and in_window is not True and (not article_like(url) or (
            named and marketing_path(url) and _entity_domain(named, cased, (urlparse(url).hostname or "").lower()))):
        reasons.append("not_article")
    reasons.extend(_repo_reasons(item, ctx))
    return {"url": url, "passed": not reasons, "reasons": reasons, "in_window": in_window,
            "on_query": on_query, "published": when.strftime("%Y-%m-%dT%H:%M:%SZ") if when else None,
            "date_source": origin, "kind": kind}


# --- how far to trust a source, when paid ones are in the mix

# What a source's results are worth before the checks, as the chance one is useful. Paid sources
# bring their own measured rate (the bundled priors); these are the free ones.
FREE_SOURCE_PRIORS = {"searxng": 0.75, "bing_news_rss": 0.6, "wikipedia": 0.6, "github": 0.5, "arxiv": 0.8,
                      "stack_exchange": 0.7}
DEFAULT_SOURCE_PRIOR = 0.5
# A prior difference of 0.2 (the best paid source over SearXNG) is worth about 0.9 of relevance:
# enough to break a tie, not enough to beat a canonical result (3 or more) or a verified date.
PRIOR_WEIGHT = 4.0
HIGH_STARS = 1000


def topic_matches(query: str, title) -> bool:
    """Whether a reference article's title is the query's topic: equal to it, or equal once a
    trailing parenthetical is dropped ("Moonlight Sonata", "Samsung (company)"). The topic of
    "who founded X" or "population of X" is X."""
    topic = _wiki_key(reference_topic(query))
    return bool(topic) and _wiki_key(title) == topic


def source_prior(source, item: dict, query: str, ctx: QueryContext, paid_rate=None) -> float:
    """The prior for one result from one source: a paid source's measured rate, else the free
    default (SearXNG 0.75, Bing News 0.6, Wikipedia 0.6, GitHub 0.5, arXiv 0.8, Stack Exchange 0.7,
    others 0.5). A Wikipedia article titled for the topic is 0.9; a GitHub repository that is the
    project asked for, or has HIGH_STARS stars, is 0.85."""
    if paid_rate is not None:
        return float(paid_rate)
    prior = FREE_SOURCE_PRIORS.get(source, DEFAULT_SOURCE_PRIOR)
    if source == "wikipedia" and topic_matches(query, item.get("title")):
        return 0.9
    if source == "github":
        url = str(item.get("url") or "")
        host = (urlparse(url).hostname or "").lower()
        stars = _stars(item)
        if (ctx.names and _canonical(ctx, url, host, item) >= 3.0) or (stars or 0) >= HIGH_STARS:
            return 0.85
    return prior


def demote_off_topic(query: str, items, specialist_only) -> list:
    """For a fact question: a specialist's (Wikipedia's) result whose title is not the topic goes
    below every other result, so it fills a slot only when nothing else is left. specialist_only
    is the set of canonical URLs that only specialist sources returned."""
    first, last = [], []
    for item in items:
        off = canonical_url(item.get("url") or "") in specialist_only and not topic_matches(query, item.get("title"))
        (last if off else first).append(item)
    return first + last


def demote_unconfirmed_repos(items, confirmed, need=False) -> list:
    """For a code query that names a language: repository pages (owner/name) that only a web
    engine returned go after the ones the code source returned, which searched in that language.
    confirmed is the set of canonical URLs the code source returned. For a need ("library for X"),
    a confirmed repository the ranking itself put far down (a tool where a library was asked for:
    relevance under -5) does not keep the others behind it; it goes last."""
    first, middle, last = [], [], []
    for item in items:
        if _repo_parts(item) is None:
            first.append(item)
        elif canonical_url(item.get("url") or "") not in confirmed:
            middle.append(item)
        elif need and ((item.get("check") or {}).get("relevance") or 0.0) < -5.0:
            last.append(item)
        else:
            first.append(item)
    return first + middle + last


def counted(dated: int, undated: int, need: int, weigh_undated: bool) -> int:
    """How many passing results count toward "good". An undated result is weaker
    than a dated one, so with weigh_undated they count at most half the quota
    (need // 2): a set of undated filler cannot end the search alone."""
    return dated + (min(undated, need // 2) if weigh_undated else undated)


def _stock_blurbs(results, verdicts, ctx):
    """For a news or announcement query that is not about stocks: a stock blurb fails as
    stock_blurb when the set has a passing result that is not one (they are demoted either
    way, see relevance); with nothing else to show, it stays."""
    if not ctx.announce or "stock" in ctx.asks:
        return
    blurbs = [verdict for item, verdict in zip(results, verdicts) if stock_blurb(item.get("title"))]
    marked = {id(verdict) for verdict in blurbs}
    if blurbs and any(verdict["passed"] for verdict in verdicts if id(verdict) not in marked):
        for verdict in blurbs:
            verdict["reasons"].append("stock_blurb")
            verdict["passed"] = False


def _paper_phrases(results, verdicts, ctx):
    """For a papers query with a topic phrase (two key terms next to each other in the query): when a
    passing paper has the phrase in its title, papers whose title has a single topic term rank lower
    still (on top of the under-half penalty in relevance)."""
    if not ctx.papers or len(ctx.terms) < 2:
        return
    titles = [str(item.get("title") or "").lower() for item in results]
    if not any(verdict["passed"] and _phrase_hit(list(ctx.terms), title) for verdict, title in zip(verdicts, titles)):
        return
    for verdict, title in zip(verdicts, titles):
        if sum(1 for term in ctx.terms if _has(term, title)) <= 1:
            verdict["relevance"] = round(verdict["relevance"] - 4.0, 3)


def check_results(query: str, results, *, limit: int = 10, recency=None, since=None, until=None,
                  now=None, min_good: int = 3, weigh_undated: bool = False, vertical=None,
                  priors=None) -> dict:
    """Verdict per result and for the set. good: at least min(min_good, limit)
    results pass; weak: some do; empty: none do. Repeats of a page already seen
    fail as duplicates. With weigh_undated, passing results of a windowed query
    whose date is unknown count at most half the quota (see counted). Every verdict also
    carries "relevance" (see relevance), which orders results within a tier.

    A windowed announcement query (announce, announcement, press release, launch) only accepts
    a result with a verified in-window date or an article-like path (see article_like); the
    rest fail as not_article. priors, {"source": id, "paid_rate": rate or None}, adds each
    verdict's "prior" (see source_prior) for the merge to weigh when paid sources are in play."""
    now = now or datetime.now(timezone.utc)
    since, until = window_bounds(recency, since, until, now)
    context = query_context(query, recency="window" if (since is not None or until is not None) else None,
                            vertical=vertical)
    # A name lookup is about the project, not the language or platform words beside it.
    terms = list(context.terms) if context.lookup else key_terms(query)
    runs = _entity_runs(query)
    named = [phrase for phrase, _ in runs]
    cased = [tokens for _, tokens in runs]
    specific = _asks_specific(terms, bool(recency or since or until), query, named)
    article = bool((since is not None or until is not None) and _ANNOUNCE.search(str(query or "")))
    verdicts, seen, counts = [], set(), {}
    for item in results:
        verdict = check_result(item, terms, since=since, until=until, specific=specific, now=now, named=named,
                               cased=cased, article=article, ctx=context)
        verdict["relevance"] = relevance(item, context)
        if priors:
            verdict["prior"] = round(source_prior(priors.get("source"), item, str(query or ""), context,
                                                  priors.get("paid_rate")), 3)
        verdicts.append(verdict)
    _stock_blurbs(results, verdicts, context)
    _paper_phrases(results, verdicts, context)
    verdicts, listed = [], verdicts
    for verdict in listed:
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
                                 ("passed", "reasons", "in_window", "on_query", "published", "date_source", "kind",
                                  "relevance", "prior") if key in verdict}}
        if report["windowed"] and verdict["published"] and not item.get("published"):
            row["published"] = verdict["published"]
        rows.append(row)
    return rows


def _relevance(item):
    """The ordering score: relevance, plus the source prior's weight when the check has one
    (only when paid sources were allowed)."""
    check = item.get("check") or {}
    return (check.get("relevance") or 0.0) + PRIOR_WEIGHT * (check.get("prior") or 0.0)


def accepted(results, report: dict) -> list[dict]:
    """The results that passed, each carrying its check fields, the more relevant first
    (a stable sort: the source's own order breaks ties). When a window was asked for,
    dated in-window results come before those with unknown dates."""
    kept = [item for item in annotate(results, report) if item["check"]["passed"]]
    windowed = report["windowed"]
    return sorted(kept, key=lambda item: (_tier(item) if windowed else 0, -_relevance(item)))


def merge_results(groups, *, windowed=False, limit=None) -> list[dict]:
    """Passing result lists from several sources as one: deduplicated by canonical
    URL, fused by reciprocal rank (k=60) so a page several sources agree on rises,
    dated in-window results ahead of unknown dates when a window was asked for, and
    within a tier the more relevant (check.relevance) ahead of the less.
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
                # A page several sources return takes the best of their priors.
                better = (item.get("check") or {}).get("prior") or 0.0
                if better > ((fused[key].get("check") or {}).get("prior") or 0.0):
                    fused[key]["check"] = {**(fused[key].get("check") or {}), "prior": better}
            fused[key]["fusion"]["score"] += 1.0 / (60 + rank + 1)
            fused[key]["fusion"]["sources"].append({"source": source, "rank": rank + 1})
    merged = sorted((fused[key] for key in order), key=lambda item: (_tier(item) if windowed else 0,
                                                                    -_relevance(item), -item["fusion"]["score"]))
    for item in merged:
        item["fusion"]["score"] = round(item["fusion"]["score"], 5)
    return merged[:limit] if limit else merged


# --- the answer when a paid source answered: anchored on its results

def anchor_reasons(item: dict, ctx: QueryContext) -> list[str]:
    """Extra hard failures for a code query when the answer is anchored on a paid source. In
    ranking they are demotions (see _code_adjustment); here they remove the result. An
    organisation's or user's page (one path segment on a code forge), or a project's front page on
    any other site, when the query asks for the repository (github, repo, repository): "homepage".
    A demo, example, sample, template, starter, boilerplate, tutorial, benchmark or awesome
    repository unless the query asks for that kind: "example_repo"."""
    if not ctx.code:
        return []
    url = str(item.get("url") or "")
    parts = _repo_parts(item)
    if parts:
        if _EXAMPLE_TOKENS & set(re.split(r"[-_.\s]+", parts[1].lower())) and not ctx.words & _EXAMPLE_TOKENS:
            return ["example_repo"]
        return []
    if ctx.repo and len([part for part in urlparse(url).path.split("/") if part]) <= 1:
        return ["homepage"]
    return []


def is_canonical(query: str, item: dict, ctx: QueryContext, vertical=None) -> bool:
    """Whether a result is the canonical answer to a lookup: for a code name lookup the
    repository of the project's name; for a reference question the article titled for its topic."""
    if ctx.code and ctx.lookup and ctx.names:
        url = str(item.get("url") or "")
        host = (urlparse(url).hostname or "").lower()
        return _repo_parts(item) is not None and _canonical(ctx, url, host, item) >= 3.0
    return vertical == "reference" and topic_matches(query, item.get("title"))


def anchored_answer(query: str, source: str, anchored, others, *, limit: int, vertical=None,
                    unwanted=None) -> tuple[list, dict]:
    """The answer when a paid source answered. anchored is its annotated result list in its own
    order (every result with its check); the first `limit` stay where they are unless they fail:
    a failed check (off_query, out_of_window, wrong_entity, homepage and the other kinds,
    stock_blurb, challenge_page, not_article, duplicate), a repository the code source showed to be
    unwanted (unwanted maps canonical URL to its reason), or an anchor_reasons failure. A result
    that merely ranks lower stays. others is the passing results of the other sources, best
    first. They fill the removed slots in order; a canonical result (see is_canonical) the paid set
    lacks goes first, into a removed slot, else the last slot of a full set (or after a short one);
    a short set is then filled the same way. Returns (results, receipt) where the receipt is
    {"source", "kept", "replaced": [{"url", "reason"}], "filled"}."""
    ctx = query_context(query, vertical=vertical)
    unwanted = unwanted or {}

    def failure(item):
        check = item.get("check") or {}
        reasons = list(check.get("reasons") or [])
        if reasons:
            return reasons[0]
        if check.get("passed") is False:
            return "failed"
        reason = unwanted.get(canonical_url(item.get("url") or ""))
        if reason:
            return reason
        found = anchor_reasons(item, ctx)
        return found[0] if found else None

    slots, replaced = [], []
    for item in list(anchored)[:limit]:
        reason = failure(item)
        if reason:
            slots.append(None)
            replaced.append({"url": item.get("url"), "reason": reason})
        else:
            slots.append(item)
    kept = [item for item in slots if item is not None]
    taken = {canonical_url(item.get("url") or "") for item in kept}
    fills = [item for item in others if canonical_url(item.get("url") or "") not in taken and not failure(item)]
    filled = 0
    canonical = next((item for item in fills if is_canonical(query, item, ctx, vertical)), None)
    if canonical is not None:
        fills.remove(canonical)
        holes = [index for index, item in enumerate(slots) if item is None]
        if holes:
            slots[holes[0]] = canonical
        elif len(slots) >= limit and slots:
            replaced.append({"url": slots[-1].get("url"), "reason": "displaced_by_canonical"})
            kept.remove(slots[-1])
            slots[-1] = canonical
        else:
            slots.append(canonical)
        filled += 1
    for index, item in enumerate(slots):
        if item is None and fills:
            slots[index] = fills.pop(0)
            filled += 1
    final = [item for item in slots if item is not None]
    while len(final) < limit and fills:
        final.append(fills.pop(0))
        filled += 1
    return final, {"source": source, "kept": len(kept), "replaced": replaced, "filled": filled}


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
                       since=None, until=None, now=None, min_good=3, weigh_undated=True, vertical=None,
                       priors=None):
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
                          min_good=min_good, weigh_undated=weigh_undated, vertical=vertical, priors=priors)
    for verdict in again["results"]:
        if canonical_url(verdict["url"]) in pages and verdict["published"]:
            verdict["date_source"] = "page"
            stats["dropped"] += 1 if verdict["in_window"] is False else 0
    return revised, again, stats
