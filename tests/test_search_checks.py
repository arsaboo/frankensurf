"""Result checks for search: date window, entity relevance, page kind, dedupe."""
from datetime import datetime, timedelta, timezone

import pytest

from frankensurf.search import build_search, filter_results, normalize_options
from frankensurf.search_checks import (accepted, annotate, canonical_url, check_result, check_results,
                                       counted, extract_published, key_terms, merge_results, published, url_kind)

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


def item(url, title="", snippet="", date=None):
    return {"url": url, "title": title, "snippet": snippet, "published": date, "engines": ["fake"]}


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
    results = [item("https://a.test/newer-rust-release", "rust release", "", "2026-10-10T00:00:00Z"),
               item("https://b.test/old", "rust release", "", "2026-08-01T00:00:00Z"),
               item("https://c.test/unknown", "rust release", "no date")]
    report = check_results("rust release", results, limit=10, recency="week", now=NOW)
    verdicts = {row["url"]: row for row in report["results"]}
    assert verdicts["https://a.test/newer-rust-release"]["in_window"] is True and verdicts["https://a.test/newer-rust-release"]["passed"]
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


@pytest.mark.parametrize("url", [
    "https://aws.example/new/",
    "https://example.com/company/newsroom/",
    "https://example.com/company/newsroom/press-release-archive/",
    "https://dev.example.com/changelog/",
    "https://example.com/headlines/",
    "https://example.com/news-types/news/",
    "https://community.example.com/c/announcements/22",
    "https://example.com/quote/stock/ACME-123/news-press-releases/",
    "https://example.com/press/press-releases/",
    "https://example.com/en/whats-new",
    "https://example.com/docs/release-notes/",
    "https://example.com/blog/2026/",
    "https://example.com/events/2/",
    "https://aggregator.example/news/amd",
    "https://aggregator.example/news/acme-corp",
])
def test_url_kind_catches_index_pages_by_their_last_segment(url):
    assert url_kind(url) == "tag_listing"


@pytest.mark.parametrize("url", [
    "https://example.com/press/press-releases/2026/acme-announces-new-widget",
    "https://example.com/news/2026/09/launching-x/",
    "https://example.com/news/launching-meta-enterprise-platform/",
    "https://example.com/news/12345",
    "https://example.com/news/acme-launches-widget-2",
    "https://example.com/blog/how-we-built-it",
    "https://example.com/blog/2026/10/12/new-release",
    "https://example.com/c/announcements/22/release-of-widget",
    "https://example.com/newsletter-signup-guide",
    "https://example.com/events/2026-conference-recap",
])
def test_url_kind_leaves_real_items_alone(url):
    assert url_kind(url) == "item"


def test_listing_paths_only_fail_when_the_query_asks_for_specific_items():
    page = item("https://example.com/changelog/", "acme changelog")
    assert check_result(page, ["acme"], specific=True, now=NOW)["reasons"] == ["tag_listing"]
    assert check_result(page, ["acme"], specific=False, now=NOW)["passed"]


PAGE = """<html><head>%s</head><body>%s</body></html>"""


@pytest.mark.parametrize("head,body,origin", [
    ('<meta property="article:published_time" content="2026-10-01T08:00:00Z">', "", "meta:article:published_time"),
    ('<meta name="date" content="2026-10-01">', "", "meta:date"),
    ('<script type="application/ld+json">{"@graph":[{"@type":"NewsArticle","datePublished":"2026-10-01T08:00:00+00:00"}]}</script>',
     "", "jsonld"),
    ("", '<span itemprop="datePublished" content="2026-10-01"></span>', "itemprop"),
    ("", '<time datetime="2026-10-01T08:00:00Z">Oct 1</time>', "time"),
])
def test_extract_published_reads_page_metadata(head, body, origin):
    when, found = extract_published(PAGE % (head, body), NOW)
    assert (when.year, when.month, when.day) == (2026, 10, 1) and found == origin


def test_extract_published_ignores_modified_dates_the_future_and_non_pages():
    modified = PAGE % ('<meta property="article:modified_time" content="2026-10-01T08:00:00Z">'
                       '<script type="application/ld+json">{"dateModified": "2026-10-01"}</script>', "")
    assert extract_published(modified, NOW) == (None, None)
    future = PAGE % ('<meta property="article:published_time" content="2031-01-01T00:00:00Z">', "")
    assert extract_published(future, NOW) == (None, None)
    assert extract_published(None, NOW) == (None, None) and extract_published("plain text", NOW) == (None, None)
    assert extract_published(PAGE % ("<script type='application/ld+json'>{broken</script>", ""), NOW) == (None, None)


