"""Is this the page, or only its frame?

A read can succeed with HTTP 200 and still miss its content: a search page
whose results load later, a listing page that rendered only its header. Core
checks a page's structure, never what the caller asked for, and escalates to a
stronger provider when the structure is missing.

- Search and category pages (a query parameter, or a path such as /search or
  /s/) need at least 10 distinct same-site item links (product, listing or
  detail URLs; never assets or site-wide service pages) or 4 prices. Menus
  carry a handful of such links; result lists carry many.
- Item pages (a path with a long id or a long slug) need a price, or enough text.
- Other pages need enough text.

A search page can pass on structure and still be the wrong page: a site that
ignores an unknown query parameter shows its default feed. When the query is
known (a query parameter, or the caller's expect_terms), a structurally
complete search page whose items and text never mention it is "off query".
A stronger tool cannot fix a wrong URL, so that page is flagged, not escalated.
"""
from __future__ import annotations

import html as html_lib
import re
from urllib.parse import parse_qs, unquote, urljoin, urlparse

QUERY_KEYS = frozenset({"q", "query", "k", "s", "st", "ss", "search", "searchterm", "keyword",
                        "keywords", "text", "term", "searchtext", "find_desc", "d", "field-keywords",
                        "kw", "_nkw", "search_string", "search_key", "search_query", "searchquery",
                        "words", "freetext", "sw", "ntt", "qs", "wd", "searchkeyword", "searchkeywords"})


def _query_key(key: str) -> bool:
    """A search-box parameter, however the site spells it (search_term, searchTerm)."""
    lowered = key.lower()
    return lowered in QUERY_KEYS or re.sub(r"[^a-z0-9]", "", lowered) in QUERY_KEYS
_SEARCH_PATH = re.compile(r"/(search|s|shop|catalogsearch|browse|category|categories|c|list|"
                          r"jobs|homes|for_sale|sale|buy|rent|pdsearch|keyword\.php|w|p/pl)(/|$|\.|\?)", re.I)
# A marker segment followed by the item itself (/p/<slug>, /rooms/<id>).
_ITEM_MARKER = re.compile(r"/(p|dp|ip|item|itm|items|product|products|listing|listings|rooms|"
                          r"property|properties|job|jobs|viewjob|questions|ad|ads|deal)/[^/]+", re.I)
# Links that are never results: assets, and site-wide service and legal pages.
_NOT_ITEM = re.compile(r"\.(css|js|mjs|json|xml|pdf|png|jpe?g|gif|svg|webp|ico|woff2?|ttf)$|"
                       r"(privacy|policy|policies|terms|conditions|statement|legal|cookie|"
                       r"customer-service|customer-care|help|faq|support|careers|about|ethics|"
                       r"disclosure|accessibility|sitemap|store-locator|stores|gift-card|login|"
                       r"sign-?in|register|account|contact|returns|shipping|delivery)", re.I)
_LONG_ID = re.compile(r"\d{5,}|[A-Z0-9]{8,}")
# A currency before the amount ($49, € 12), or after it as most of Europe
# writes it (1.599 €, 249,00 kr, 99 zł).
_PRICE = re.compile(r"(?:A\$|AU\$|US\$|C\$|NZ\$|\$|€|£)\s?\d[\d,]*(?:\.\d{2})?"
                    r"|(?<![\w.,])\d{1,3}(?:[.\s ]\d{3})*(?:,\d{2}|,-)?\s?(?:€|EUR|kr|zł|Kč|Ft|CHF|lei|лв)(?!\w)")
_HREF = re.compile(r"""<a\b[^>]*?\bhref\s*=\s*["']([^"'#]+)""", re.I)
_MARKDOWN_LINK = re.compile(r"\]\((https?://[^)\s#]+|/[^)\s#]*)")
# An anchor with its attributes and inner HTML, and a markdown link with its text.
_ANCHOR = re.compile(r"""<a\b([^>]*?)\bhref\s*=\s*["']([^"'#]+)["']([^>]*)>(.*?)</a\s*>""", re.I | re.S)
_MARKDOWN_ANCHOR = re.compile(r"\[([^\]]{0,300})\]\((https?://[^)\s#]+|/[^)\s#]*)")
_TAG = re.compile(r"<[^>]+>")
_LABEL = re.compile(r"""\b(?:title|aria-label|alt)\s*=\s*["']([^"']*)["']""", re.I)
_WORD = re.compile(r"[^\W_]+")
_STOPWORDS = frozenset({"the", "and", "for", "with", "from", "new", "used", "buy", "sale", "cheap",
                        "best", "near", "all", "any", "www", "com", "html"})
