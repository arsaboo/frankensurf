"""Result checks for search: date window, entity relevance, page kind, dedupe."""
from datetime import datetime, timedelta, timezone

import pytest

from frankensurf.search import build_search, filter_results, normalize_options
from frankensurf.search_checks import (accepted, annotate, canonical_url, check_result, check_results,
                                       key_terms, merge_results, published, url_kind)

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


def item(url, title="", snippet="", date=None):
    return {"url": url, "title": title, "snippet": snippet, "indexed_date": date, "engines": ["fake"]}


# --- key terms ---------------------------------------------------------------

@pytest.mark.parametrize("query,expected", [
    ("latest news this week on Tesla earnings", ["tesla", "earning"]),
    ("how to fix a leaky tap", ["fix", "leaky", "tap"]),
    ("RTX 5090 review site:example.com -site:spam.com", ["rtx", "5090", "review"]),
    ("news 2026 today", []),
    ("the and for", []),
    ("kubernetes operators", ["kubernet", "operator"]),
])
def test_key_terms_drop_stopwords_freshness_words_operators_and_years(query, expected):
    assert key_terms(query) == expected


def test_key_terms_keep_short_non_ascii_words():
    assert key_terms("東京 天気") == ["東京", "天気"]


# --- dates -------------------------------------------------------------------

def test_published_prefers_source_date_then_snippet_then_url():
    assert published(item("https://a.test/2020/01/02/x", "t", "3 days ago", "2026-10-01T00:00:00Z"), NOW) == (
        datetime(2026, 10, 1, tzinfo=timezone.utc), "source")
    when, origin = published(item("https://a.test/2020/01/02/x", "t", "3 days ago - text"), NOW)
    assert origin == "snippet" and (NOW - when).days == 3
    when, origin = published(item("https://a.test/2026/10/05/story", "t", "no date here"), NOW)
    assert origin == "url" and (when.year, when.month, when.day) == (2026, 10, 5)
    assert published(item("https://a.test/story", "t", "no date"), NOW) == (None, None)


@pytest.mark.parametrize("snippet,day", [
    ("2026-10-09 - Something happened", 9),
    ("9 Oct 2026 - Something happened", 9),
    ("Oct 9, 2026 — Something happened", 9),
    ("October 9th, 2026 Something happened", 9),
    ("Oct 9 - Something happened", 9),
])
def test_published_reads_absolute_dates_the_snippet_leads_with(snippet, day):
    when, origin = published(item("https://a.test/x", "", snippet), NOW)
    assert (when.year, when.month, when.day, origin) == (2026, 10, day, "snippet")


@pytest.mark.parametrize("snippet,days", [
    ("3 days ago - text", 3), ("a week ago - text", 7), ("2 months ago", 60), ("1 year ago", 365),
    ("yesterday - text", 1),
])
def test_published_reads_relative_dates(snippet, days):
    when, _ = published(item("https://a.test/x", "", snippet), NOW)
    assert (NOW - when).days == days


def test_published_ignores_dates_buried_in_the_text_and_future_source_dates():
    buried = "A long introduction that goes on and on before it finally mentions 1 Jan 2019 somewhere"
    assert published(item("https://a.test/x", "", buried), NOW) == (None, None)
    future = item("https://a.test/x", "", "", "2030-01-01T00:00:00Z")
    assert published(future, NOW) == (None, None)


def test_window_fails_provably_old_results_and_leaves_unknown_dates_alone():
    results = [item("https://a.test/new", "rust release", "", "2026-10-10T00:00:00Z"),
               item("https://b.test/old", "rust release", "", "2026-08-01T00:00:00Z"),
               item("https://c.test/unknown", "rust release", "no date")]
    report = check_results("rust release", results, limit=10, recency="week", now=NOW)
    verdicts = {row["url"]: row for row in report["results"]}
    assert verdicts["https://a.test/new"]["in_window"] is True and verdicts["https://a.test/new"]["passed"]
    assert verdicts["https://b.test/old"]["in_window"] is False
    assert verdicts["https://b.test/old"]["reasons"] == ["out_of_window"]
    assert verdicts["https://c.test/unknown"]["in_window"] is None and verdicts["https://c.test/unknown"]["passed"]
    assert report["unknown_dates"] == 1 and report["windowed"] is True


def test_date_only_values_count_as_the_end_of_that_day():
    # Day window at noon: a result dated the previous calendar day could be 12h old.
    row = check_result(item("https://a.test/x", "rust", "", None) | {"snippet": "2026-10-11 - rust"},
                       ["rust"], since=NOW - timedelta(days=1), now=NOW)
    assert row["in_window"] is True
    row = check_result(item("https://a.test/x", "rust", "", None) | {"snippet": "2026-10-10 - rust"},
                       ["rust"], since=NOW - timedelta(days=1), now=NOW)
    assert row["in_window"] is False


