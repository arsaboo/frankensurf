"""Search v2, iteration 2: the article rule for announcement queries, fact queries that do not let a
specialist's off-topic article fill a slot, code ranking and filters, arXiv identifiers as dates, and
source priors when paid sources are allowed. The URL shapes are examples of generic patterns; the
fixtures are synthetic and nothing here touches the network."""
from datetime import datetime, timedelta, timezone

import pytest

from frankensurf import WebPolicy
from frankensurf.search import github_parts, github_query, normalize_results, reference_topic
from frankensurf.search_checks import (FREE_SOURCE_PRIORS, accepted, annotate, article_like, check_results,
                                       demote_off_topic, merge_results, query_context, source_prior,
                                       topic_matches, url_kind, _url_date)

from test_search_ladder import CALLS, Fake, dated, install, run
from test_search_v2 import Up, paid_policy, row

NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
FRESH = (NOW - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(autouse=True)
def fresh_calls():
    CALLS.clear()


def verdict_of(query, url, title="", snippet="", **kwargs):
    item = row(url, title, snippet, **kwargs.pop("fields", {}))
    report = check_results(query, [item], limit=10, now=NOW, **kwargs)
    return report["results"][0]


def order_of(query, rows, **kwargs):
    report = check_results(query, rows, limit=10, now=NOW, **kwargs)
    return [item["url"] for item in accepted(rows, report)], {v["url"]: v for v in report["results"]}


# --- 1. announcement queries: an article, or a verified date ------------------------------------------------------

NOT_ARTICLES = [
    ("CrowdStrike latest announcement", "https://www.crowdstrike.com/en-us/pricing/", "CrowdStrike Pricing"),
    ("CrowdStrike latest announcement", "https://www.crowdstrike.com/en-us/careers/", "CrowdStrike Careers"),
    ("CrowdStrike latest announcement", "https://www.crowdstrike.com/en-us/platform/", "CrowdStrike Falcon Platform"),
    ("CrowdStrike latest announcement", "https://www.crowdstrike.com/en-us/services/", "CrowdStrike Services"),
    ("CrowdStrike latest announcement", "https://www.crowdstrike.com/en-us/platform/falcon-guardian-aidr/",
     "CrowdStrike Falcon Guardian AIDR"),
    ("Roku latest announcement", "https://my.roku.com/signin", "Sign in to your Roku account"),
    ("Roku latest announcement", "https://investors.roku.com/overview/default.aspx", "Roku Investor Overview"),
    ("Roku latest announcement", "https://investors.roku.com/", "Roku Investor Relations"),
    ("Roku latest announcement", "https://newsroom.roku.com/news-product?l=100", "Roku Newsroom - Product News"),
    ("Roku latest announcement", "https://investor.roku.com/financials/sec-filings/default.aspx", "SEC Filings | Roku"),
    ("Roku latest announcement", "https://investor.roku.com/financials/quarterly-results/default.aspx",
     "Quarterly Results | Roku"),
    ("Roku latest announcement", "https://www.kogan.com/au/c/roku-smart-tvs/", "Roku Smart TVs - Kogan"),
    ("Roku latest announcement", "https://www.techradar.com/best-roku-devices", "The best Roku devices"),
    ("Roku latest announcement", "https://en.wikipedia.org/wiki/Roku,_Inc.", "Roku, Inc. - Wikipedia"),
    ("Roku latest announcement", "https://en.wikipedia.org/wiki/Roku", "Roku - Wikipedia"),
]


@pytest.mark.parametrize("query,url,title", NOT_ARTICLES)
def test_an_announcement_query_in_a_window_fails_pages_that_are_not_articles(query, url, title):
    got = verdict_of(query, url, title, recency="month")
    assert got["passed"] is False and "not_article" in got["reasons"], got


ARTICLES = [
    ("CrowdStrike latest announcement",
     "https://www.crowdstrike.com/en-us/blog/crowdstrike-announces-new-falcon-capability/",
     "CrowdStrike announces new Falcon capability"),
    ("CrowdStrike latest announcement", "https://www.crowdstrike.com/en-us/press-releases/crowdstrike-launches-aidr/",
     "CrowdStrike launches AIDR"),
    ("CrowdStrike latest announcement",
     "https://ir.crowdstrike.com/news-releases/news-release-details/crowdstrike-announces-results",
     "CrowdStrike announces results"),
    ("Roku latest announcement", "https://newsroom.roku.com/news/2026/10/roku-announces-update", "Roku announces update"),
    ("Roku latest announcement", "https://news.example.com/news/12345678", "Roku announces a new streaming stick"),
    ("Roku latest announcement", "https://blog.example.com/roku-announces-a-new-streaming-stick-today",
     "Roku announces a new streaming stick"),
    ("Roku latest announcement", "https://news.example.com/20261008/roku-thing", "Roku announces a deal"),
]


@pytest.mark.parametrize("query,url,title", ARTICLES)
def test_article_shaped_paths_pass_without_a_date(query, url, title):
    got = verdict_of(query, url, title, recency="month")
    assert got["passed"] is True, got
    assert article_like(url)


def test_a_verified_in_window_date_passes_a_page_with_no_article_path():
    page = "https://www.roku.com/whats-on/spotlight"
    undated = verdict_of("Roku latest announcement", page, "Roku spotlight", recency="month")
    assert undated["passed"] is False and undated["reasons"] == ["not_article"]
    dated_ = verdict_of("Roku latest announcement", page, "Roku spotlight", recency="month",
                        fields={"published": FRESH})
    assert dated_["passed"] is True and dated_["in_window"] is True
    # A date that proves it is out of the window fails on that, not on being an article.
    old = verdict_of("Roku latest announcement", page, "Roku spotlight", recency="month",
                     fields={"published": "2024-01-01T00:00:00Z"})
    assert old["passed"] is False and "out_of_window" in old["reasons"]


def test_the_article_rule_needs_both_a_window_and_announcement_wording():
    url = "https://www.crowdstrike.com/en-us/platform/falcon-guardian"
    assert verdict_of("CrowdStrike latest announcement", url, "CrowdStrike platform")["reasons"] == []
    assert "not_article" not in verdict_of("CrowdStrike funding round", "https://www.crowdstrike.com/about",
                                           "CrowdStrike about", recency="month")["reasons"]
    for wording in ("announces", "announcement", "press release", "launch"):
        got = verdict_of("CrowdStrike %s" % wording, "https://www.crowdstrike.com/en-us/platform/fal-con",
                         "CrowdStrike", recency="month")
        assert "not_article" in got["reasons"], wording


@pytest.mark.parametrize("url,kind", [
    ("https://investors.acme.com/overview/default.aspx", "tag_listing"),
    ("https://investor.acme.com/financials/sec-filings/default.aspx", "tag_listing"),
    ("https://investor.acme.com/financials/quarterly-results/default.aspx", "tag_listing"),
    ("https://acme.com/presentations", "tag_listing"),
    ("https://acme.com/events", "tag_listing"),
    ("https://acme.com/en-us/governance/", "tag_listing"),
    ("https://acme.com/careers", "tag_listing"),
    ("https://acme.com/pricing/", "tag_listing"),
    ("https://acme.com/en-us/platform/", "tag_listing"),
    ("https://acme.com/services", "tag_listing"),
    ("https://acme.com/products/", "tag_listing"),
    ("https://acme.com/solutions", "tag_listing"),
    ("https://my.acme.com/signin", "tag_listing"),
    ("https://my.acme.com/sign-in", "tag_listing"),
    ("https://my.acme.com/login", "tag_listing"),
    ("https://my.acme.com/account", "tag_listing"),
    ("https://acme.com/support", "tag_listing"),
    ("https://acme.com/help", "tag_listing"),
    ("https://acme.com/category", "tag_listing"),
    ("https://shop.example/au/c/roku-smart-tvs/", "tag_listing"),
    ("https://newsroom.acme.com/news-product?l=100", "tag_listing"),
    ("https://newsroom.acme.com/news-product?page=2", "tag_listing"),
    ("https://acme.com/stories?category=ai", "tag_listing"),
    ("https://investors.acme.com/", "homepage"),
    # Not listings:
    ("https://acme.com/blog/acme-announces-new-widget?page=2", "item"),
    ("https://acme.com/2026/10/12/acme-news?page=2", "item"),
    ("https://acme.com/platform/falcon-guardian-aidr/", "item"),
    ("https://github.com/acme/platform", "item"),
    ("https://github.com/acme/help", "item"),
])
def test_url_kind_knows_the_generic_index_words_and_listing_parameters(url, kind):
    assert url_kind(url) == kind


# The ladder: no passing result climbs, nothing passing is returned as nothing.

def products(prefix, count=4):
    return [{"url": "https://%s%d.test/platform/" % (prefix, n), "title": "Acme platform %d" % n}
            for n in range(count)]


def articles(prefix, count=4):
    return [{"url": "https://%s%d.test/blog/acme-announces-widget-%d" % (prefix, n, n),
             "title": "Acme announces widget %d" % n} for n in range(count)]


def plain_policy(**kwargs):
    return WebPolicy(provider="http", search_date_reads=0, **kwargs)


async def test_product_pages_climb_to_the_next_free_source(monkeypatch, tmp_path):
    install(monkeypatch, Fake("first", products("p")), Fake("second", articles("a")))
    result = await run(tmp_path, query="Acme latest announcement", recency="week", limit=4, policy=plain_policy())
    assert CALLS == ["first", "second"]
    assert [item["url"][8] for item in result["results"]] == ["a"] * 4
    assert result["receipt"]["search_checks"]["reasons"]["not_article"] >= 4


async def test_product_pages_climb_to_paid_when_it_is_allowed(monkeypatch, tmp_path):
    install(monkeypatch, Fake("first", products("p"), delay=0.05), Fake("second", articles("a")),
            Fake("exa", articles("e"), paid=True, cost=0.005))
    result = await run(tmp_path, query="Acme latest announcement", recency="week", limit=4,
                       policy=paid_policy())
    assert "exa" in CALLS
    assert result["results"] and all(item["url"][8] in "ae" for item in result["results"])


async def test_when_nothing_passes_nothing_is_returned_and_failures_do_not_pad_it(monkeypatch, tmp_path):
    install(monkeypatch, Fake("first", products("p")), Fake("second", products("q")))
    result = await run(tmp_path, query="Acme latest announcement", recency="week", limit=4, policy=plain_policy())
    assert CALLS == ["first", "second"]
    assert result["results"] == []
    assert result["receipt"]["search_checks"]["verdict"] in ("empty", None)


async def test_a_passing_set_comes_back_without_the_failures_in_it(monkeypatch, tmp_path):
    install(monkeypatch, Fake("first", products("p", 3) + articles("a", 3)), Fake("second", articles("b")))
    result = await run(tmp_path, query="Acme latest announcement", recency="week", limit=10, policy=plain_policy())
    assert result["results"] and all("/blog/" in item["url"] for item in result["results"])


# --- 2. fact queries: a specialist's off-topic article ranks below the web ----------------------------------------------------

@pytest.mark.parametrize("query,topic", [
    ("who founded Samsung", "Samsung"),
    ("population of New Zealand", "New Zealand"),
    ("who wrote War and Peace", "War and Peace"),
    ("what is the capital of France", "France"),
    ("Moonlight Sonata", "Moonlight Sonata"),
])
def test_the_topic_of_a_fact_question(query, topic):
    assert reference_topic(query) == topic


@pytest.mark.parametrize("query,title,matches", [
    ("who founded Samsung", "Samsung", True),
    ("who founded Samsung", "Samsung (company)", True),
    ("who founded Samsung", "Samsung Galaxy", False),
    ("who founded Samsung", "Samsung Electronics", False),
    ("who wrote War and Peace", "War and Peace", True),
    ("who wrote War and Peace", "No war, no peace", False),
    ("who wrote War and Peace", "Sonya (War and Peace)", False),
    ("Moonlight Sonata", "Moonlight Sonata", True),
    ("Moonlight Sonata", "Sonata (Moonlight)", False),
    ("population of New Zealand", "New Zealand", True),
    ("population of New Zealand", "New Zealanders", False),
    ("population of New Zealand", "New Zealand census", False),
    ("smallest country in the world", "World's Smallest Violin", False),
    ("theory of relativity", "Criticism of the theory of relativity", False),
])
def test_a_title_matches_the_topic_when_equal_or_equal_minus_a_parenthetical(query, title, matches):
    assert topic_matches(query, title) is matches


def test_demote_off_topic_moves_only_specialist_only_off_topic_results_to_the_end():
    items = [row("https://en.wikipedia.org/wiki/Samsung_Galaxy", "Samsung Galaxy"),
             row("https://web.test/samsung-history", "Samsung history"),
             row("https://en.wikipedia.org/wiki/Samsung", "Samsung"),
             row("https://en.wikipedia.org/wiki/Samsung_Electronics", "Samsung Electronics")]
    only = {"en.wikipedia.org/wiki/Samsung_Galaxy", "en.wikipedia.org/wiki/Samsung",
            "en.wikipedia.org/wiki/Samsung_Electronics"}
    got = [item["title"] for item in demote_off_topic("who founded Samsung", items, only)]
    assert got == ["Samsung history", "Samsung", "Samsung Galaxy", "Samsung Electronics"]
    # A page the web engines returned too is not a specialist-only result, whatever its title.
    got = [item["title"] for item in demote_off_topic("who founded Samsung", items, only - {"en.wikipedia.org/wiki/Samsung_Galaxy"})]
    assert got.index("Samsung Galaxy") < got.index("Samsung Electronics")
    assert got[:3] == ["Samsung Galaxy", "Samsung history", "Samsung"]


def wiki_rows():
    return [{"url": "https://en.wikipedia.org/wiki/Samsung_Galaxy", "title": "Samsung Galaxy", "snippet": "phones"},
            {"url": "https://en.wikipedia.org/wiki/Samsung", "title": "Samsung", "snippet": "founded by Lee Byung-chul"},
            {"url": "https://en.wikipedia.org/wiki/Samsung_Electronics", "title": "Samsung Electronics",
             "snippet": "company"}]


def web_rows(count=5):
    return [{"url": "https://answers%d.test/samsung/founder-%d" % (n, n), "title": "Who founded Samsung? part %d" % n}
            for n in range(count)]


async def test_an_off_topic_specialist_article_never_fills_a_slot_while_web_results_remain(monkeypatch, tmp_path):
    install(monkeypatch, Up("wikipedia", wiki_rows(), vertical="reference", delay=0.02),
            Up("searxng", web_rows(), delay=0.05))
    result = await run(tmp_path, query="who founded Samsung", limit=5, policy=plain_policy())
    titles = [item["title"] for item in result["results"]]
    assert len(titles) == 5
    assert "Samsung Galaxy" not in titles and "Samsung Electronics" not in titles
    assert "Samsung" in titles    # the article titled for the topic stays
    # With room to spare they follow every web result.
    result = await run(tmp_path, query="who founded Samsung", limit=10, policy=plain_policy())
    titles = [item["title"] for item in result["results"]]
    assert titles[-2:] == ["Samsung Galaxy", "Samsung Electronics"] and len(titles) == 8


async def test_the_off_topic_article_is_kept_when_the_web_has_nothing(monkeypatch, tmp_path):
    install(monkeypatch, Up("wikipedia", wiki_rows(), vertical="reference"), Up("searxng", [], up=False))
    result = await run(tmp_path, query="who founded Samsung", limit=5, policy=plain_policy())
    assert [item["title"] for item in result["results"]][0] == "Samsung"
    assert len(result["results"]) == 3


# --- 3. code queries --------------------------------------------------------------------------------------------------------

TOPICS = "rust serialization testing framework scheduling jobs configuration files http client"


def repo(owner, name, stars=None, language=None, snippet=TOPICS, **extra):
    return row("https://github.com/%s/%s" % (owner, name), "%s/%s" % (owner, name), snippet,
               extra={"stars": stars, "stargazers_count": stars, "language": language, "fork": False,
                      "archived": False, **extra})


def test_github_results_keep_stars_fork_archived_and_language_and_drop_forks_and_archives():
    payload = {"items": [
        {"html_url": "https://github.com/encode/httpx", "full_name": "encode/httpx", "description": "client",
         "stargazers_count": 14000, "fork": False, "archived": False, "language": "Python",
         "pushed_at": "2026-10-01T00:00:00Z", "created_at": "2019-01-01T00:00:00Z"},
        {"html_url": "https://github.com/A-Archives-and-Forks/httpx-", "full_name": "A-Archives-and-Forks/httpx-",
         "stargazers_count": 1, "fork": True, "archived": False, "language": "Python"},
        {"html_url": "https://github.com/old/httpx", "full_name": "old/httpx", "stargazers_count": 900,
         "fork": False, "archived": True, "language": "Python"}]}
    found, _ = normalize_results("", payload, "github", 10)
    assert [item["title"] for item in found] == ["encode/httpx"]
    extra = found[0]["extra"]
    assert extra["stargazers_count"] == 14000 and extra["fork"] is False and extra["archived"] is False
    assert extra["language"] == "Python"


def test_github_parts_treats_x_framework_as_a_need():
    assert github_parts("typescript testing framework")["name_lookup"] is False
    assert github_query("typescript testing framework") == '"testing framework" in:name,description language:typescript'
    assert github_parts("fastify github repo")["name_lookup"] is True


def test_forks_and_archived_repositories_fail_even_from_another_source():
    rows = [repo("a", "httpx", 5000, "Python", fork=True), repo("b", "httpx", 5000, "Python", archived=True)]
    _, verdicts = order_of("httpx python github", rows)
    assert verdicts[rows[0]["url"]]["reasons"] == ["fork"] and verdicts[rows[1]["url"]]["reasons"] == ["archived"]


def test_a_named_language_drops_repositories_of_another_known_language():
    rows = [repo("projectdiscovery", "httpx", 9000, "Go"), repo("encode", "httpx", 14000, "Python"),
            repo("someone", "httpx-extras", 400, None)]
    order, verdicts = order_of("httpx python github", rows)
    assert verdicts["https://github.com/projectdiscovery/httpx"]["reasons"] == ["wrong_language"]
    assert order[0] == "https://github.com/encode/httpx"
    assert "https://github.com/someone/httpx-extras" in order        # unknown language: not held against it
    # Without a language in the query the Go tool stays.
    order, _ = order_of("httpx github", rows)
    assert "https://github.com/projectdiscovery/httpx" in order
    # The language is compared as GitHub names it.
    assert order_of("rust crate for serialization", [repo("serde-rs", "serde", 9000, "Rust")])[0]
    assert not order_of("c++ library for serialization", [repo("a", "ser", 9000, "C")])[0]
    assert order_of("c++ library for serialization", [repo("a", "ser", 9000, "C++")])[0]


def test_a_need_drops_repositories_under_500_stars_and_a_name_lookup_under_100_unless_exact():
    rows = [repo("aws-samples", "performance-testing-framework-for-apache-kafka", 120, "TypeScript"),
            repo("L-Qun", "mcp-testing-framework", 40, "TypeScript"),
            repo("Bondar-Academy", "playwright-api-testing-framework", 80, "TypeScript"),
            repo("jestjs", "jest", 44000, "TypeScript")]
    order, verdicts = order_of("typescript testing framework", rows, vertical="code")   # as the router infers it
    assert order ==["https://github.com/jestjs/jest"]
    assert all(verdicts[r["url"]]["reasons"] == ["low_stars"] for r in rows[:3])
    rows = [repo("a", "scheduler-lib", 499, "Python"), repo("b", "sched", 500, "Python")]
    assert order_of("python library for scheduling jobs", rows)[0] == ["https://github.com/b/sched"]
    # A name lookup: the repository of that name has no floor, others need 100.
    rows = [repo("someone", "fastify", 0), repo("x", "fastify-thing", 99), repo("y", "fastify-other", 100)]
    order, _ = order_of("fastify github repo", rows)
    assert "https://github.com/someone/fastify" in order and "https://github.com/y/fastify-other" in order
    assert "https://github.com/x/fastify-thing" not in order


def test_repo_queries_prefer_repository_pages_to_org_pages_and_project_sites():
    rows = [row("https://github.com/fastify", "fastify", "Fastify has 80 repositories"),
            row("https://github.com/fastify/demo", "fastify/demo", "a demo app", extra={"stars": 300}),
            row("https://fastify.dev/", "Fastify - Fast and low overhead web framework"),
            repo("fastify", "fastify", 30000, "JavaScript"),
            repo("fastify", "fastify-cors", 600, "JavaScript")]
    order, verdicts = order_of("fastify github repo", rows)
    # Iteration 3: an add-on (fastify-cors) is not the project; it ranks below the project's own site.
    assert order[0] == "https://github.com/fastify/fastify"
    assert order.index("https://fastify.dev/") < order.index("https://github.com/fastify/fastify-cors")
    assert order.index("https://github.com/fastify") < order.index("https://github.com/fastify/demo")
    assert order.index("https://fastify.dev/") < order.index("https://github.com/fastify/demo")
    assert order[-1] == "https://github.com/fastify/demo"
    # The project site is not penalised when the query does not ask for the repository.
    assert verdicts["https://fastify.dev/"]["relevance"] < 0
    assert order_of("fastify web framework", rows[2:3])[1]["https://fastify.dev/"]["relevance"] > 0
    for name in ("tokio", "polars"):
        site = row("https://%s.rs/" % name, name, "the project")
        rows = [site, repo(name + "-rs", name, 20000, "Rust")]
        assert order_of("%s github repo" % name, rows)[0][0] == "https://github.com/%s-rs/%s" % (name, name)


@pytest.mark.parametrize("name", ["demo", "examples", "sample-app", "template", "starter-kit", "boilerplate",
                                  "tutorial", "rust_serialization_benchmark", "awesome-rust", "api-examples"])
def test_demos_examples_templates_and_benchmarks_rank_last_unless_asked_for(name):
    rows = [repo("someone", name, 5000, "Rust"), repo("serde-rs", "serde", 9000, "Rust")]
    order, verdicts = order_of("rust crate for serialization", rows)
    assert order[-1] == "https://github.com/someone/" + name
    assert verdicts[rows[0]["url"]]["relevance"] < 0


@pytest.mark.parametrize("asked,name", [("examples", "serde-examples"), ("demo", "demo"), ("benchmark", "serde-benchmark"),
                                        ("template", "rust-template")])
def test_demo_like_repositories_are_not_demoted_when_the_query_asks_for_them(asked, name):
    rows = [repo("someone", name, 5000, "Rust")]
    plain = order_of("rust crate for serialization", rows)[1][rows[0]["url"]]["relevance"]
    wanted = order_of("rust crate for serialization %s" % asked, rows)[1][rows[0]["url"]]["relevance"]
    assert plain < 0 < wanted


def test_a_name_lookup_leads_with_the_repository_of_that_name_and_owner():
    rows = [repo("A-Archives-and-Forks", "httpx-", 3, "Python"), repo("api-evangelist", "httpx", 150, "Python"),
            repo("lundberg", "pytest_httpx", 400, "Python"), repo("encode", "httpx", 14000, "Python"),
            repo("projectdiscovery", "httpx", 9000, "Go"), repo("random", "unrelated-client", 9000, "Python"),
            repo("example", "httpx", 120, "Python")]
    rows[5]["snippet"] = "an async http client like httpx"
    order, _ = order_of("httpx python github", rows)
    assert order[0] == "https://github.com/encode/httpx"
    assert "https://github.com/projectdiscovery/httpx" not in order
    # Anything ranked above a non-canonical repository holds the name as a word in owner/name; the
    # ones that do not come last.
    assert order[-1] == "https://github.com/random/unrelated-client"
    assert order.index("https://github.com/lundberg/pytest_httpx") < order.index("https://github.com/random/unrelated-client")


def test_a_need_ranks_library_pages_and_popular_repositories_above_low_star_repos_and_guides():
    rows = [repo("small", "cron-ish", 600, "Python"),
            row("https://medium.com/@someone/how-to-schedule-jobs-in-python-a1b2c3d4e5f6",
                "How to schedule jobs in Python - Medium"),
            repo("agronholm", "apscheduler", 6500, "Python"),
            row("https://blog.example/top-python-job-scheduling-libraries", "Top Python job scheduling libraries"),
            repo("BraxisAI", "braxis-blueprint", 12, "Python")]
    order, verdicts = order_of("python library for scheduling jobs", rows)
    assert "https://github.com/BraxisAI/braxis-blueprint" not in order
    pos = {url: order.index(url) for url in order}
    assert pos["https://blog.example/top-python-job-scheduling-libraries"] == 0
    assert pos["https://github.com/agronholm/apscheduler"] < pos["https://github.com/small/cron-ish"]
    assert pos["https://github.com/agronholm/apscheduler"] < pos["https://medium.com/@someone/how-to-schedule-jobs-in-python-a1b2c3d4e5f6"]
    assert pos["https://medium.com/@someone/how-to-schedule-jobs-in-python-a1b2c3d4e5f6"] == len(order) - 1 or \
        verdicts["https://medium.com/@someone/how-to-schedule-jobs-in-python-a1b2c3d4e5f6"]["relevance"] < \
        verdicts["https://blog.example/top-python-job-scheduling-libraries"]["relevance"]


def test_a_how_to_guide_ranks_below_library_pages_but_is_not_failed():
    rows = [row("https://medium.com/@dev/managing-configuration-files-in-go-9f8e7d6c5b4a",
                "How to manage configuration files in Go"),
            row("https://pkg.go.dev/github.com/spf13/viper", "viper package - go library for configuration")]
    order, _ = order_of("go library for configuration files", rows)
    assert order == ["https://pkg.go.dev/github.com/spf13/viper", rows[0]["url"]]
    # Asking for a guide lifts the penalty.
    _, verdicts = order_of("go library for configuration files tutorial", rows)
    assert verdicts[rows[0]["url"]]["relevance"] > 0


def test_exas_code_misses_rank_last_for_a_repo_query():
    rows = [row("https://github.com/fastify", "Fastify", ""), row("https://github.com/fastify/demo", "fastify/demo", ""),
            row("https://tokio.rs/", "Tokio - An asynchronous Rust runtime"),
            repo("fastify", "fastify", 30000, "JavaScript")]
    order, _ = order_of("fastify github repository", rows)
    assert order[0] == "https://github.com/fastify/fastify" and order[-1] == "https://github.com/fastify/demo"


async def test_a_repository_the_code_source_rejects_is_dropped_from_a_web_engines_results_too(monkeypatch, tmp_path):
    github = [repo("encode", "httpx", 14000, "Python"), repo("projectdiscovery", "httpx", 9000, "Go")]
    web = [row("https://github.com/projectdiscovery/httpx", "GitHub - projectdiscovery/httpx: httpx is a fast tool"),
           row("https://github.com/encode/httpx", "GitHub - encode/httpx: A next generation HTTP client for Python"),
           row("https://www.python-httpx.org/", "HTTPX documentation")]
    install(monkeypatch, Up("github", github, vertical="code", delay=0.02), Up("searxng", web, delay=0.03))
    result = await run(tmp_path, query="httpx python github", limit=5, policy=plain_policy())
    urls = [item["url"] for item in result["results"]]
    assert "https://github.com/projectdiscovery/httpx" not in urls
    assert urls[0] == "https://github.com/encode/httpx"


async def test_with_a_named_language_repository_pages_only_the_web_returned_follow_the_confirmed_ones(monkeypatch, tmp_path):
    github = [repo("encode", "httpx", 14000, "Python"), repo("lundberg", "pytest_httpx", 400, "Python")]
    web = [row("https://github.com/projectdiscovery/httpx", "GitHub - projectdiscovery/httpx: httpx is a fast tool"),
           row("https://github.com/encode/httpx", "GitHub - encode/httpx: A next generation HTTP client for Python"),
           row("https://www.python-httpx.org/", "HTTPX documentation")]
    install(monkeypatch, Up("github", github, vertical="code", delay=0.02), Up("searxng", web, delay=0.03))
    result = await run(tmp_path, query="httpx python github", limit=10, policy=plain_policy())
    urls = [item["url"] for item in result["results"]]
    assert urls[0] == "https://github.com/encode/httpx" and urls[-1] == "https://github.com/projectdiscovery/httpx"
    # No language named: nothing is held back for want of one.
    result = await run(tmp_path, query="httpx github", limit=10, policy=plain_policy())
    assert "https://github.com/projectdiscovery/httpx" in [item["url"] for item in result["results"]]


def test_bing_web_rss_dates_are_crawl_times_not_publication_dates():
    rss = ("<rss><channel><item><title>Acme platform</title><link>https://acme.test/platform/</link>"
           "<description>d</description><pubDate>Mon, 12 Oct 2026 07:19:00 GMT</pubDate></item></channel></rss>")
    web, _ = normalize_results(rss, None, "bing_rss", 5)
    assert "published" not in web[0] and web[0]["indexed_date"]
    news, _ = normalize_results(rss, None, "bing_news_rss", 5)
    assert news[0]["published"] == "2026-10-12T07:19:00Z"


# --- 4. arXiv identifiers are dates -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("https://arxiv.science/abs/2410.04941", (2024, 10, 31)),
    ("https://arxiv.org/abs/2602.01234v2", (2026, 2, 28)),
    ("https://example.org/pdf/2410.04941.pdf", (2024, 10, 31)),
    ("https://example.org/abs/0704.0001", (2007, 4, 30)),
])
def test_a_new_style_arxiv_identifier_is_the_end_of_its_month(url, expected):
    got = _url_date(url)
    assert (got.year, got.month, got.day) == expected