# At most this many query mentions in the text of an off-query page: the
# search box, the title and "results for ..." echo the query even when the
# results don't.
_ECHO_MENTIONS = 2


# Words a search path uses for the kind of page, not for what was searched.
_PATH_GENERIC = frozenset({"search", "s", "q", "jobs", "job", "results", "result", "shop", "browse", "category",
                           "categories", "c", "list", "html", "htm", "php", "aspx", "for", "sale", "buy", "rent",
                           "homes", "products", "product", "items", "all", "new", "used", "in", "near", "w"})


# Subdomains that hold a site's services, not its results.
_SERVICE_HOSTS = frozenset({"help", "support", "blog", "blogs", "careers", "jobs", "account", "accounts", "login",
                            "auth", "signin", "investors", "investor", "ir", "press", "news", "status", "docs",
                            "developer", "developers", "community", "forum", "forums", "about", "corporate",
                            "legal", "privacy", "m", "mobile", "cdn", "static", "assets", "img", "images", "media"})


def _same_site(link_host: str | None, base_host: str | None) -> bool:
    """A link on the page's own host or one of its subdomains: results that live on
    artist.bandcamp.com or name.substack.com are that page's results. Never a
    sibling host, and never a service subdomain (help., blog., careers., ...)."""
    link = (link_host or "").lower().removeprefix("www.")
    base = (base_host or "").lower().removeprefix("www.")
    if not link or not base:
        return False
    if link == base:
        return True
    return link.endswith("." + base) and link[:-len(base) - 1].split(".")[-1] not in _SERVICE_HOSTS


# Path words that name a kind of page or a locale, not what the page is about.
_URL_FILLER = frozenset({"html", "htm", "php", "aspx", "jsp", "index", "default", "item", "items", "product",
                         "products", "detail", "details", "page", "pages", "www", "en", "us", "au", "uk",
                         "gb", "ca", "nz", "de", "fr", "es", "it", "nl", "dp", "view", "listing", "listings"})


def url_words(url: str) -> str:
    """What a URL asks for, in plain words: its search query, or the words of its
    last meaningful path segment (/p/brass-desk-lamp-123 is "brass desk lamp")."""
    parsed = urlparse(url)
    values = [value for key, items in parse_qs(parsed.query).items() if _query_key(key) for value in items]
    if not values:
        for segment in reversed([unquote(part) for part in parsed.path.split("/") if part]):
            words = [word for word in _WORD.findall(segment.lower())
                     if len(word) >= 3 and not word.isdigit() and word not in _PATH_GENERIC
                     and word not in _URL_FILLER and word not in _STOPWORDS]
            if words:
                values = [" ".join(words[:6])]
                break
    return " ".join(" ".join(values).split())[:80]


def query_terms(url: str, expect_terms=()) -> list[str]:
    """Words the results of this search should mention, lightly stemmed."""
    values = [value for key, items in parse_qs(urlparse(url).query).items()
              if _query_key(key) for value in items]
    if not values and _SEARCH_PATH.search(urlparse(url).path or "/"):
        # /jobs/python, /q/fiets, /python-jobs: the last path segment is the query.
        last = [unquote(segment) for segment in urlparse(url).path.split("/") if segment][-1:]
        words = [word for word in _WORD.findall(last[0].lower()) if word not in _PATH_GENERIC] if last else []
        values = [" ".join(words)] if words and not re.search(r"\d{4,}", last[0]) else []
    terms = []
    for value in [*values, *expect_terms]:
        for word in _WORD.findall(str(value).lower()):
            if len(word) < 3 or word in _STOPWORDS or word.isdigit():
                continue
            if word.endswith("es") and len(word) >= 5:
                word = word[:-2]
            elif word.endswith("s") and len(word) >= 4:
                word = word[:-1]
            if word not in terms:
                terms.append(word)
    return terms[:10]


