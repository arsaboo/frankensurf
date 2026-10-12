"""Search v2: query building for GitHub and Wikipedia, a broken bing_rss, relevance ranking, the
wrong-entity guard, paid search in parallel, the specialist and SearXNG racing, and fill without a
window. Synthetic results and fake plugins only; nothing here touches the network."""
import asyncio
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from frankensurf import Runtime, WebPolicy
from frankensurf.search import build_search, github_parts, github_query, normalize_results, wiki_topic
from frankensurf.search_checks import (accepted, annotate, check_results, merge_results, query_kinds,
                                       unrelated_set)
from frankensurf.search_plugins import DEFAULT_SEARCHES, HttpSearchPlugin, SearchRegistry

from test_search_ladder import CALLS, Fake, dated, good, install, run


@pytest.fixture(autouse=True)
def fresh_calls():
    CALLS.clear()


def params(url):
    return {key: values[0] for key, values in parse_qs(urlparse(url).query).items()}


def row(url, title, snippet="", **extra):
    return {"url": url, "title": title, "snippet": snippet, **extra}


# --- GitHub query building ------------------------------------------------------------------

@pytest.mark.parametrize("query,expected", [
    ("fastify github repo", "fastify in:name"),
    ("httpx python github", "httpx in:name language:python"),
    ("official pydantic repository", "pydantic in:name"),
    ("React", "react in:name"),
    ("rust github repo", "rust in:name"),                      # the language is the project
    ("pydantic settings library", "pydantic settings in:name"),
])
def test_github_name_lookup_drops_platform_words_and_searches_names(query, expected):
    assert github_query(query) == expected
    assert github_parts(query)["name_lookup"] is True


@pytest.mark.parametrize("query,expected", [
    ("python library for image processing", '"image processing" in:name,description language:python'),
    ("best typescript package for form validation", '"form validation" in:name,description language:typescript'),
    ("go library for http clients", '"http clients" in:name,description language:go'),
    ("a rust crate that parses markdown", '"parses markdown" in:name,description language:rust'),
])
def test_github_need_keeps_topic_and_language_and_does_not_search_names(query, expected):
    assert github_query(query) == expected
    assert github_parts(query)["name_lookup"] is False


def test_github_request_sorts_by_stars_and_keeps_the_recency_window():
    now = datetime(2026, 10, 12, tzinfo=timezone.utc)
    got = params(build_search("fastify github repo", "github", now=now)[0])
    assert got["q"] == "fastify in:name" and got["sort"] == "stars" and got["order"] == "desc"
    got = params(build_search("python library for image processing", "github", {"recency": "month"}, now=now)[0])
    assert got["q"] == '"image processing" in:name,description language:python pushed:>2026-09-11'
    assert "github" not in got["q"] and "repo" not in got["q"]
    # Qualifiers the person already wrote stay.
    assert "org:fastify" in github_query("router org:fastify github")


# --- Wikipedia ----------------------------------------------------------------------------

@pytest.mark.parametrize("query,topic", [
    ("who wrote War and Peace", "War and Peace"),
    ("who invented the light bulb", "light bulb"),
    ("smallest country in the world", "smallest country in world"),
    ("when was the Eiffel Tower built", "Eiffel Tower"),
    ("what is a monad", "monad"),
    ("how many moons does Mars have", "moons Mars have"),
    ("Hamlet", "Hamlet"),
    ("who", "who"),                                            # nothing left: keep the query
])
def test_wikipedia_gets_the_topic_not_the_question_words(query, topic):
    assert wiki_topic(query) == topic
    assert params(build_search(query, "wikipedia")[0])["srsearch"] == topic


def wiki(*titles, snippets=None):
    snippets = snippets or {}
    return {"query": {"search": [{"title": title, "snippet": snippets.get(title, "text about it")}
                                 for title in titles]}}


def wikipedia_titles(query, payload):
    url = build_search(query, "wikipedia")[0]
    return [item["title"] for item in normalize_results("", payload, "wikipedia", 20, url)[0]]