@pytest.mark.parametrize("url", ["https://example.org/abs/2413.04941", "https://example.org/abs/hep-th/9901001",
                                 "https://example.org/item/241004941", "https://example.org/abs/2410.0494",
                                 "https://example.org/abs/0613.12345"])
def test_things_that_are_not_arxiv_identifiers_give_no_date(url):
    assert _url_date(url) is None


def test_an_old_paper_fails_a_one_year_window_and_a_recent_one_passes():
    old = verdict_of("recent papers on diffusion models", "https://arxiv.science/abs/2410.04941",
                     "Diffusion models for audio", recency="year")
    assert old["passed"] is False and "out_of_window" in old["reasons"] and old["date_source"] == "url"
    new = verdict_of("recent papers on diffusion models", "https://arxiv.org/abs/2607.01234",
                     "Diffusion models for audio", recency="year")
    assert new["passed"] is True and new["in_window"] is True


# --- 5. source priors when paid is allowed -----------------------------------------------------------------------------------

def test_free_source_priors_and_the_special_cases():
    ctx = query_context("who founded Samsung")
    assert FREE_SOURCE_PRIORS == {"searxng": 0.75, "bing_news_rss": 0.6, "wikipedia": 0.6, "github": 0.5,
                                  "arxiv": 0.8, "stack_exchange": 0.7}
    assert source_prior("searxng", row("https://a.test/x", "t"), "q", ctx) == 0.75
    assert source_prior("something_else", row("https://a.test/x", "t"), "q", ctx) == 0.5
    assert source_prior("exa", row("https://a.test/x", "t"), "q", ctx, 290 / 300) == pytest.approx(0.9667, abs=1e-3)
    assert source_prior("wikipedia", row("https://w.test/Samsung", "Samsung"), "who founded Samsung", ctx) == 0.9
    assert source_prior("wikipedia", row("https://w.test/Galaxy", "Samsung Galaxy"), "who founded Samsung", ctx) == 0.6
    code = query_context("httpx python github")
    assert source_prior("github", repo("encode", "httpx", 14000), "httpx python github", code) == 0.85   # canonical
    assert source_prior("github", repo("x", "other", 2000), "httpx python github", code) == 0.85         # high stars
    assert source_prior("github", repo("x", "other", 20), "httpx python github", code) == 0.5