def test_url_month_path_is_out_only_when_the_whole_month_is():
    recent = check_result(item("https://a.test/2026/10/story", "rust"), ["rust"], since=NOW - timedelta(days=7), now=NOW)
    old = check_result(item("https://a.test/2026/05/story", "rust"), ["rust"], since=NOW - timedelta(days=7), now=NOW)
    assert recent["in_window"] is True and old["in_window"] is False


def test_unknown_dates_rank_below_dated_in_window_results():
    results = [item("https://u.test/1", "rust news", "no date"),
               item("https://d.test/2", "rust news", "", "2026-10-11T00:00:00Z"),
               item("https://u.test/3", "rust news", "no date"),
               item("https://d.test/4", "rust news", "", "2026-10-12T01:00:00Z")]
    report = check_results("rust news", results, recency="week", now=NOW)
    assert [row["url"] for row in accepted(results, report)] == [
        "https://d.test/2", "https://d.test/4", "https://u.test/1", "https://u.test/3"]
    # With no window the order is the source's.
    plain = check_results("rust news", results, now=NOW)
    assert [row["url"] for row in accepted(results, plain)] == [row["url"] for row in results]


# --- relevance ---------------------------------------------------------------

def test_result_with_no_key_term_is_off_query():
    results = [item("https://a.test/x", "Celebrity gossip", "who wore what"),
               item("https://b.test/tesla-earnings", "Quarterly report", "numbers"),
               item("https://c.test/y", "Tesla earnings beat", "")]
    report = check_results("latest Tesla earnings news", results, now=NOW)
    assert [row["on_query"] for row in report["results"]] == [False, True, True]
    assert report["results"][0]["reasons"] == ["off_query"]
    assert report["reasons"] == {"off_query": 1}


def test_a_query_with_no_key_terms_cannot_be_off_query():
    report = check_results("news today", [item("https://a.test/x", "anything")], now=NOW)
    assert report["results"][0]["on_query"] is True


# --- page kinds --------------------------------------------------------------

@pytest.mark.parametrize("url,kind", [
    ("https://example.com", "homepage"),
    ("https://example.com/", "homepage"),
    ("https://example.com/en-au/", "homepage"),
    ("https://example.com/index.html", "homepage"),
    ("https://example.com/search?q=rust", "search_page"),
    ("https://example.com/?s=rust", "search_page"),
    ("https://example.com/results", "search_page"),
    ("https://example.com/tag/rust", "tag_listing"),
    ("https://example.com/category/news/", "tag_listing"),
    ("https://example.com/questions/tagged/rust", "tag_listing"),
    ("https://example.com/blog", "tag_listing"),
    ("https://example.com/?p=123", "item"),
    ("https://news.ycombinator.com/item?id=1", "item"),
    ("https://example.com/2026/10/12/rust-1-90-released", "item"),
    ("https://en.wikipedia.org/wiki/Rust_(programming_language)", "item"),
    ("https://github.com/rust-lang/rust", "item"),
])
def test_url_kind(url, kind):
    assert url_kind(url) == kind


def test_homepages_and_listings_fail_only_when_the_query_asks_for_specific_items():
    pages = [item("https://rust-lang.org/", "Rust language home"), item("https://x.test/tag/rust", "Rust tag")]
    specific = check_results("rust async runtime comparison", pages, now=NOW)
    assert [row["reasons"] for row in specific["results"]] == [["homepage"], ["tag_listing"]]
    single = check_results("rust", pages, now=NOW)
    assert [row["passed"] for row in single["results"]] == [True, True]
    # A search page is never an answer.
    search = check_results("rust", [item("https://x.test/search?q=rust", "rust")], now=NOW)
    assert search["results"][0]["reasons"] == ["search_page"]


@pytest.mark.parametrize("title", ["Just a moment...", "Attention Required! | Cloudflare",
                                   "Access Denied", "Are you a robot?", "Page not found", "Verify you are human"])
def test_challenge_titles_fail(title):
    row = check_results("rust release", [item("https://x.test/rust/release", title + " rust")], now=NOW)["results"][0]
    assert "challenge_page" in row["reasons"] and not row["passed"]


# --- dedupe ------------------------------------------------------------------

def test_canonical_url_ignores_scheme_www_fragment_slash_tracking_and_index():
    base = canonical_url("https://example.com/a/b?x=1")
    for variant in ("http://www.example.com/a/b/?x=1#top", "https://example.com/a/b?utm_source=n&x=1",
                    "https://EXAMPLE.com/a/b?x=1&fbclid=zzz", "https://example.com/a/b/index.html?x=1"):
        assert canonical_url(variant) == base
    assert canonical_url("https://example.com/a/b?x=2") != base


def test_duplicates_fail_after_the_first():
    results = [item("https://a.test/story", "rust story"), item("http://www.a.test/story/?utm_x=1", "rust story again"),
              item("https://b.test/story", "rust story")]
    report = check_results("rust story", results, now=NOW)
    assert [row["passed"] for row in report["results"]] == [True, False, True]
    assert report["results"][1]["reasons"] == ["duplicate"]