def test_undated_results_count_for_at_most_half_the_quota_when_weighed():
    assert counted(0, 5, 3, True) == 1 and counted(2, 5, 3, True) == 3 and counted(0, 5, 3, False) == 5
    undated = [item("https://s%d.test/rust/%d" % (index, index), "rust news") for index in range(5)]
    assert check_results("rust", undated, limit=10, recency="week", now=NOW)["verdict"] == "good"
    weighed = check_results("rust", undated, limit=10, recency="week", now=NOW, weigh_undated=True)
    assert weighed["verdict"] == "weak" and weighed["passed"] == 5 and weighed["counted"] == 1
    assert check_results("rust", undated, limit=10, now=NOW, weigh_undated=True)["verdict"] == "good"


def test_annotate_sets_published_from_a_snippet_or_url_date_only_for_windowed_checks():
    results = [item("https://a.test/2026/10/11/story", "rust"), item("https://b.test/x", "rust", "2 days ago - rust")]
    windowed = annotate(results, check_results("rust", results, limit=5, recency="week", now=NOW))
    assert [row["published"] for row in windowed] == ["2026-10-11T23:59:59Z", "2026-10-10T12:00:00Z"]
    assert [row["check"]["date_source"] for row in windowed] == ["url", "snippet"]
    plain = annotate(results, check_results("rust", results, limit=5, now=NOW))
    assert [row["published"] for row in plain] == [None, None]


# --- homepages, entities and whole-word matching ------------------------------

from frankensurf.search_checks import entities  # noqa: E402


def passes(query, row, **kwargs):
    return check_results(query, [row], now=NOW, **kwargs)["results"][0]


@pytest.mark.parametrize("url", [
    "https://www.nintendo.com/",
    "https://www.nintendo.com",
    "https://www.nintendo.com/en/",
    "https://www.nintendo.com/en-us/",
    "https://www.nintendo.com/en-gb/index.html",
    "https://www.nintendo.com/zh-hans/",
    "https://www.nintendo.com/us/",
    "https://www.nintendo.com/news/",
    "https://www.nintendo.com/en-us/news",
    "https://www.nintendo.com/about",
    "https://www.facebook.com/nintendo",
    "https://www.nintendo.com/en/nintendo",
])
def test_one_term_news_queries_reject_homepages_roots_and_section_indexes(url):
    row = item(url, "Nintendo")
    for query in ("Nintendo latest announcement", "Nintendo news", "Nintendo new releases"):
        verdict = passes(query, row)
        assert not verdict["passed"] and verdict["reasons"][0] in ("homepage", "tag_listing"), (query, verdict)


def test_a_recency_window_rejects_a_homepage_whatever_the_term_count():
    row = item("https://www.nintendo.com/", "Nintendo")
    assert passes("Nintendo", row, recency="week")["reasons"] == ["homepage"]
    both = item("https://www.nintendo.com/en/", "Nintendo Switch")
    assert passes("Nintendo Switch", both, recency="month")["reasons"] == ["homepage"]


def test_news_words_make_any_query_specific_even_without_a_window():
    for query in ("Nintendo announcements", "OpenAI papers", "Mozilla posts", "Meta releases"):
        row = item("https://%s.test/" % query.split()[0].lower(), query)
        assert passes(query, row)["reasons"] == ["homepage"], query


@pytest.mark.parametrize("query,title", [
    ("Nintendo", "Nintendo"), ("Nintendo homepage", "Nintendo"), ("Nintendo official site", "Nintendo"),
    ("Nintendo website", "Nintendo"), ("nintendo home page", "Nintendo"), ("Hugging Face", "Hugging Face")])
def test_navigational_queries_accept_a_homepage(query, title):
    host = "huggingface.co" if "Face" in query else "www.nintendo.com"
    assert passes(query, item("https://%s/" % host, title))["passed"]
    assert passes(query, item("https://%s/en-us/" % host, title))["passed"]


def test_a_navigational_query_accepts_the_entity_homepage_but_a_window_still_rejects_it():
    row = item("https://huggingface.co/", "Hugging Face")
    assert passes("Hugging Face", row)["passed"]
    assert passes("Hugging Face official site", row)["passed"]
    assert not passes("Hugging Face official site", row, recency="week")["passed"]