def _item_blobs(html: str, base: str) -> dict[str, str]:
    """Same-site item links, each with the lowercased text that describes it."""
    host = (urlparse(base).hostname or "").removeprefix("www.")
    base_path = urlparse(base).path
    blobs: dict[str, str] = {}
    pairs = [(href, before + " " + after + " " + inner)
             for before, href, after, inner in _ANCHOR.findall(html[:4_000_000])]
    pairs += [(href, text) for text, href in _MARKDOWN_ANCHOR.findall(html[:4_000_000])]
    for href, raw in pairs:
        parsed = urlparse(urljoin(base, href.replace("&amp;", "&")))
        if parsed.scheme not in ("http", "https"):
            continue
        link_host = (parsed.hostname or "").removeprefix("www.")
        if not _same_site(link_host, host) or parsed.path == base_path:
            continue
        if not _item_like(parsed.path):
            continue
        labels = " ".join(_LABEL.findall(raw))
        text = html_lib.unescape(_TAG.sub(" ", raw) + " " + labels + " " + parsed.path)
        key = parsed.path.rstrip("/")
        blobs[key] = (blobs.get(key, "") + " " + text.lower())[:2000]
    return blobs


def relevance(url: str, result: dict, expect_terms=()) -> dict | None:
    """How many result items and text mentions match the query, when it is known."""
    terms = query_terms(url, expect_terms)
    if not terms:
        return None
    content = result.get("content") or ""
    text = (result.get("text") or "").lower()
    blobs = _item_blobs(content, result.get("url") or url)
    relevant = sum(1 for blob in blobs.values() if any(term in blob for term in terms))
    mentions = sum(text.count(term) for term in terms)
    return {"terms": terms, "relevant_items": relevant, "mentions": mentions,
            # No mention anywhere in the text and at most one matching link (often
            # a menu entry) is a default feed too.
            "off_query": (relevant == 0 and mentions <= _ECHO_MENTIONS) or (mentions == 0 and relevant <= 1)}


def page_kind(url: str) -> str:
    parsed = urlparse(url)
    params = {key.lower() for key in parse_qs(parsed.query)}
    if any(_query_key(key) for key in params) or _SEARCH_PATH.search(parsed.path or "/"):
        return "search"
    if _ITEM_MARKER.search(parsed.path) or any(
            _LONG_ID.search(segment) or segment.count("-") >= 3
            for segment in parsed.path.split("/") if segment):
        return "item"
    return "page"


def _item_like(path: str) -> bool:
    if _NOT_ITEM.search(path):
        return False
    if _ITEM_MARKER.search(path):
        return True
    return any(_LONG_ID.search(segment) or (segment.count("-") >= 3 and len(segment) >= 20)
               for segment in path.split("/") if segment)


def result_group(html: str, base: str, terms) -> int:
    """Links that are results by shape: many distinct same-site links under one
    path prefix (/packages/<name>, /project/<name>), at least a third of whose
    link texts mention the query. A menu shares a prefix too, but not the query."""
    if not terms:
        return 0
    host = (urlparse(base).hostname or "").removeprefix("www.")
    base_path = urlparse(base).path.rstrip("/")
    groups: dict[str, dict[str, str]] = {}
    pairs = [(href, inner) for _before, href, _after, inner in _ANCHOR.findall(html[:4_000_000])]
    pairs += [(href, text) for text, href in _MARKDOWN_ANCHOR.findall(html[:4_000_000])]
    for href, inner in pairs:
        parsed = urlparse(urljoin(base, href.replace("&amp;", "&")))
        link_host = (parsed.hostname or "").removeprefix("www.")
        if parsed.scheme not in ("http", "https") or not _same_site(link_host, host):
            continue
        path = parsed.path.rstrip("/")
        if not path or path == base_path or _NOT_ITEM.search(path) or "/" not in path.strip("/"):
            continue
        prefix, leaf = path.rsplit("/", 1)
        text = html_lib.unescape(_TAG.sub(" ", inner) + " " + leaf).lower()
        # Results on subdomains share a path prefix (/p/<post>) across hosts.
        groups.setdefault(prefix, {})[leaf] = text
    best = 0
    for leaves in groups.values():
        if len(leaves) < 8:
            continue
        matching = sum(1 for text in leaves.values() if any(term in text for term in terms))
        if matching >= max(3, len(leaves) / 3):
            best = max(best, len(leaves))
    return best


