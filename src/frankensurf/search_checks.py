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
from .search import RECENCY, github_parts, parse_date

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


def key_terms(query: str) -> list[str]:
    """What the query is about: its words without operators, stopwords, years and
    freshness words (news, latest, this week), lightly stemmed. Two-character names
    written in capitals or with a digit (BP, GE, 3M) stay; other short words go.
    Platform words in lower case (github, repo, library) count only when nothing
    else is left."""
    return _terms(query, True)


def raw_terms(query: str) -> list[str]:
    """key_terms without the stemming: the words as written, lowercased (pandas, not panda)."""
    return _terms(query, False)


def _terms(query, stem):
    terms, platform = [], []
    for raw in _WORD.findall(_OPERATOR.sub(" ", str(query or ""))):
        word = raw.lower()
        if word in _STOPWORDS or word in _QUERY_FILLER or _YEAR.fullmatch(word):
            continue
        if len(word) < 3 and word.isascii() and not (len(word) == 2 and (raw.isupper() or any(c.isdigit() for c in raw))):
            continue
        word = _stem(word) if stem and len(word) > 2 else word
        # Written as a name (GitHub, Docs), a platform word is the topic after all.
        bucket = platform if word in _PLATFORM_WORDS and raw.islower() else terms
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
        if match := _URL_MONTH_NAME.search(path):
            year, month = int(match[1]), _month_number(match[2])
            if match[3] and int(match[3]) == month:
                return _day_end(year, month, int(match[4]))
            return _day_end(year, month, calendar.monthrange(year, month)[1])
        if match := _URL_STAMP.search(path):
            return _day_end(int(match[1]), int(match[2]), int(match[3]))
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


def _on_query(terms, named, item, cased=()):
    """Whether a result is about the query. Terms match whole words (light
    stemming), never inside other words. When the query names something, the
    result must too: all of an entity's words in the title or URL, or its domain
    (a snippet-only mention only counts for a multi-word name, or alongside
    another query term). The entity also goes by aliases: its words minus
    corporate suffixes (Arm for Arm Holdings), its acronym (AWS for Amazon Web
    Services), the registrable domain, and, for an all-caps query entity
    (AWS), the capitalised words that spell it in the title. Without a named
    entity any key term will do."""
    if not terms:
        return True
    url = str(item.get("url") or "")
    title = str(item.get("title") or "")
    head = (title + " " + unquote(url)).lower()
    snippet = str(item.get("snippet") or "").lower()
    if not named:
        return any(_has(term, head) or _has(term, snippet) for term in terms)
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
    if vertical == "code" or words & _CODE_WORDS:
        found = github_parts(query)
        if found["name_lookup"]:
            # A project by name ("httpx python github"): the project is the topic, the language
            # and the platform are not, and the repository carrying its name is canonical.
            terms = tuple(_stem(word) if len(word) > 2 else word for word in found["terms"])
            names = names | {word.replace("-", "") for word in found["terms"]} | {"".join(found["terms"]).replace("-", "")}
    return QueryContext(
        terms=terms, named=named, cased=cased,
        names=frozenset(name for name in names if len(name) >= 2),
        announce=bool(recency) or bool(words & _ANNOUNCE_WORDS) or vertical == "news",
        papers=vertical == "papers" or bool(words & _PAPER_WORDS),
        asks=frozenset(name for name, group in _ASKS.items() if words & group))


# Generic patterns for pages that are not what a search is after.
_STOCK_URL = re.compile(r"/(?:quotes?|stocks?|stock-?quotes?|stock-?prices?|share-?prices?|live-?blog|stock-market)"
                        r"(?:/|-|$)|(?:share|stock)-?price|liveblog")