def prior_rows(prefix, count=3, fresh=True):
    return [{"url": "https://%s%d.test/story/rust-async-%d" % (prefix, n, n), "title": "rust async story %d" % n,
             **({"published": FRESH} if fresh else {})} for n in range(count)]


def with_priors(source, rows, rate=None, **kwargs):
    report = check_results("rust async", rows, limit=10, recency="week", now=NOW,
                           priors={"source": source, "paid_rate": rate}, **kwargs)
    return annotate(rows, report)


def test_a_passing_result_of_the_best_paid_source_outranks_a_free_one_of_similar_relevance():
    free = with_priors("searxng", prior_rows("f"))
    paid = with_priors("exa", prior_rows("e"), 290 / 300)
    merged = merge_results([("searxng", free), ("exa", paid)], windowed=True)
    assert [item["url"][8] for item in merged] == ["e", "e", "e", "f", "f", "f"]
    # Without priors the source order and fusion decide, as before.
    plain = merge_results([("searxng", annotate(prior_rows("f"), check_results("rust async", prior_rows("f"), recency="week", now=NOW))),
                           ("exa", annotate(prior_rows("e"), check_results("rust async", prior_rows("e"), recency="week", now=NOW)))],
                          windowed=True)
    assert [item["url"][8] for item in plain][:2] == ["f", "e"]
    assert "prior" not in plain[0]["check"]