def item_links(html: str, base: str) -> int:
    """Distinct same-site links that look like items (products, listings, detail pages)."""
    host = (urlparse(base).hostname or "").removeprefix("www.")
    base_path = urlparse(base).path
    found = set()
    for href in _HREF.findall(html[:4_000_000]) + _MARKDOWN_LINK.findall(html[:4_000_000]):
        absolute = urljoin(base, href.replace("&amp;", "&"))
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https"):
            continue
        link_host = (parsed.hostname or "").removeprefix("www.")
        if not _same_site(link_host, host) or parsed.path == base_path:
            continue
        if _item_like(parsed.path):
            found.add(parsed.path.rstrip("/"))
    return len(found)


_ZERO_PRICE = re.compile(r"^\D*0+(?:[.,]0+|,-)?\D*$")
# Template syntax and script values that leaked into the text before the page rendered.
_UNRENDERED = re.compile(r"\{\{\s*[\w.$]+\s*\}\}|\$\{\s*[\w.]+\s*\}|\b(?:NaN|undefined)\b|\[object Object\]")


_INTERACTION_GATE = re.compile(
    r"(?i)\b(?:select|choose|pick|enter|add)\b[^.!?]{0,80}?\bto (?:see|view|get|show|check)\b"
    r"[^.!?]{0,30}?\b(?:prices?|pricing|rates?|fares?|availability|cost|quotes?)\b")


def _placeholder(text: str, zero_prices: int, prices: int) -> str | None:
    """Why the text is an unrendered placeholder, or None."""
    if zero_prices and not prices:
        return f"{zero_prices} price(s) of 0 and no real price: the prices have not loaded"
    # Count outside links and URLs (ad URLs carry "undefined" legitimately), and
    # relative to the page: three in a short page is a template, not in a long one.
    prose = re.sub(r"\]\([^)]*\)|https?://\S+", " ", text[:200_000])
    leaks = len(_UNRENDERED.findall(prose))
    if leaks >= 3 and leaks * 4000 >= len(prose):
        return f"{leaks} unrendered template values ({{{{ }}}}, NaN, undefined) in the text"
    return None


def _link_text_share(content: str, text: str) -> float:
    """How much of the page's text sits inside links: near 1 for a page that
    is only menus and footers."""
    if not text:
        return 0.0
    linked = 0
    for found in _ANCHOR.finditer(content[:2_000_000]):
        linked += len(" ".join(_TAG.sub(" ", found.group(4)).split()))
    return min(linked / max(len(" ".join(text.split())), 1), 1.0)


# Article conventions: schema.org types, Open Graph, and the paths publishers use.
_ARTICLE_TYPES = {"Article", "NewsArticle", "BlogPosting", "Report", "ScholarlyArticle", "AnalysisNewsArticle",
                  "OpinionNewsArticle", "ReviewNewsArticle", "TechArticle", "LiveBlogPosting"}
_ARTICLE_PATH = re.compile(r"(?i)/(news|article|articles|story|stories|blog|blogs|post|posts|opinion|"
                           r"insights?|press|media-releases?|press-releases?)/|/20\d\d/\d{1,2}/")
_OG_ARTICLE = re.compile(r"""<meta[^>]+property=["']og:type["'][^>]+content=["']article""", re.I)
MIN_ARTICLE_CHARS = 600


def article_like(url: str, result: dict) -> str | None:
    """How a page says it holds an article: "markup" (schema.org or Open Graph),
    "path" (a publisher-style URL), or None."""
    structured = result.get("structured") if isinstance(result.get("structured"), dict) else {}
    for block in structured.get("jsonld") or ():
        for node in (block.get("@graph") or [block]) if isinstance(block, dict) else block if isinstance(block, list) else ():
            kinds = node.get("@type") if isinstance(node, dict) else None
            kinds = {kinds} if isinstance(kinds, str) else set(kinds) if isinstance(kinds, list) else set()
            if kinds & _ARTICLE_TYPES:
                return "markup"
    if _OG_ARTICLE.search((result.get("content") or "")[:200_000]):
        return "markup"
    path = urlparse(url).path or ""
    last = [segment for segment in path.split("/") if segment][-1:] or [""]
    # The path alone counts only with a story-like last segment: /news/ itself is a section.
    if _ARTICLE_PATH.search(path) and (last[0].count("-") >= 2 or re.search(r"\d{5,}", last[0]) is not None):
        return "path"
    return None