def test_real_items_still_pass_news_queries():
    row = item("https://www.nintendo.com/en-us/news/2026/10/new-console-announced", "Nintendo announces console")
    assert passes("Nintendo latest announcement", row)["passed"]
    assert passes("Nintendo latest announcement", item("https://www.nintendo.com/switch-2-sales", "Nintendo"))["passed"]


def test_only_a_leading_segment_is_a_locale():
    assert url_kind("https://example.com/en/") == "homepage"
    assert url_kind("https://example.com/zh-hans") == "homepage"
    assert url_kind("https://example.com/docs/en/install-guide") == "item"
    assert url_kind("https://example.com/about-us") == "tag_listing"


@pytest.mark.parametrize("query,expected", [
    ("Nintendo latest announcement", [["nintendo"]]),
    ("Hugging Face new model release", [["hugging", "face"]]),
    ("Arm Holdings earnings", [["arm", "holdings"]]),
    ("Bank of America layoffs", [["bank", "america"]]),
    ("Tesla and Ford recall", [["tesla"], ["ford"]]),
    ("latest news on Apple", [["apple"]]),
    ("how to fix a leaky tap", []),
    ("Best laptops for students", []),
    ("Install Docker on Ubuntu", [["docker"], ["ubuntu"]]),
    ("Rust Async Runtime Comparison", []),
    ("rust async runtime", []),
    ("What is Rust", [["rust"]]),
])
def test_entities_are_capitalised_runs_without_filler(query, expected):
    assert entities(query) == expected


@pytest.mark.parametrize("query,title,url", [
    ("Arm Holdings earnings", "Alarm company reports earnings", "https://a.test/alarm-earnings"),
    ("Arm Holdings earnings", "Harm reduction earnings", "https://a.test/x/1"),
    ("Meta earnings", "Metadata earnings explained", "https://a.test/metadata-earnings"),
    ("SAP earnings", "Sapling earnings report", "https://a.test/sapling-earnings"),
    ("Nintendo latest announcement", "Sony latest announcement", "https://sony.test/news/2026/announcement"),
])
def test_terms_match_whole_words_not_substrings(query, title, url):
    verdict = passes(query, item(url, title))
    assert verdict["on_query"] is False and "off_query" in verdict["reasons"]


@pytest.mark.parametrize("query,title,url", [
    ("Arm Holdings earnings", "Arm Holdings reports earnings", "https://a.test/news/arm-1"),
    ("Arm Holdings earnings", "Holdings: Arm quarterly results", "https://a.test/x/1"),
    ("Hugging Face new model", "New model released", "https://huggingface.co/blog/new-model-2026"),
    ("Hugging Face new model", "Hugging-Face launches a model", "https://a.test/x/1"),
    ("Nintendo latest announcement", "Switch sales", "https://www.nintendo.com/en-us/news/2026/switch-sales"),
    ("Kubernetes operators", "Writing a Kubernetes operator", "https://a.test/x/1"),
    ("Tesla earnings", "Teslas earned more", "https://a.test/tesla-earnings-q3"),
])
def test_entity_in_title_url_or_domain_is_on_query(query, title, url):
    assert passes(query, item(url, title))["on_query"] is True


def test_multi_word_entity_needs_every_word_not_any_one():
    assert passes("Hugging Face models", item("https://a.test/x/1", "Face cream models"))["on_query"] is False
    assert passes("Hugging Face models", item("https://a.test/x/1", "Hugging puppies"))["on_query"] is False
    # "Arm" alone in the snippet is not the company, even next to another query term's absence.
    assert passes("Arm Holdings earnings", item("https://a.test/x/1", "Wrestling", "Arm wrestling tips"))["on_query"] is False


def test_a_generic_company_word_alone_in_unrelated_text_is_not_on_query():
    # "apple" only in the snippet and no other query term: a recipe, not the company.
    recipe = item("https://food.test/pie", "Easy pie", "Slice an apple and bake.")
    assert passes("Apple", recipe)["on_query"] is False
    assert passes("Apple earnings", recipe)["on_query"] is False
    # The same word in the title is the company's.
    assert passes("Apple earnings", item("https://news.test/x/1", "Apple earnings beat"))["on_query"] is True