def test_wikipedia_puts_the_article_titled_for_the_topic_first():
    payload = wiki("War and Peace (film series)", "Leo Tolstoy", "war and peace", "War & Peace (2016 TV series)")
    assert wikipedia_titles("who wrote War and Peace", payload)[0] == "war and peace"
    # Case, articles and a trailing parenthetical do not matter for the match.
    assert wikipedia_titles("The Hobbit", wiki("Tolkien", "Hobbit (film)", "Hobbit", "The Hobbit"))[0] in ("Hobbit", "The Hobbit")
    assert wikipedia_titles("light bulb", wiki("Fluorescent lamp", "Incandescent light bulb", "Light bulb"))[:2] == [
        "Light bulb", "Incandescent light bulb"]


def test_wikipedia_demotes_parenthetical_qualifiers_unless_the_query_has_them():
    payload = wiki("War and Peace (film series)", "War and Peace (1956 film)", "Leo Tolstoy", "War and Peace")
    assert wikipedia_titles("who wrote War and Peace", payload) == [
        "War and Peace", "Leo Tolstoy", "War and Peace (film series)", "War and Peace (1956 film)"]
    # "film" in the query is the qualifier's kind of thing: the 1956 film is no longer demoted,
    # while "(film series)" still has a word ("series") the query did not use.
    got = wikipedia_titles("War and Peace film", payload)
    assert got.index("War and Peace (1956 film)") < got.index("War and Peace (film series)")
    assert got[-1] == "War and Peace (film series)"


def test_wikipedia_drops_disambiguation_pages():
    payload = wiki("Mercury (disambiguation)", "Mercury", "Mercury (element)", "Freddie Mercury",
                   snippets={"Freddie Mercury": "Freddie Mercury may refer to: a singer or a crater"})
    got = wikipedia_titles("Mercury", payload)
    assert "Mercury (disambiguation)" not in got and "Freddie Mercury" not in got
    assert got == ["Mercury", "Mercury (element)"]
    # Without a topic in the request only the disambiguation pages go.
    assert [item["title"] for item in normalize_results("", payload, "wikipedia", 20)[0]] == [
        "Mercury", "Mercury (element)"]


# --- a broken bing_rss ----------------------------------------------------------------------

def rss(*titles):
    return "<rss><channel>%s</channel></rss>" % "".join(
        "<item><title>%s</title><link>https://help.example/%d</link><description>about help</description></item>"
        % (title, number) for number, title in enumerate(titles))


async def test_bing_rss_answering_every_query_with_the_same_pages_is_a_failure(tmp_path):
    garbage = httpx.MockTransport(lambda request: httpx.Response(
        200, text=rss("YouTube Help", "Resorts in Queensland"), headers={"content-type": "application/rss+xml"}))
    async with Runtime(tmp_path, transport=garbage) as web:
        result = await web.search("fastify github repo", source="bing_rss", limit=5)
    assert result["results"] == []
    assert result["receipt"]["status"] == "failed"
    assert result["receipt"]["failure"]["code"] == "SEARCH_UNAVAILABLE"
    assert "unrelated" in result["receipt"]["failure"]["message"]
    assert result["upstream_failures"] == [["bing_rss", "results unrelated to the query"]]


async def test_bing_rss_with_at_least_one_on_query_result_is_an_answer(tmp_path):
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, text=rss("YouTube Help", "Fastify web framework"), headers={"content-type": "application/rss+xml"}))
    async with Runtime(tmp_path, transport=transport) as web:
        result = await web.search("fastify github repo", source="bing_rss", limit=5)
    assert result["receipt"]["status"] == "observed" and len(result["results"]) == 2


def test_unrelated_set_needs_results_and_key_terms():
    assert unrelated_set("fastify github repo", [row("https://a.test/1", "YouTube Help")]) is True
    assert unrelated_set("fastify github repo", [row("https://a.test/1", "Help", "built on fastify")]) is False
    assert unrelated_set("fastify github repo", []) is False
    assert unrelated_set("the", [row("https://a.test/1", "anything")]) is False  # no key terms: nothing to judge