# A teaser in front of a subscription: schema.org's own flag, or the usual words.
_PAYWALL = re.compile(
    r"(?i)(unlock (this|the) (story|article)|subscribe (now )?to (continue|keep) reading|to (continue|keep) "
    r"reading,? (please )?(subscribe|sign in|log in|register)|already a subscriber|this (article|story|content) "
    r"is (only )?(available )?(to|for) (subscribers|members)|subscribers? only|create a free account to (continue|read))")
_NOT_FREE = re.compile(r"""["']isAccessibleForFree["']\s*:\s*["']?(false|False)""")


def paywalled(result: dict) -> bool:
    content = (result.get("content") or "")[:400_000]
    return bool(_NOT_FREE.search(content) or _PAYWALL.search(result.get("text") or ""))


def assess(url: str, result: dict, *, min_items: int = 10, min_prices: int = 4,
           min_text_chars: int = 1500, expect_terms=()) -> dict:
    """Score a page and say whether it looks complete for its kind of URL."""
    content = result.get("content") or ""
    text = (result.get("text") or "").strip()
    content_type = result.get("content_type") or ""
    kind = page_kind(url)
    if "html" not in content_type and "markdown" not in content_type:
        return {"kind": kind, "complete": True, "score": len(text), "reason": "not a document page"}
    items = item_links(content, result.get("url") or url)
    if kind == "search" and items < min_items:
        items = max(items, result_group(content, result.get("url") or url, query_terms(url, expect_terms)))
    found_prices = _PRICE.findall(text)
    # "$0" and "$ 0.00" are a price that has not loaded yet, not a price.
    prices = sum(1 for price in found_prices if not _ZERO_PRICE.search(price))
    score = items * 100 + prices * 20 + min(len(text), 50_000) / 10
    placeholder = _placeholder(text, len(found_prices) - prices, prices)
    if not placeholder:
        from .main_content import consent_only
        if consent_only(text):
            placeholder = "only a cookie or consent notice, not the page"
    if placeholder:
        return {"kind": kind, "complete": False, "score": score, "item_links": items, "prices": prices,
                "text_chars": len(text), "reason": placeholder, "placeholder": True}
    if kind != "search" and prices == 0 and "html" in content_type:
        share = _link_text_share(content, text)
        if share > 0.8 and len(text) >= 400:
            return {"kind": kind, "complete": False, "score": score, "item_links": items, "prices": prices,
                    "text_chars": len(text), "link_text_share": round(share, 2),
                    "reason": f"{round(share * 100)}% of the text is links: menus, not the page"}
    article = None
    # Markup that says "article" is trusted whatever numbers the story quotes;
    # a path alone only counts on a page without prices (product slugs look alike).
    how = article_like(url, result) if kind != "search" else None
    if how == "markup" or (how == "path" and prices == 0):
        # An article page is complete when its article is there, however long
        # the menus around it: judge the extracted main text, not the page.
        from .main_content import main_content, paragraphs
        found = main_content(content, content_type, result.get("url") or url) or {}
        article = {"chars": found.get("chars", 0), "paragraphs": paragraphs(found.get("text") or ""),
                   "method": found.get("method")}
        if article["chars"] < 1500 and paywalled(result):
            article["paywall"] = True
        if article["chars"] < MIN_ARTICLE_CHARS or article["paragraphs"] < 2:
            return {"kind": "article", "complete": False, "score": score + article["chars"], "item_links": items,
                    "prices": prices, "text_chars": len(text), "article": article,
                    **({"paywall": True} if article.get("paywall") else {}),
                    "reason": (f"paywalled: only {article['chars']} characters of the article are free"
                               if article.get("paywall") else
                               f"article page with {article['chars']} characters of article "
                               f"({article['paragraphs']} paragraphs) in {len(text)} of text")}
    if article is not None:
        complete, reason = True, None
        score += article["chars"]
    elif kind == "search":
        complete = items >= min_items or prices >= min_prices
        reason = None if complete else f"search page with {items} item links and {prices} prices"
    elif kind == "item":
        complete = prices >= 1 or len(text) >= min_text_chars
        reason = None if complete else f"item page with no price and {len(text)} characters"
    else:
        complete = len(text) >= min_text_chars or (len(text) >= 400 and "<script" not in content)
        reason = None if complete else f"page with {len(text)} characters of text"
    verdict = {"kind": "article" if article is not None else kind, "complete": complete, "score": score,
               "item_links": items, "prices": prices, "text_chars": len(text), "reason": reason}
    if article is not None:
        verdict["article"] = article
        if article.get("paywall"):
            verdict["paywall"] = True
    if not prices:
        gate = _INTERACTION_GATE.search(text[:500_000])
        if gate:
            # Real page, but its prices wait for a choice (guests, dates, a
            # postcode): no stronger read tool will show them.
            verdict["needs_interaction"] = " ".join(gate.group(0).split())[:160]
    # Only a page that looks like results can be the wrong results; an
    # incomplete one may still be loading them, so it escalates as before.
    if kind == "search" and complete:
        match = relevance(url, result, expect_terms)
        if match is not None:
            verdict["query"] = match
            if match["off_query"]:
                verdict.update(complete=False, off_query=True, reason=(
                    "results don't mention the query (" + ", ".join(match["terms"]) + ")"))
    return verdict