def test_a_snippet_mention_counts_with_another_query_term_or_a_full_multi_word_name():
    assert passes("Apple earnings", item("https://a.test/x/1", "Quarterly results", "Apple posted earnings"))["on_query"]
    assert passes("Hugging Face", item("https://a.test/x/1", "Open models", "by Hugging Face"))["on_query"]


def test_lowercase_queries_match_any_whole_word_term():
    assert passes("rust async", item("https://a.test/x/1", "Async patterns"))["on_query"] is True
    assert passes("rust async", item("https://a.test/x/1", "Trust asynchronous"))["on_query"] is False


def test_profile_pages_named_for_the_entity_are_not_items_only_for_specific_queries():
    row = item("https://x.test/nintendo", "Nintendo (@nintendo)")
    assert passes("Nintendo latest announcement", row)["reasons"] == ["homepage"]
    assert passes("Nintendo", row)["passed"]
    assert passes("Hugging Face news", item("https://x.test/hugging-face", "HF"))["reasons"] == ["homepage"]


def test_failing_homepages_and_wrong_company_results_leave_a_weak_set():
    junk = [item("https://www.nintendo.com/", "Nintendo"), item("https://sony.test/news/2026/a-launch", "Sony launch"),
            item("https://x.test/alarm-2026", "Alarm system installs"), item("https://www.nintendo.com/en-us/", "Nintendo"),
            item("https://nintendo.test/news/2026/10/new-game", "Nintendo new game announced")]
    report = check_results("Nintendo latest announcement", junk, now=NOW)
    assert report["verdict"] == "weak" and report["passed"] == 1
    assert report["reasons"] == {"homepage": 2, "off_query": 2}


# --- entity aliases: acronyms, expansions, corporate suffixes, domains --------

def on_query(query, url, title="", snippet=""):
    return passes(query, item(url, title, snippet))["on_query"]


@pytest.mark.parametrize("query,title", [
    ("Amazon Web Services latest announcement", "AWS announces a new region"),
    ("International Business Machines latest announcement", "IBM unveils a quantum chip"),
    ("General Electric latest announcement", "GE reports quarterly earnings"),
])
def test_an_entitys_initials_in_the_title_match_the_entity(query, title):
    assert on_query(query, "https://a.test/x/1", title) is True


@pytest.mark.parametrize("query,title", [
    ("General Electric latest announcement", "ge reports quarterly earnings"),
    ("General Electric latest announcement", "Why ge is a common prefix"),
    ("Amazon Web Services latest announcement", "AWSOME news"),
    ("Amazon Web Services latest announcement", "Awsome Wonderful Stories announcement"),
])
def test_acronyms_need_the_whole_token_and_two_letter_ones_need_uppercase(query, title):
    assert on_query(query, "https://a.test/x/1", title) is False


def test_an_acronym_in_the_url_or_domain_matches_in_any_case():
    query = "International Business Machines latest announcement"
    assert on_query(query, "https://www.ibm.com/new/announcements-2026", "Quantum chip") is True
    assert on_query(query, "https://a.test/news/ibm-quantum-chip", "Quantum chip") is True
    assert on_query(query, "https://a.test/news/ibmx-quantum-chip", "Quantum chip") is False
    # A two-letter acronym counts as a domain label only, not as a path word.
    assert on_query("General Electric announcement", "https://ge.com/news/2026/jet", "Jet engine") is True
    assert on_query("General Electric announcement", "https://a.test/ge/news/2026", "Jet engine") is False


def test_a_snippet_mention_of_an_acronym_does_not_count():
    assert on_query("Amazon Web Services announcement", "https://a.test/x/1", "Cloud news", "AWS did a thing") is False


@pytest.mark.parametrize("query,title,url", [
    ("AWS latest announcement", "Amazon Web Services launches a new region", "https://a.test/x/1"),
    ("IBM latest announcement", "International Business Machines unveils a chip", "https://a.test/x/1"),
    ("AMD latest announcement", "Advanced Micro Devices, Inc. reports results", "https://a.test/x/1"),
    ("NASA latest announcement", "National Aeronautics and Space Administration picks a lander", "https://a.test/x/1"),
    ("AWS latest announcement", "Cloud region opens", "https://aws.amazon.com/about-aws/whats-new/2026/10/region"),
    ("AMD latest announcement", "New GPUs", "https://www.amd.com/en/newsroom/2026/gpus.html"),
])
def test_an_all_caps_query_entity_matches_words_that_spell_it_and_acronym_domains(query, title, url):
    assert on_query(query, url, title) is True