def test_bing_rss_is_the_last_web_source_but_a_policy_route_still_decides():
    registry = SearchRegistry()
    registry.register(HttpSearchPlugin("bing_rss"))
    registry.register(HttpSearchPlugin("searxng"))
    registry.register(Fake("later"))
    policy = WebPolicy(provider="http")
    assert registry.candidates(policy) == ["searxng", "later", "bing_rss"]
    routed = WebPolicy(provider="http", search_source_candidates=("bing_rss", "searxng"))
    assert registry.candidates(routed) == ["bing_rss", "searxng"]
    assert DEFAULT_SEARCHES.candidates(policy)[-1] == "bing_rss"


async def test_a_last_resort_source_never_races_a_slow_one(monkeypatch, tmp_path):
    install(monkeypatch, Fake("slow", good(4), delay=0.4), Fake("bing_rss", good(5, "b")))
    policy = WebPolicy(provider="http", search_hedge_after_seconds=0.05)
    result = await run(tmp_path, policy=policy)
    assert CALLS == ["slow"] and result["receipt"]["search_ladder"]["winner"] == "slow"


# --- relevance ranking ------------------------------------------------------------------------

NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)


def relevance_order(query, rows, **kwargs):
    report = check_results(query, rows, limit=10, now=NOW, **kwargs)
    kept = accepted(rows, report)
    return [item["url"] for item in kept], {v["url"]: v["relevance"] for v in report["results"]}


def test_title_matches_outrank_snippet_only_matches_and_stay_in_the_sources_order_when_equal():
    rows = [row("https://a.test/1", "Some page", "all about sqlalchemy migrations"),
            row("https://b.test/2", "SQLAlchemy migrations guide"),
            row("https://c.test/3", "Alembic and SQLAlchemy migrations")]
    order, scores = relevance_order("sqlalchemy migrations", rows)
    assert order == ["https://b.test/2", "https://c.test/3", "https://a.test/1"]
    assert scores["https://b.test/2"] > scores["https://a.test/1"] > 0


def test_the_repository_named_for_the_project_comes_first():
    rows = [row("https://github.com/LizDodion/NestJsSwaggerExample", "LizDodion/NestJsSwaggerExample",
                "an example that uses fastify", extra={"stars": 3}),
            row("https://github.com/someone/fastify", "someone/fastify", "my copy", extra={"stars": 0}),
            row("https://github.com/fastify/fastify", "fastify/fastify", "Fast web framework", extra={"stars": 30000}),
            row("https://github.com/fastify/fastify-cors", "fastify/fastify-cors", "", extra={"stars": 500})]
    order, _ = relevance_order("fastify github repo", rows)
    assert order[0] == "https://github.com/fastify/fastify"
    assert order[1] == "https://github.com/someone/fastify"       # the name, but no stars
    assert order[-1] == "https://github.com/LizDodion/NestJsSwaggerExample"


def test_a_project_named_with_a_language_word_is_still_found():
    rows = [row("https://github.com/rhawk117/github-httpx-client", "rhawk117/github-httpx-client", "client"),
            row("https://github.com/encode/httpx", "encode/httpx", "A next-generation HTTP client", extra={"stars": 14000})]
    assert relevance_order("httpx python github", rows)[0][0] == "https://github.com/encode/httpx"


def test_the_entitys_own_domain_is_canonical_and_official_for_announcements():
    rows = [row("https://news.example/2026/10/11/acme-roundup", "Acme and others in this week's roundup"),
            row("https://www.acme.com/news/2026/10/acme-launches-widget", "Acme launches the Widget"),
            row("https://blog.example/acme-widget", "Acme Widget reviewed")]
    order, scores = relevance_order("Acme latest announcement", rows, recency="month")
    assert order[0] == "https://www.acme.com/news/2026/10/acme-launches-widget"
    assert scores[order[0]] >= scores[order[1]] + 3