def quality(verdict: dict | None, receipt: dict | None = None) -> dict:
    """A short grade for any read: good (the content is there), partial (there,
    with warnings) or poor (a placeholder, a wall, the wrong results, menus)."""
    verdict = verdict or {}
    flags = [name for name in ("placeholder", "off_query", "cookie_notice", "paywall") if verdict.get(name)]
    if verdict.get("link_text_share"):
        flags.append("menus")
    if verdict.get("needs_interaction"):
        flags.append("needs_interaction")
    if (receipt or {}).get("archived"):
        flags.append("archived")
    if (receipt or {}).get("rescued"):
        flags.append("rescued")
    if not verdict.get("complete", True) or {"placeholder", "off_query", "cookie_notice", "menus"} & set(flags):
        grade = "poor"
    elif flags:
        grade = "partial"
    else:
        grade = "good"
    out = {"grade": grade, "kind": verdict.get("kind"), "flags": flags}
    for key in ("reason", "article", "item_links", "prices", "text_chars"):
        if verdict.get(key) is not None:
            out[key] = verdict[key]
    return out


_SRCSET_URL = re.compile(r"\s*([^\s,]+)(?:\s+[\d.]+[wx])?\s*(?:,|$)")
_BACKGROUND = re.compile(r"""background(?:-image)?\s*:[^;]*url\(\s*["']?([^"')]+)""", re.I)
_IMAGE_ATTRS = ("data-src", "data-lazy-src", "data-original", "data-lazy", "src")
# Distinct item links scanned for cards: menus come first in most pages, so
# the scan reads well past them before ordering and trimming.
_CARD_SCAN_MAX = 2000
# Site chrome, never an item's thumbnail.
_CHROME_IMAGE = re.compile(r"(^|[/_.-])(logo|logos|icon|icons|sprite|favicon|placeholder|spacer)([/_.-]|$)", re.I)


def _image_in(node, base: str) -> str | None:
    """The best image URL inside a node: lazy-load attributes first, then the
    largest srcset entry, then src, then a CSS background image."""
    candidates = []
    for element in node.find_all(["img", "source"]) if hasattr(node, "find_all") else ():
        for attribute in ("data-srcset", "srcset"):
            entries = _SRCSET_URL.findall(element.get(attribute) or "")
            if entries:
                candidates.append(entries[-1])
        candidates += [element.get(attribute) for attribute in _IMAGE_ATTRS]
    elements = [node, *node.find_all(style=True)] if hasattr(node, "find_all") else [node]
    for element in elements:
        found = _BACKGROUND.search(element.get("style") or "")
        if found:
            candidates.append(found.group(1))
    for candidate in candidates:
        if not candidate or candidate.startswith("data:") or _CHROME_IMAGE.search(candidate):
            continue
        absolute = urljoin(base, candidate.strip())
        if urlparse(absolute).scheme in ("http", "https"):
            return absolute
    return None