def test_a_canonical_or_verified_fresh_free_result_can_still_outrank_it():
    # Canonical: the repository of the name from a free source beats a paid page of similar title.
    free = annotate([repo("encode", "httpx", 14000, "Python")], check_results(
        "httpx python github", [repo("encode", "httpx", 14000, "Python")], limit=10, now=NOW,
        priors={"source": "github", "paid_rate": None}))
    paid_rows = [row("https://blog.example/httpx-python-notes", "httpx python notes")]
    paid = annotate(paid_rows, check_results("httpx python github", paid_rows, limit=10, now=NOW,
                                             priors={"source": "exa", "paid_rate": 290 / 300}))
    merged = merge_results([("exa", paid), ("github", free)])
    assert merged[0]["url"] == "https://github.com/encode/httpx"
    # Verified fresh: an undated paid result does not displace a dated free one.
    fresh = with_priors("searxng", prior_rows("f", 1))
    undated = with_priors("exa", prior_rows("e", 1, fresh=False), 290 / 300)
    merged = merge_results([("exa", undated), ("searxng", fresh)], windowed=True)
    assert [item["url"][8] for item in merged] == ["f", "e"]


def test_a_page_several_sources_return_takes_the_best_prior():
    one = with_priors("searxng", prior_rows("s", 1))
    two = with_priors("exa", prior_rows("s", 1), 0.97)
    merged = merge_results([("searxng", one), ("exa", two)], windowed=True)
    assert len(merged) == 1 and merged[0]["check"]["prior"] == 0.97