def test_stock_quote_policy_docs_and_listing_pages_rank_below_ordinary_results():
    rows = [row("https://finance.example/quote/ACME", "Acme Corp (ACME) share price"),
            row("https://acme.com/legal/privacy", "Privacy Policy - Acme"),
            row("https://acme.com/docs/release-notes/v4-2", "Acme release notes"),
            row("https://acme.com/security/advisories", "Acme security advisories"),
            row("https://news.example/acme-tag", "Acme: latest news, breaking headlines and updates"),
            row("https://blog.example/acme-hires", "Acme hires a new chief executive", "in the company's announcement")]
    order, scores = relevance_order("Acme announcement", rows, recency="month")
    assert len(order) == 6
    assert order[0] == "https://blog.example/acme-hires"
    # Stock quotes and policy pages are last, even the policy page on the company's own domain.
    assert order[-2:] == ["https://acme.com/legal/privacy", "https://finance.example/quote/ACME"]
    for url in order[1:]:
        assert scores[url] < scores["https://blog.example/acme-hires"]
    assert scores["https://acme.com/legal/privacy"] < 0 and scores["https://finance.example/quote/ACME"] < 0


def test_the_penalties_do_not_apply_when_the_query_asks_for_that_kind_of_page():
    quote = row("https://finance.example/quote/ACME", "Acme Corp (ACME) share price")
    policy = row("https://acme.com/legal/privacy", "Privacy Policy - Acme")
    notes = row("https://acme.com/docs/release-notes/v4-2", "Acme release notes")
    assert relevance_order("Acme share price", [quote])[1][quote["url"]] > 0
    assert relevance_order("Acme privacy policy", [policy])[1][policy["url"]] > 0
    assert relevance_order("Acme release notes", [notes], recency="month")[1][notes["url"]] > 0
    assert relevance_order("Acme announcement", [notes], recency="month")[1][notes["url"]] < 0


def test_awesome_lists_and_paper_digests_rank_last_for_paper_queries_only():
    rows = [row("https://github.com/someone/awesome-diffusion-papers", "awesome-diffusion-papers",
                "A curated list of diffusion papers"),
            row("https://arxiv.org/abs/2601.00001", "Diffusion models for audio"),
            row("https://digest.example/daily", "Daily papers digest: diffusion models")]
    order, scores = relevance_order("diffusion models papers", rows)
    assert order[0] == "https://arxiv.org/abs/2601.00001"
    assert scores["https://github.com/someone/awesome-diffusion-papers"] < 0
    _, plain = relevance_order("diffusion awesome lists", rows[:1])
    assert plain["https://github.com/someone/awesome-diffusion-papers"] > 0