def _item_list_cards(structured, base: str) -> list[dict]:
    """Cards from a JSON-LD ItemList: url, name and image where the site gives them."""
    found = []

    def image_of(value):
        if isinstance(value, list):
            value = value[0] if value else None
        if isinstance(value, dict):
            value = value.get("url") or value.get("contentUrl")
        return urljoin(base, value) if isinstance(value, str) and value else None

    def visit(node):
        if isinstance(node, dict):
            if node.get("@type") == "ItemList" and isinstance(node.get("itemListElement"), list):
                for element in node["itemListElement"]:
                    if not isinstance(element, dict):
                        continue
                    item = element.get("item") if isinstance(element.get("item"), dict) else element
                    url = item.get("url") or item.get("@id") or element.get("url")
                    if isinstance(url, str) and urlparse(urljoin(base, url)).scheme in ("http", "https"):
                        found.append({"url": urljoin(base, url),
                                      "title": str(item.get("name") or element.get("name") or "")[:300],
                                      "image": image_of(item.get("image") or element.get("image"))})
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)
    visit(structured)
    return found


def cards(html: str, base: str, *, limit: int = 200, structured=None) -> list[dict]:
    """Result cards: each item link with its title and its own card image.

    The image is the one inside the link, or inside the nearest container that
    holds no other item link, so neighbouring cards never share a thumbnail.
    A JSON-LD ItemList's entries come first (some sites list results only
    there), then linked cards with an image, then the rest, so menu links never
    crowd out results. Pairing unlinked card images with ItemList entries by
    position is site knowledge, left to site modules.
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html[:4_000_000], "html.parser")
    host = (urlparse(base).hostname or "").removeprefix("www.")
    base_path = urlparse(base).path

    def item_path(href):
        parsed = urlparse(urljoin(base, href.replace("&amp;", "&")))
        if (parsed.scheme not in ("http", "https")
                or (parsed.hostname or "").removeprefix("www.") != host
                or parsed.path == base_path or not _item_like(parsed.path)):
            return None, None
        return parsed.path.rstrip("/"), parsed.geturl()

    found: dict[str, dict] = {}
    for anchor in soup.select("a[href]"):
        key, absolute = item_path(anchor["href"])
        if key is None:
            continue
        title = (anchor.get("aria-label") or anchor.get("title")
                 or anchor.get_text(" ", strip=True) or "")[:300]
        if key in found and found[key]["image"]:
            found[key]["title"] = found[key]["title"] or title
            continue
        image, node = _image_in(anchor, base), anchor
        for _ in range(4):
            if image:
                break
            parent = node.parent
            if parent is None or parent.name in ("body", "html", "[document]"):
                break
            others = {item_path(a["href"])[0] for a in parent.select("a[href]")} - {key, None}
            if others:
                break
            node = parent
            image = _image_in(node, base)
        image_tag = anchor.find("img")
        alt = ((image_tag.get("alt") if image_tag else "") or "")[:300]
        if key in found:
            found[key]["image"] = image
            found[key]["title"] = found[key]["title"] or title
            found[key]["alt"] = found[key]["alt"] or alt
        else:
            found[key] = {"url": absolute, "title": title, "image": image, "alt": alt}
        if len(found) >= _CARD_SCAN_MAX:
            break
    # A link's own text names the item best; an image's alt text is the fallback.
    linked = [{"url": card["url"], "title": card["title"] or card["alt"], "image": card["image"]}
              for card in found.values()]
    listed = _item_list_cards(structured, base) if structured else []
    by_url = {card["url"].rstrip("/"): card for card in linked}
    for card in listed:
        twin = by_url.get(card["url"].rstrip("/"))
        if twin:
            card["image"] = card["image"] or twin["image"]
            card["title"] = card["title"] or twin["title"]
    seen = {card["url"].rstrip("/") for card in listed}
    rest = [card for card in linked if card["url"].rstrip("/") not in seen]
    ordered = listed + [card for card in rest if card["image"]] + [card for card in rest if not card["image"]]
    return ordered[:limit]