@pytest.mark.parametrize("query,title", [
    ("AWS latest announcement", "Amazon Web launches a new region"),
    ("AWS latest announcement", "Amazon web services launches a region"),
    ("AWS latest announcement", "Amazon Cloud Services launches"),
    ("IBM latest announcement", "Intel Brings Chips announcement"),
])
def test_expansion_needs_capitalised_words_with_the_right_initials(query, title):
    assert on_query(query, "https://a.test/x/1", title) is False


@pytest.mark.parametrize("query,title,url", [
    ("Arm Holdings latest announcement", "Arm unveils a new core", "https://a.test/x/1"),
    ("Arm Holdings latest announcement", "New core", "https://www.arm.com/company/news/2026/core"),
    ("Arm Holdings latest announcement", "New core", "https://arm.com/newsroom/2026/core"),
    ("Mistral AI latest announcement", "Mistral releases a new model", "https://a.test/x/1"),
    ("Mistral AI latest announcement", "New model", "https://mistral.ai/news/2026/model"),
    ("Unity Technologies latest announcement", "New engine", "https://unity.com/news/2026/engine"),
    ("Unity Technologies latest announcement", "Unity ships an engine update", "https://a.test/x/1"),
])
def test_an_entity_goes_by_its_name_without_corporate_suffixes_and_by_its_domain(query, title, url):
    assert on_query(query, url, title) is True


@pytest.mark.parametrize("query,title,url,snippet", [
    ("Arm Holdings latest announcement", "Alarm systems announce a launch", "https://a.test/alarm-launch", ""),
    ("Arm Holdings latest announcement", "Harm reduction announcement", "https://a.test/x/1", ""),
    ("Arm Holdings latest announcement", "Armed forces announcement", "https://a.test/armed-forces", ""),
    ("Arm Holdings latest announcement", "Shoulder injuries", "https://a.test/x/1", "Your arm has bones."),
    ("Arm Holdings latest announcement", "Recovery", "https://farm.com/news/2026/x", ""),
    ("Mistral AI latest announcement", "Cooking", "https://a.test/x/1", "A mistral recipe"),
    ("Unity Technologies latest announcement", "Cooking", "https://community.com/x/1", ""),
])
def test_dropping_corporate_suffixes_keeps_whole_word_and_snippet_protection(query, title, url, snippet):
    assert on_query(query, url, title, snippet) is False


def test_a_lone_core_word_in_a_snippet_needs_another_query_term():
    query = "Arm Holdings latest announcement"
    assert on_query(query, "https://a.test/x/1", "Chip news", "Arm posted an announcement") is True
    assert on_query(query, "https://a.test/x/1", "Chip news", "Arm posted a thing") is False


def test_a_domain_equal_to_the_entity_run_together_matches():
    assert on_query("Hugging Face news", "https://huggingface.co/blog/2026/new", "A post") is True
    assert on_query("Hugging Face news", "https://hugging.co/blog/2026/new", "A post") is False
    assert on_query("Mistral AI news", "https://www.mistralai.com/news/2026/model", "A post") is True
    assert on_query("Bank of America news", "https://www.bankofamerica.com/news/2026/x", "A post") is True


def test_connector_acronyms_match_in_their_written_form():
    query = "Bank of America layoffs"
    assert on_query(query, "https://a.test/x/1", "BofA announces layoffs") is True
    assert on_query(query, "https://a.test/x/1", "BoA announces layoffs") is True
    assert on_query(query, "https://a.test/x/1", "BOA announces layoffs") is True
    assert on_query(query, "https://a.test/x/1", "Bofa constrictors announce layoffs") is False


def test_aliases_do_not_change_entity_extraction():
    assert entities("Amazon Web Services latest announcement") == [["amazon", "web", "services"]]
    assert entities("AWS latest announcement") == [["aws"]]
    assert entities("Mistral AI news") == [["mistral"]]


def test_the_aws_row_is_on_query_in_both_directions_in_a_result_set():
    rows = [item("https://a.test/n/1", "AWS launches a region"),
            item("https://b.test/n/2", "Amazon Web Services adds a tool"),
            item("https://c.test/n/3", "Azure launches a region")]
    for query in ("Amazon Web Services latest announcement", "AWS latest announcement"):
        report = check_results(query, rows, now=NOW)
        assert [row["on_query"] for row in report["results"]] == [True, True, False]
