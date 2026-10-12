"""What a site publishes about itself: feeds and sitemaps.

Watching a site for new pages by searching it or re-reading its front page
is the expensive way. Most sites already list their new pages: RSS, Atom and
JSON feeds advertised with <link rel="alternate">, and sitemaps (named in
robots.txt) with last-modified dates, including news sitemaps. This module
finds and parses those; Runtime.watch_sites polls them with conditional
requests, so an unchanged feed costs one small request.

Web standards and common paths only; nothing here knows a site.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlparse

from .search import parse_date

FEED_TYPES = {"application/rss+xml", "application/atom+xml", "application/feed+json", "application/json",
              "application/rdf+xml", "text/xml", "application/xml"}
# Paths many sites serve feeds at without advertising them; tried only when none is advertised.
CONVENTIONAL_FEEDS = ("/feed", "/rss", "/rss.xml", "/feed.xml", "/atom.xml", "/index.xml")
_ATOM = "{http://www.w3.org/2005/Atom}"
_SITEMAP = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
_NEWS = "{http://www.google.com/schemas/sitemap-news/0.9}"
_MAX_ENTRIES = 2000


def find_feeds(html: str, base: str) -> list[str]:
    """Feed URLs a page advertises in <link rel="alternate">."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup((html or "")[:2_000_000], "html.parser")
    found = []
    for link in soup.find_all("link", href=True):
        rel = " ".join(link.get("rel") or []).lower()
        kind = (link.get("type") or "").lower().split(";")[0].strip()
        if "alternate" in rel and kind in FEED_TYPES and kind not in ("application/json",):
            url = urljoin(base, link["href"])
            if urlparse(url).scheme in ("http", "https") and url not in found:
                found.append(url)
        elif "alternate" in rel and kind == "application/json" and "feed" in (link.get("title") or "").lower():
            found.append(urljoin(base, link["href"]))
    return found[:10]


def robots_sitemaps(text: str, base: str) -> list[str]:
    found = []
    for line in (text or "").splitlines():
        if line.lower().startswith("sitemap:"):
            url = urljoin(base, line.split(":", 1)[1].strip())
            if urlparse(url).scheme in ("http", "https") and url not in found:
                found.append(url)
    return found[:20]


def _iso(value):
    when = parse_date(value)
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if when else None


def _summarised(entry, text):
    """Add the feed's own summary (markup stripped, bounded) when it has one."""
    if isinstance(text, str) and text.strip():
        from bs4 import BeautifulSoup
        plain = " ".join(BeautifulSoup(text[:20_000], "html.parser").get_text(" ").split())
        if plain:
            entry["summary"] = plain[:500]
    return entry


def parse_feed(content: str) -> list[dict] | None:
    """Entries of an RSS, RDF, Atom or JSON feed: url, title, published. None if
    the content is not a feed."""
    text = (content or "").lstrip("﻿ \t\r\n")
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            return None
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            return None
        return [_summarised({"url": item.get("url") or item.get("external_url"), "title": item.get("title") or "",
                             "published": _iso(item.get("date_published") or item.get("date_modified"))},
                            item.get("summary") or item.get("content_text") or item.get("content_html"))
                for item in data["items"][:_MAX_ENTRIES] if isinstance(item, dict)]
    try:
        root = ET.fromstring(text.encode() if isinstance(text, str) else text)
    except ET.ParseError:
        return None
    entries = []
    if root.tag == _ATOM + "feed":
        for entry in root.findall(_ATOM + "entry")[:_MAX_ENTRIES]:
            link = next((node.get("href") for node in entry.findall(_ATOM + "link")
                         if node.get("rel", "alternate") == "alternate"), None)
            entries.append(_summarised(
                {"url": link, "title": (entry.findtext(_ATOM + "title") or "").strip(),
                 "published": _iso(entry.findtext(_ATOM + "published") or entry.findtext(_ATOM + "updated"))},
                entry.findtext(_ATOM + "summary") or entry.findtext(_ATOM + "content")))
        return entries
    items = root.findall("./channel/item") or [node for node in root if node.tag.endswith("}item")]
    if root.tag != "rss" and not root.tag.endswith("RDF"):
        return None
    for item in items[:_MAX_ENTRIES]:
        link = item.findtext("link") or next((node.text for node in item if node.tag.endswith("}link")), None)
        guid = item.findtext("guid")
        if not link and guid and guid.startswith("http"):
            link = guid
        date = item.findtext("pubDate") or next((node.text for node in item if node.tag.endswith("}date")), None)
        body = item.findtext("description") or next(
            (node.text for node in item if node.tag.endswith("}encoded")), None)
        entries.append(_summarised({"url": (link or "").strip(), "title": (item.findtext("title") or "").strip(),
                                    "published": _iso(date)}, body))
    return entries


def parse_sitemap(content: str) -> dict | None:
    """{"pages": [{url, published, title}], "sitemaps": [{url, published}]} or None."""
    try:
        root = ET.fromstring((content or "").lstrip("﻿ \t\r\n").encode())
    except ET.ParseError:
        return None
    if root.tag == _SITEMAP + "sitemapindex":
        return {"pages": [], "sitemaps": [{"url": (node.findtext(_SITEMAP + "loc") or "").strip(),
                                           "published": _iso(node.findtext(_SITEMAP + "lastmod"))}
                                          for node in root.findall(_SITEMAP + "sitemap")[:5000]]}
    if root.tag != _SITEMAP + "urlset":
        return None
    pages = []
    for node in root.findall(_SITEMAP + "url")[:50_000]:
        news = node.find(_NEWS + "news")
        published = (news.findtext(_NEWS + "publication_date") if news is not None else None) \
            or node.findtext(_SITEMAP + "lastmod")
        pages.append({"url": (node.findtext(_SITEMAP + "loc") or "").strip(),
                      "title": ((news.findtext(_NEWS + "title") if news is not None else None) or "").strip(),
                      "published": _iso(published)})
    return {"pages": pages, "sitemaps": []}


def pick_child_sitemaps(children: list[dict], limit: int = 3) -> list[str]:
    """The child sitemaps most likely to hold new pages: news ones, then the
    most recently modified, then names with the latest dates or numbers."""
    def rank(child):
        url = child["url"].lower()
        news = 1 if re.search(r"news|post|article|blog|latest|recent", url) else 0
        dates = re.findall(r"(20\d\d)[-_/]?(\d\d)?", url)
        newest = max(("".join(parts) for parts in dates), default="")
        return (news, child.get("published") or "", newest)
    return [child["url"] for child in sorted(children, key=rank, reverse=True)[:limit] if child.get("url")]


_TRACKING = re.compile(r"(?i)^(utm_|fbclid|gclid|ref$|ref_|mc_|cmpid|ocid|taid)")


def page_key(url: str) -> str:
    """One key per page: scheme, www., trailing slash, fragments and tracking
    parameters don't make a different page."""
    parsed = urlparse(url)
    query = "&".join(sorted(part for part in parsed.query.split("&")
                            if part and not _TRACKING.match(part.split("=", 1)[0])))
    return ((parsed.hostname or "").lower().removeprefix("www.") + parsed.path.rstrip("/")
            + ("?" + query if query else ""))


def story_key(title: str) -> str | None:
    """The same story syndicated under two URLs has the same title words."""
    words = re.findall(r"[^\W_]+", (title or "").lower())
    return " ".join(words[:12]) if len(words) >= 4 else None


def newer_than(entry: dict, since: datetime | None) -> bool:
    if since is None:
        return True
    when = parse_date(entry.get("published"))
    return when is None or when >= since