async def test_with_paid_allowed_the_best_paid_sources_results_lead_and_free_ones_fill(monkeypatch, tmp_path):
    import frankensurf.runtime as runtime
    monkeypatch.setattr(runtime, "_paid_priors", lambda: {"exa": {"overall": [290, 300], "cost_usd": 0.0095}})
    free_rows = prior_rows("f", 4)
    exa_rows = prior_rows("e", 2) + [{"url": "https://junk.test/pricing/", "title": "rust async pricing"}]
    install(monkeypatch, Fake("searxng", free_rows, delay=0.05), Fake("exa", exa_rows, paid=True, cost=0.0095))
    result = await run(tmp_path, query="rust async", recency="week", limit=4, policy=paid_policy())
    urls = [item["url"] for item in result["results"]]
    assert [url[8] for url in urls] == ["e", "e", "f", "f"]
    assert all(item["check"]["prior"] in (0.75, round(290 / 300, 3)) for item in result["results"])


async def test_without_paid_nothing_changes_in_the_ordering(monkeypatch, tmp_path):
    install(monkeypatch, Fake("searxng", prior_rows("f", 4)), Fake("exa", prior_rows("e", 4), paid=True, cost=0.005))
    result = await run(tmp_path, query="rust async", recency="week", limit=4, policy=plain_policy())
    assert CALLS == ["searxng"]
    assert all("prior" not in item["check"] for item in result["results"])