# --- verdict -----------------------------------------------------------------

def make(count, **kwargs):
    return [item("https://s%d.test/rust/%d" % (index, index), "rust article", **kwargs) for index in range(count)]


@pytest.mark.parametrize("count,limit,verdict,need", [
    (5, 10, "good", 3), (3, 10, "good", 3), (2, 10, "weak", 3), (1, 10, "weak", 3), (0, 10, "empty", 3),
    (1, 1, "good", 1), (2, 2, "good", 2), (1, 2, "weak", 2),
])
def test_set_verdict_needs_min_3_or_limit_passing(count, limit, verdict, need):
    report = check_results("rust article", make(count), limit=limit, now=NOW)
    assert (report["verdict"], report["need"]) == (verdict, need)


def test_failing_results_do_not_count_toward_the_set():
    results = make(2) + [item("https://z.test/x", "unrelated", "stuff")]
    report = check_results("rust article", results, limit=10, now=NOW)
    assert report["verdict"] == "weak" and report["passed"] == 2 and report["total"] == 3
    assert check_results("rust article", [item("https://z.test/x", "unrelated")], now=NOW)["verdict"] == "empty"


def test_annotate_and_accepted_carry_check_fields():
    results = make(2, date="2026-10-11T00:00:00Z") + [item("https://z.test/x", "unrelated")]
    report = check_results("rust article", results, recency="week", now=NOW)
    assert len(annotate(results, report)) == 3
    kept = accepted(results, report)
    assert len(kept) == 2
    assert kept[0]["check"] == {"passed": True, "reasons": [], "in_window": True, "on_query": True,
                                "published": "2026-10-11T00:00:00Z", "date_source": "source", "kind": "item"}


# --- merge -------------------------------------------------------------------

def test_merge_dedupes_and_fuses_by_reciprocal_rank():
    first = [item("https://a.test/1", "rust"), item("https://b.test/2", "rust")]
    second = [item("https://b.test/2?utm_x=1", "rust"), item("https://c.test/3", "rust")]
    merged = merge_results([("one", first), ("two", second)])
    assert [row["url"] for row in merged] == ["https://b.test/2", "https://a.test/1", "https://c.test/3"]
    assert merged[0]["fusion"]["sources"] == [{"source": "one", "rank": 2}, {"source": "two", "rank": 1}]
    assert len(merge_results([("one", first), ("two", second)], limit=2)) == 2


def test_merge_puts_dated_in_window_first_when_windowed():
    dated = {**item("https://d.test/1", "rust"), "check": {"in_window": True, "published": "2026-10-11T00:00:00Z"}}
    unknown = {**item("https://u.test/1", "rust"), "check": {"in_window": None, "published": None}}
    merged = merge_results([("one", [unknown]), ("two", [dated])], windowed=True)
    assert [row["url"] for row in merged] == ["https://d.test/1", "https://u.test/1"]


# --- exclusion ---------------------------------------------------------------

DOMAINS = ["site%02d.example" % index for index in range(36)]


def test_long_exclude_lists_send_only_a_few_operators():
    options = normalize_options(exclude_domains=DOMAINS)
    url, _ = build_search("rust async", "duckduckgo_html", {"exclude_domains": DOMAINS})
    from urllib.parse import parse_qs, urlparse
    sent = parse_qs(urlparse(url).query)["q"][0]
    assert sent.count("-site:") == 3 and sent.startswith("rust async")
    assert len(options["exclude_domains"]) == 36
    url, _ = build_search("rust async", "bing_rss", {"exclude_domains": DOMAINS, "max_exclude_operators": 1})
    assert parse_qs(urlparse(url).query)["q"][0].count("-site:") == 1
    url, _ = build_search("rust async", "searxng", {"exclude_domains": DOMAINS, "max_exclude_operators": 0})
    assert "-site:" not in parse_qs(urlparse(url).query)["q"][0]


@pytest.mark.parametrize("value", [-1, 21, "3", True])
def test_exclude_operator_cap_is_validated(value):
    with pytest.raises(ValueError):
        build_search("rust", "searxng", {"exclude_domains": ["a.example"], "max_exclude_operators": value})


def test_post_filter_enforces_the_whole_list_with_subdomain_matching():
    options = normalize_options(exclude_domains=DOMAINS)
    results = [item("https://site35.example/a"), item("https://news.site17.example/b"),
               item("https://www.site00.example/c"), item("https://deep.sub.site01.example/d"),
               item("https://notsite02.example/e"), item("https://site02.example.org/f"),
               item("https://keep.test/g")]
    kept, dropped = filter_results(results, options, NOW)
    assert [row["url"] for row in kept] == ["https://notsite02.example/e", "https://site02.example.org/f",
                                            "https://keep.test/g"]
    assert dropped["excluded"] == 4