def test_merge_orders_a_tier_by_relevance_then_fusion_and_keeps_dated_results_first():
    fresh = (NOW - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [row("https://finance.example/quote/ACME", "Acme share price", published=fresh),
            row("https://blog.example/acme-hires", "Acme hires a chief executive", published=fresh),
            row("https://old.example/acme", "Acme history")]
    report = check_results("Acme announcement", rows, recency="month", now=NOW)
    kept = annotate(rows, report)
    merged = merge_results([("a", kept), ("b", list(reversed(kept)))], windowed=True)
    assert [item["url"] for item in merged] == ["https://blog.example/acme-hires",
                                               "https://finance.example/quote/ACME", "https://old.example/acme"]


# --- the wrong entity -----------------------------------------------------------------------------

def verdicts(query, rows, **kwargs):
    report = check_results(query, rows, limit=10, now=NOW, **kwargs)
    return {item["url"]: (verdict["passed"], verdict["reasons"]) for item, verdict in zip(rows, report["results"])}


def test_the_tv_show_is_not_the_company():
    rows = [row("https://www.9now.com.au/the-block", "The Block - Channel 9", "Watch the renovation series"),
            row("https://www.theblock.co/post/1", "The Block: crypto news", "latest announcement"),
            row("https://example.com/news/2026/10/cinder", "Cinder Block prices rise across the market", "announcement"),
            row("https://news.example.com/2026/10/12/mixed", "Watch The Block Mount Eliza finale and more"),
            row("https://block.xyz/inside/q3-results", "Block announces third quarter results"),
            row("https://investors.block.xyz/news/2026/10/new-product", "Block, Inc. Announces New Product"),
            row("https://news.example.com/2026/10/12/dorsey", "Jack Dorsey's Block unveils a bitcoin miner"),
            row("https://news.example.com/2026/10/12/wallet", "Latest Block announcement: a new hardware wallet")]
    got = verdicts("Block latest announcement", rows, recency="month")
    for url in ("https://www.9now.com.au/the-block", "https://www.theblock.co/post/1",
                "https://example.com/news/2026/10/cinder", "https://news.example.com/2026/10/12/mixed"):
        assert got[url] == (False, ["wrong_entity"]), url
    for url in ("https://block.xyz/inside/q3-results", "https://investors.block.xyz/news/2026/10/new-product",
                "https://news.example.com/2026/10/12/dorsey", "https://news.example.com/2026/10/12/wallet"):
        assert got[url] == (True, []), url


def test_the_entitys_own_domain_is_never_the_wrong_entity_and_longer_names_are_untouched():
    assert verdicts("Block latest announcement",
                    [row("https://block.xyz/news/2026/10/the-block-story", "The Block of the future: our plans")],
                    recency="month")["https://block.xyz/news/2026/10/the-block-story"][0] is True
    # A two-word entity is a name already: no guard.
    assert verdicts("Tata Motors news", [row("https://x.example/2026/10/a", "The Tata Motors results")])[
        "https://x.example/2026/10/a"][0] is True
    # Lower-case queries name no entity.
    assert verdicts("block news", [row("https://x.example/2026/10/a", "The Block returns")])[
        "https://x.example/2026/10/a"][0] is True


def test_a_wrong_entity_result_does_not_count_toward_a_good_set():
    shows = [row("https://show%d.test/the-block-%d" % (n, n), "The Block episode %d" % n) for n in range(4)]
    assert check_results("Block latest announcement", shows, recency="month")["verdict"] == "empty"


# --- what kind of query it is ---------------------------------------------------------------------

@pytest.mark.parametrize("query,vertical,recency,kinds", [
    ("who wrote War and Peace", "web", None, {"fact"}),
    ("smallest country in the world", "web", None, {"fact"}),
    ("fastify github repo", "web", None, {"code"}),
    ("python library for image processing", "web", None, {"code"}),
    ("Block latest announcement", "web", "month", {"announcement"}),
    ("news about Tata Motors past month", "news", "month", {"news", "announcement"}),
    ("anything", "reference", None, {"fact"}),
    ("anything", "code", None, {"code"}),
    ("how to bake bread", "web", None, set()),
    ("rust async runtime comparison", "web", None, set()),
])
def test_query_kinds(query, vertical, recency, kinds):
    assert query_kinds(query, vertical=vertical, recency=recency) == frozenset(kinds)


# --- paid search in parallel ---------------------------------------------------------------------------

def paid_policy(**kwargs):
    base = dict(provider="http", allow_paid_fallbacks=True, search_date_reads=0)
    return WebPolicy(**{**base, **kwargs})


def test_the_policy_flag_defaults_on_and_validates():
    assert WebPolicy().search_paid_parallel is True
    with pytest.raises(Exception):
        WebPolicy(search_paid_parallel="yes")


async def test_a_news_style_query_starts_the_cheapest_paid_source_beside_the_first_free_one(monkeypatch, tmp_path):
    import frankensurf.runtime as runtime
    monkeypatch.setattr(runtime, "_paid_priors", lambda: {"exa": {"overall": [0, 0], "cost_usd": 0.005},
                                                          "firecrawl_search": {"overall": [0, 0], "cost_usd": 0.02}})
    install(monkeypatch, Fake("free", dated(4, "f"), delay=0.1), Fake("free_two", dated(4, "g")),
            Fake("firecrawl_search", dated(4, "w"), paid=True, cost=0.02, delay=0.05),
            Fake("exa", dated(4, "e"), paid=True, cost=0.005, delay=0.05))
    result = await run(tmp_path, recency="week", limit=5, policy=paid_policy())
    # The free source has not finished when the paid one starts; the dearer paid source never starts.
    assert CALLS == ["free", "exa"]
    ladder = result["receipt"]["search_ladder"]
    assert ladder["paid_parallel"] == "exa" and ladder["first_rung"] == ["free", "exa"]
    assert ladder["winner"] == "merged" and ladder["stopped"] == "good"
    assert result["receipt"]["cost_usd"] == 0.005
    assert {row_["url"][8] for row_ in result["results"]} == {"f", "e"}
    assert all(attempt["raced"] for attempt in result["receipt"]["search_attempts"])


async def test_paid_results_that_fail_the_checks_give_way_to_passing_free_ones(monkeypatch, tmp_path):
    junk = [{"url": "https://junk%d.test/x/1" % n, "title": "unrelated topic %d" % n} for n in range(5)]
    install(monkeypatch, Fake("free", dated(4, "f")), Fake("exa", junk, paid=True, cost=0.005))
    result = await run(tmp_path, recency="week", limit=5, policy=paid_policy())
    assert CALLS == ["free", "exa"]
    assert len(result["results"]) == 4 and all("junk" not in item["url"] for item in result["results"])
    assert result["receipt"]["cost_usd"] == 0.005
    exa = [attempt for attempt in result["receipt"]["search_attempts"] if attempt["source"] == "exa"][0]
    assert exa["passed"] == 0


async def test_paid_runs_alone_first_when_there_is_no_free_source(monkeypatch, tmp_path):
    install(monkeypatch, Fake("exa", dated(3, "e"), paid=True, cost=0.005))
    result = await run(tmp_path, recency="week", limit=3, policy=paid_policy())
    assert CALLS == ["exa"] and len(result["results"]) == 3


@pytest.mark.parametrize("query,kwargs", [
    ("rust async", dict(recency="week")),                      # announcement-type (windowed)
    ("who wrote War and Peace", dict()),                        # fact
    ("fastify github repo", dict()),                            # code
    ("rust async", dict(vertical="news")),                      # news
])
async def test_each_kind_of_query_starts_paid_in_parallel(monkeypatch, tmp_path, query, kwargs):
    def rows(prefix):
        titles = "rust async fastify war peace wrote github repo"
        return [{"url": "https://%s%d.test/rust/story-%d" % (prefix, n, n), "title": titles + " %d" % n,
                 "published": (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")}
                for n in range(4)]
    vertical = kwargs.get("vertical", "web")
    install(monkeypatch, Fake("free", rows("f"), vertical=vertical, delay=0.05),
            Fake("exa", rows("e"), paid=True, cost=0.005))
    await run(tmp_path, query=query, limit=5, policy=paid_policy(), **kwargs)
    assert CALLS == ["free", "exa"]


@pytest.mark.parametrize("policy,query,kwargs", [
    (WebPolicy(provider="http", search_date_reads=0), "rust async", dict(recency="week")),          # paid not allowed
    (paid_policy(search_paid_parallel=False), "rust async", dict(recency="week")),                    # flag off
    (paid_policy(), "rust async", dict()),                                                           # plain query
    (paid_policy(), "how to bake bread", dict()),                                                    # how-to
    (paid_policy(search_source_candidates=("free", "exa")), "rust async", dict(recency="week")),   # explicit route
    (paid_policy(search_ladder=False), "rust async", dict(recency="week")),                          # first answer only
])
async def test_paid_never_runs_in_parallel_when_not_allowed_or_not_wanted(monkeypatch, tmp_path, policy, query, kwargs):
    fresh = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [{"url": "https://f%d.test/x/story-%d" % (n, n), "title": query + " story %d" % n, "published": fresh}
            for n in range(4)]
    install(monkeypatch, Fake("free", rows, delay=0.05), Fake("exa", dated(4, "e"), paid=True, cost=0.005))
    await run(tmp_path, query=query, limit=3, policy=policy, **kwargs)
    assert CALLS == ["free"]


async def test_parallel_paid_respects_the_cost_cap(monkeypatch, tmp_path):
    install(monkeypatch, Fake("free", dated(4, "f"), delay=0.05), Fake("exa", dated(4, "e"), paid=True, cost=0.5))
    result = await run(tmp_path, recency="week", limit=5, policy=paid_policy(max_cost_usd=0.1))
    # The paid source overspent its cap, so its answer is refused; the free results stand.
    exa = [attempt for attempt in result["receipt"]["search_attempts"] if attempt["source"] == "exa"][0]
    assert exa["status"] == "failed" and exa["failure"] == "BUDGET_EXHAUSTED"
    assert len(result["results"]) == 4 and all(item["url"].startswith("https://f") for item in result["results"])


async def test_a_paid_source_that_is_not_cost_bounded_is_not_started_under_a_cap(monkeypatch, tmp_path):
    unbounded = Fake("exa", dated(4, "e"), paid=True, cost=0.005)
    object.__setattr__(unbounded.manifest, "cost_bounded", False)
    install(monkeypatch, Fake("free", dated(4, "f")), unbounded)
    result = await run(tmp_path, recency="week", limit=3, policy=paid_policy(max_cost_usd=0.1))
    assert CALLS == ["free"] and "paid_parallel" not in result["receipt"]["search_ladder"]


# --- the specialist and SearXNG race ------------------------------------------------------------------------

class Up(Fake):
    """A source that can say whether it is reachable."""

    def __init__(self, *args, up=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.up = up

    def reachable(self, base_url=None):
        return self.up


def repo_rows(prefix, count=4):
    return [{"url": "https://%s%d.test/fastify/project-%d" % (prefix, n, n), "title": "fastify project %d" % n}
            for n in range(count)]


@pytest.mark.parametrize("vertical", ["code", "reference", "papers", "news"])
async def test_the_specialist_and_searxng_start_together_and_their_answers_merge(monkeypatch, tmp_path, vertical):
    install(monkeypatch, Up("specialist", repo_rows("s", 3), vertical=vertical, delay=0.05),
            Up("searxng", repo_rows("x", 3), delay=0.15), Fake("ddg", repo_rows("d")))
    result = await run(tmp_path, query="fastify", limit=5, vertical=vertical)
    # The specialist was good alone, and SearXNG was not left waiting for it to fail or be cancelled.
    assert CALLS == ["specialist", "searxng"]
    ladder = result["receipt"]["search_ladder"]
    assert ladder["first_rung"] == ["specialist", "searxng"] and ladder["winner"] == "merged"
    assert {item["url"][8] for item in result["results"]} == {"s", "x"} and len(result["results"]) == 5
    assert [row_["source"] for row_ in result["receipt"]["search_attempts"]] == ["specialist", "searxng"]


async def test_a_failing_specialist_does_not_hold_searxng_back(monkeypatch, tmp_path):
    install(monkeypatch, Up("specialist", fail=True, vertical="code"), Up("searxng", repo_rows("x")))
    result = await run(tmp_path, query="fastify", vertical="code")
    assert CALLS == ["specialist", "searxng"] and len(result["results"]) == 4
    assert result["receipt"]["search_ladder"]["winner"] == "searxng"


async def test_a_racer_slower_than_the_grace_is_cut_off_and_the_answer_goes_ahead(monkeypatch, tmp_path):
    import frankensurf.runtime as runtime
    monkeypatch.setattr(runtime, "_RACE_GRACE_SECONDS", 0.1)
    install(monkeypatch, Up("specialist", repo_rows("s"), vertical="code"), Up("searxng", repo_rows("x"), delay=5))
    result = await run(tmp_path, query="fastify", vertical="code")
    statuses = {row_["source"]: row_["status"] for row_ in result["receipt"]["search_attempts"]}
    assert statuses == {"specialist": "observed", "searxng": "cancelled"}
    assert result["receipt"]["search_ladder"]["winner"] == "specialist" and len(result["results"]) == 4


async def test_no_race_when_searxng_is_not_reachable_or_hedging_is_off_or_the_vertical_has_no_specialist(monkeypatch, tmp_path):
    install(monkeypatch, Up("specialist", repo_rows("s"), vertical="code"), Up("searxng", repo_rows("x"), up=False))
    await run(tmp_path, query="fastify", vertical="code", limit=3)
    assert CALLS == ["specialist"]
    CALLS.clear()
    install(monkeypatch, Up("specialist", repo_rows("s"), vertical="code"), Up("searxng", repo_rows("x")))
    await run(tmp_path, query="fastify", vertical="code", limit=3,
              policy=WebPolicy(provider="http", search_hedge_after_seconds=0))
    assert CALLS == ["specialist"]
    CALLS.clear()
    install(monkeypatch, Up("searxng", repo_rows("x")), Fake("ddg", repo_rows("d")))
    await run(tmp_path, query="fastify", limit=3)  # the web vertical has no specialist
    assert CALLS == ["searxng"]


# --- fill without a window -------------------------------------------------------------------------------------

async def test_a_short_answer_without_a_window_is_filled_once_when_searxng_is_reachable(monkeypatch, tmp_path):
    install(monkeypatch, Up("searxng", good(3)), Fake("two", good(3, "t")), Fake("three", good(3, "u")))
    result = await run(tmp_path, limit=5)
    assert CALLS == ["searxng", "two"]                      # one extra rung at most
    assert len(result["results"]) == 5 and result["receipt"]["search_ladder"]["winner"] == "merged"


async def test_the_fill_rung_asks_searxng_first_when_something_else_led(monkeypatch, tmp_path):
    install(monkeypatch, Fake("one", good(3, "o")), Fake("two", good(3, "t")), Up("searxng", good(3, "s")))
    result = await run(tmp_path, limit=5)
    assert CALLS == ["one", "searxng"]
    assert {item["url"][8] for item in result["results"]} == {"o", "s"} and len(result["results"]) == 5


async def test_fill_without_a_window_needs_reachable_searxng_room_in_the_limit_and_the_setting(monkeypatch, tmp_path):
    install(monkeypatch, Up("searxng", good(3), up=False), Fake("two", good(3, "t")))
    await run(tmp_path, limit=5)
    assert CALLS == ["searxng"]                             # not reachable: no fill
    CALLS.clear()
    install(monkeypatch, Up("searxng", good(5)), Fake("two", good(3, "t")))
    await run(tmp_path, limit=5)
    assert CALLS == ["searxng"]                             # already full
    CALLS.clear()
    install(monkeypatch, Up("searxng", good(3)), Fake("two", good(3, "t")))
    await run(tmp_path, limit=5, policy=WebPolicy(provider="http", search_fill_rungs=0))
    assert CALLS == ["searxng"]                             # turned off
    CALLS.clear()
    install(monkeypatch, Up("searxng", good(3)), Fake("two", good(3, "t")))
    await run(tmp_path, limit=5, source="searxng")
    assert CALLS == ["searxng"]                             # an explicit source is asked once


async def test_filling_never_reaches_paid_or_a_last_resort_source(monkeypatch, tmp_path):
    install(monkeypatch, Up("searxng", good(3)), Fake("bing_rss", good(3, "b")),
            Fake("exa", good(3, "p"), paid=True, cost=0.01))
    await run(tmp_path, limit=5, policy=WebPolicy(provider="http", allow_paid_fallbacks=True))
    assert CALLS == ["searxng"]