_STOCK_TITLE = re.compile(r"\b(?:share|stock) prices?\b|\bstock quotes?\b|\blive ?blog\b|\blive blog\b|"
                          r"\b(?:stocks?|shares?) (?:live|today)\b|\bstock market today\b", re.I)
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
            score += 4.0
        elif hits * 2 >= len(ctx.terms):
            score += 2.0
        elif any(_has(term, snippet) for term in ctx.terms):
            score += 1.0
    score += _canonical(ctx, url, host, item)
    if ctx.announce:
        if ctx.named and _entity_domain(ctx.named, ctx.cased, host):
            score += 2.0
        if _PRESS_URL.search(path) and url_kind(url) == "item":
            score += 1.0
    # The penalties outweigh everything above (at most about 11), so a page of an unwanted
    # kind ranks below every ordinary passing result, even on the entity's own domain.
    asks = ctx.asks
    if "stock" not in asks and (_STOCK_URL.search(path) or _STOCK_TITLE.search(title)):
        score -= 20.0
    if "policy" not in asks and (_POLICY_URL.search(path) or _POLICY_TITLE.search(title)):
        score -= 20.0
    if ctx.announce:
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
                    and not token.lower().endswith(("'s", "\u2019s"))):
                continue
        return False
    return True


def unrelated_set(query: str, results) -> bool:
    """Whether a source's whole result set is off the query: it has results and none of them
    mentions any of the query's key terms (title, URL or snippet). A source that answers every
    query with the same pages looks like this, and is failing, not finding nothing."""
    terms = key_terms(query)
    if not terms or not results:
        return False
    for item in results:
        text = (str(item.get("title") or "") + " " + unquote(str(item.get("url") or "")) + " "
                + str(item.get("snippet") or "")).lower()
        if any(_has(term, text) for term in terms):
            return False
    return True


def check_result(item: dict, terms, *, since=None, until=None, specific=True, now=None, named=(),
                 cased=()) -> dict:
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
    on_query = _on_query(terms, named, item, cased)
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
    return {"url": url, "passed": not reasons, "reasons": reasons, "in_window": in_window,
            "on_query": on_query, "published": when.strftime("%Y-%m-%dT%H:%M:%SZ") if when else None,
            "date_source": origin, "kind": kind}


def counted(dated: int, undated: int, need: int, weigh_undated: bool) -> int:
    """How many passing results count toward "good". An undated result is weaker
    than a dated one, so with weigh_undated they count at most half the quota
    (need // 2): a set of undated filler cannot end the search alone."""
    return dated + (min(undated, need // 2) if weigh_undated else undated)


def check_results(query: str, results, *, limit: int = 10, recency=None, since=None, until=None,
                  now=None, min_good: int = 3, weigh_undated: bool = False, vertical=None) -> dict:
    """Verdict per result and for the set. good: at least min(min_good, limit)
    results pass; weak: some do; empty: none do. Repeats of a page already seen
    fail as duplicates. With weigh_undated, passing results of a windowed query
    whose date is unknown count at most half the quota (see counted). Every verdict also
    carries "relevance" (see relevance), which orders results within a tier."""
    now = now or datetime.now(timezone.utc)
    since, until = window_bounds(recency, since, until, now)
    context = query_context(query, recency="window" if (since is not None or until is not None) else None,
                            vertical=vertical)
    terms = key_terms(query)
    runs = _entity_runs(query)
    named = [phrase for phrase, _ in runs]
    cased = [tokens for _, tokens in runs]
    specific = _asks_specific(terms, bool(recency or since or until), query, named)
    verdicts, seen, counts = [], set(), {}
    for item in results:
        verdict = check_result(item, terms, since=since, until=until, specific=specific, now=now, named=named,
                               cased=cased)
        verdict["relevance"] = relevance(item, context)
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
                                  "relevance") if key in verdict}}
        if report["windowed"] and verdict["published"] and not item.get("published"):
            row["published"] = verdict["published"]
        rows.append(row)
    return rows


def _relevance(item):
    return (item.get("check") or {}).get("relevance") or 0.0


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
            fused[key]["fusion"]["score"] += 1.0 / (60 + rank + 1)
            fused[key]["fusion"]["sources"].append({"source": source, "rank": rank + 1})
    merged = sorted((fused[key] for key in order), key=lambda item: (_tier(item) if windowed else 0,
                                                                    -_relevance(item), -item["fusion"]["score"]))
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
                       since=None, until=None, now=None, min_good=3, weigh_undated=True, vertical=None):
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
                          min_good=min_good, weigh_undated=weigh_undated, vertical=vertical)
    for verdict in again["results"]:
        if canonical_url(verdict["url"]) in pages and verdict["published"]:
            verdict["date_source"] = "page"
            stats["dropped"] += 1 if verdict["in_window"] is False else 0
    return revised, again, stats
