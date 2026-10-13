"""Search v2, iteration 4: when a paid source answered, the answer is anchored on its results. They keep
their own order and stay unless the checks fail them; the other sources' passing results fill the slots
that leaves. Without a paid answer, free merging is exactly as before. Synthetic results and fake plugins
only; nothing here touches the network."""
from datetime import datetime, timedelta, timezone

import pytest

from frankensurf import WebPolicy
from frankensurf.search_checks import (accepted, anchor_reasons, anchored_answer, annotate, check_results,
                                       merge_results, query_context)

from test_search_ladder import CALLS, Fake, dated, install, run
from test_search_v2 import paid_policy, row

NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def fresh_calls():
    CALLS.clear()


@pytest.fixture
def low_paid_prior(monkeypatch):
    """The paid source's measured rate is below the free source's default, so under the old merge every
    free result outranks its results."""
    import frankensurf.runtime as runtime
    monkeypatch.setattr(runtime, "_paid_priors", lambda: {"exa": {"overall": [1, 10], "cost_usd": 0.005}})


def urls(result):
    return [item["url"] for item in result["results"]]


def anchor_of(result):
    return result["receipt"]["search_ladder"].get("anchor")


def repo_row(owner, name, stars=5000, snippet="", language="JavaScript"):
    return row("https://github.com/%s/%s" % (owner, name), "%s/%s" % (owner, name), snippet,
               extra={"stars": stars, "stargazers_count": stars, "language": language, "fork": False,
                      "archived": False})


# --- the paid set is all good ---------------------------------------------------------------------------------

async def test_a_paid_set_that_passes_is_the_answer_in_its_own_order_even_if_free_results_score_higher(
        monkeypatch, tmp_path, low_paid_prior):
    when = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Its first result matches the query only through the snippet (lower relevance), the others by title.
    paid = [{"url": "https://e9.test/rust/runtime-notes", "title": "Tokio runtime notes",
             "snippet": "rust async", "published": when}] + dated(3, "e", days=2)
    install(monkeypatch, Fake("free", dated(4, "f"), delay=0.05), Fake("exa", paid, paid=True, cost=0.005))
    result = await run(tmp_path, recency="week", limit=4, policy=paid_policy())
    assert urls(result) == [item["url"] for item in paid]
    anchor = anchor_of(result)
    assert anchor == {"source": "exa", "kept": 4, "replaced": [], "filled": 0}
    # The same results, merged the old way, put the free ones first.
    free_group = annotate(dated(4, "f"), check_results("rust async", dated(4, "f"), recency="week", now=NOW,
                                                      priors={"source": "free", "paid_rate": None}))
    paid_rows = [{**item, "published": item["published"]} for item in paid]
    paid_group = annotate(paid_rows, check_results("rust async", paid_rows, recency="week", now=NOW,
                                                  priors={"source": "exa", "paid_rate": 0.1}))
    assert merge_results([("exa", paid_group), ("free", free_group)], windowed=True)[0]["url"].startswith("https://f")


# --- failures are replaced ------------------------------------------------------------------------------------

async def test_an_off_query_result_and_an_org_page_are_replaced_by_the_best_passing_free_results(
        monkeypatch, tmp_path, low_paid_prior):
    paid = [repo_row("fastify", "fastify", 30000),
            row("https://celebrity.test/gossip/1", "celebrity gossip this week"),
            row("https://github.com/fastify", "fastify · GitHub"),
            repo_row("fastify", "fastify-cors", 800),
            repo_row("fastify", "fastify-helmet", 700)]
    free = [repo_row("fastify", "fastify-static", 600), repo_row("fastify", "fastify-jwt", 500),
            repo_row("fastify", "fastify-sensible", 400)]
    install(monkeypatch, Fake("free", free, delay=0.05, vertical="code"), Fake("exa", paid, paid=True, cost=0.005))
    result = await run(tmp_path, query="fastify github repo", limit=5, policy=paid_policy())
    assert urls(result)[0] == "https://github.com/fastify/fastify"
    assert urls(result)[3:] == ["https://github.com/fastify/fastify-cors", "https://github.com/fastify/fastify-helmet"]
    assert urls(result)[1:3] == ["https://github.com/fastify/fastify-static", "https://github.com/fastify/fastify-jwt"]
    anchor = anchor_of(result)
    assert anchor["source"] == "exa" and anchor["kept"] == 3 and anchor["filled"] == 2
    assert anchor["replaced"] == [{"url": "https://celebrity.test/gossip/1", "reason": "off_query"},
                                  {"url": "https://github.com/fastify", "reason": "homepage"}]


async def test_the_code_demotions_count_as_failures_with_a_reason_code():
    ctx = query_context("react query github", vertical="code")
    assert anchor_reasons(row("https://github.com/TanStack", "TanStack"), ctx) == ["homepage"]            # org page
    assert anchor_reasons(row("https://tanstack.com/", "TanStack Query"), ctx) == ["homepage"]            # website
    assert anchor_reasons(row("https://github.com/TanStack/query", "TanStack/query"), ctx) == []
    assert anchor_reasons(row("https://tanstack.com/query/latest", "TanStack Query docs"), ctx) == []
    for name in ("react-query-demo", "query-examples", "react-boilerplate", "awesome-react-query",
                 "query-benchmarks", "react-query-starter", "react-query-template", "react-query-sample",
                 "react-query-tutorial"):
        assert anchor_reasons(row("https://github.com/x/" + name, name), ctx) == ["example_repo"], name
    # Asked for, they stay; and a query that is not about a repository keeps the front page.
    asked = query_context("react query examples github", vertical="code")
    assert anchor_reasons(row("https://github.com/x/query-examples", "x"), asked) == []
    assert anchor_reasons(row("https://www.fastify.io/", "Fastify web framework"),
                          query_context("how to set up a postgres connection pool")) == []


def test_each_code_demotion_removes_the_result_in_the_anchored_answer():
    rows = [repo_row("a", "fastify-demo", snippet="fastify demo"),
            row("https://github.com/fastify", "fastify org page"),
            row("https://www.fastify.io/", "Fastify web framework"),
            repo_row("fastify", "fastify", snippet="fastify")]
    # These pass the base checks (or fail them only as the front page kind); the anchored answer
    # holds the repository only, and says why for each of the others.
    paid = annotate(rows, check_results("fastify github repo", rows, limit=4, now=NOW, vertical="code"))
    answer, receipt = anchored_answer("fastify github repo", "exa", paid, [], limit=4, vertical="code")
    assert [item["url"] for item in answer] == ["https://github.com/fastify/fastify"]
    assert [item["url"] for item in receipt["replaced"]] == [rows[0]["url"], rows[1]["url"], rows[2]["url"]]
    assert receipt["replaced"][0]["reason"] == "example_repo"
    assert {item["reason"] for item in receipt["replaced"][1:]} == {"homepage"}
    assert receipt["kept"] == 1 and receipt["filled"] == 0


def test_a_failed_check_is_a_replacement_a_lower_relevance_is_not():
    rows = [row("https://a.test/rust/story-1", "rust async story"),
            row("https://b.test/rust/story-2", "something else", "mentions rust async only here"),
            row("https://c.test/x/1", "unrelated topic")]
    paid = annotate(rows, check_results("rust async", rows, limit=10, now=NOW))
    fill = annotate([row("https://d.test/rust/story-4", "rust async story four")],
                    check_results("rust async", [row("https://d.test/rust/story-4", "rust async story four")],
                                  limit=10, now=NOW))
    answer, receipt = anchored_answer("rust async", "exa", paid, fill, limit=3)
    passed = [item["url"] for item in paid if item["check"]["passed"]]
    assert [item["url"] for item in answer][:len(passed)] == passed
    assert receipt["replaced"] == [{"url": "https://c.test/x/1", "reason": "off_query"}]
    assert answer[-1]["url"] == "https://d.test/rust/story-4" and receipt["filled"] == 1


def test_a_repository_the_code_source_showed_to_be_a_fork_is_replaced():
    rows = [repo_row("a", "fastify"), repo_row("b", "fastify-fork")]
    paid = annotate(rows, check_results("fastify github repo", rows, limit=5, now=NOW, vertical="code"))
    answer, receipt = anchored_answer("fastify github repo", "exa", paid, [], limit=5, vertical="code",
                                      unwanted={"github.com/b/fastify-fork": "fork"})
    assert [item["url"] for item in answer] == ["https://github.com/a/fastify"]
    assert receipt["replaced"] == [{"url": "https://github.com/b/fastify-fork", "reason": "fork"}]


# --- a short paid set -----------------------------------------------------------------------------------------

async def test_a_short_paid_set_is_filled_after_its_own_results(monkeypatch, tmp_path, low_paid_prior):
    install(monkeypatch, Fake("free", dated(4, "f"), delay=0.05), Fake("exa", dated(2, "e"), paid=True, cost=0.005))
    result = await run(tmp_path, recency="week", limit=5, policy=paid_policy())
    assert [url[8] for url in urls(result)] == ["e", "e", "f", "f", "f"]
    assert anchor_of(result) == {"source": "exa", "kept": 2, "replaced": [], "filled": 3}


# --- a canonical free result the paid set lacks -----------------------------------------------------------------

async def test_a_canonical_free_repo_takes_the_last_slot_when_nothing_was_removed(monkeypatch, tmp_path, low_paid_prior):
    paid = [row("https://www.python-httpx.org/quickstart/guide", "HTTPX python quickstart"),
            row("https://realpython.test/httpx/client", "Python httpx client"),
            row("https://blog.test/posts/httpx-python", "httpx python tips")]
    free = [repo_row("encode", "httpx", 14000, "A next generation HTTP client for Python.", "Python")]
    install(monkeypatch, Fake("free", free, vertical="code", delay=0.05), Fake("exa", paid, paid=True, cost=0.005))
    result = await run(tmp_path, query="httpx python github", limit=3, policy=paid_policy())
    assert urls(result) == [paid[0]["url"], paid[1]["url"], "https://github.com/encode/httpx"]
    anchor = anchor_of(result)
    assert anchor["kept"] == 2 and anchor["filled"] == 1
    assert anchor["replaced"] == [{"url": paid[2]["url"], "reason": "displaced_by_canonical"}]


async def test_a_canonical_free_repo_takes_the_first_removed_slot(monkeypatch, tmp_path, low_paid_prior):
    paid = [row("https://www.python-httpx.org/quickstart/guide", "HTTPX python quickstart"),
            row("https://gossip.test/a/1", "celebrity gossip"),
            row("https://blog.test/posts/httpx-python", "httpx python tips")]
    free = [repo_row("encode", "httpx", 14000, "A next generation HTTP client for Python.", "Python")]
    install(monkeypatch, Fake("free", free, vertical="code", delay=0.05), Fake("exa", paid, paid=True, cost=0.005))
    result = await run(tmp_path, query="httpx python github", limit=3, policy=paid_policy())
    assert urls(result) == [paid[0]["url"], "https://github.com/encode/httpx", paid[2]["url"]]
    assert anchor_of(result)["replaced"] == [{"url": paid[1]["url"], "reason": "off_query"}]


def test_a_canonical_the_paid_set_already_has_is_not_added_again_and_a_reference_topic_counts():
    repo = annotate([repo_row("encode", "httpx", 14000, language="Python")], check_results(
        "httpx python github", [repo_row("encode", "httpx", 14000, language="Python")], limit=3, now=NOW, vertical="code"))
    other = annotate([row("https://b.test/httpx/guide", "httpx python guide")], check_results(
        "httpx python github", [row("https://b.test/httpx/guide", "httpx python guide")], limit=3, now=NOW))
    answer, receipt = anchored_answer("httpx python github", "exa", repo, repo + other, limit=2, vertical="code")
    assert [item["url"] for item in answer] == ["https://github.com/encode/httpx", "https://b.test/httpx/guide"]
    assert receipt["replaced"] == [] and receipt["filled"] == 1
    # A reference question: the article titled for the topic is the canonical result.
    paid = annotate([row("https://a.test/sam/1", "Samsung Galaxy news"), row("https://a.test/sam/2", "Samsung phones")],
                    check_results("who founded Samsung", [row("https://a.test/sam/1", "Samsung Galaxy news"),
                                                          row("https://a.test/sam/2", "Samsung phones")],
                                  limit=3, now=NOW, vertical="reference"))
    wiki = annotate([row("https://en.wikipedia.org/wiki/Samsung", "Samsung")], check_results(
        "who founded Samsung", [row("https://en.wikipedia.org/wiki/Samsung", "Samsung")], limit=3, now=NOW,
        vertical="reference"))
    answer, receipt = anchored_answer("who founded Samsung", "exa", paid, wiki, limit=2, vertical="reference")
    assert answer[-1]["url"] == "https://en.wikipedia.org/wiki/Samsung" and answer[0]["url"] == "https://a.test/sam/1"
    assert receipt["replaced"] == [{"url": "https://a.test/sam/2", "reason": "displaced_by_canonical"}]
    # Not a name lookup or a reference topic: nothing is placed over a full paid set.
    full, receipt = anchored_answer("rust async", "exa", paid, wiki, limit=2)
    assert [item["url"] for item in full] == [item["url"] for item in paid] and receipt["filled"] == 0


# --- no paid answer: free merging is as it was ----------------------------------------------------------------

def old_order(query, groups, **kwargs):
    """The free ordering: each source's passing results by relevance, merged by reciprocal rank."""
    kept = []
    for name, rows in groups:
        kept.append((name, accepted(rows, check_results(query, rows, limit=10, now=NOW, **kwargs))))
    return [item["url"] for item in (kept[0][1] if len(kept) == 1 else merge_results(kept, windowed=bool(kwargs)))]


def mixed(prefix):
    return [row("https://%s1.test/x/1" % prefix, "Tokio runtime", "rust async"),         # snippet only
            row("https://%s2.test/rust/story-2" % prefix, "rust async story two"),
            row("https://%s3.test/rust/story-3" % prefix, "rust async story three")]


async def test_without_paid_allowed_the_free_ordering_is_unchanged(monkeypatch, tmp_path):
    install(monkeypatch, Fake("free", mixed("f")), Fake("exa", mixed("e"), paid=True, cost=0.005))
    result = await run(tmp_path, limit=3, policy=WebPolicy(provider="http"))
    assert CALLS == ["free"]
    assert urls(result) == old_order("rust async", [("free", mixed("f"))])
    assert urls(result)[0] != "https://f1.test/x/1"          # relevance reordered it: the source order is not kept
    assert "anchor" not in result["receipt"]["search_ladder"]


async def test_merged_free_sources_keep_the_old_ordering(monkeypatch, tmp_path):
    install(monkeypatch, Fake("one", mixed("a")[1:]), Fake("two", mixed("b")[:2]))
    result = await run(tmp_path, limit=5, policy=WebPolicy(provider="http"))
    assert CALLS == ["one", "two"]
    assert urls(result) == old_order("rust async", [("one", mixed("a")[1:]), ("two", mixed("b")[:2])])
    assert "anchor" not in result["receipt"]["search_ladder"]


async def test_a_paid_source_that_fails_leaves_the_free_behaviour(monkeypatch, tmp_path):
    install(monkeypatch, Fake("free", mixed("f"), delay=0.05), Fake("exa", mixed("e"), paid=True, cost=0, fail=True))
    result = await run(tmp_path, recency="week", limit=3, policy=paid_policy())
    assert "exa" in CALLS
    ladder = result["receipt"]["search_ladder"]
    assert "anchor" not in ladder
    assert urls(result) == old_order("rust async", [("free", mixed("f"))], recency="week")


async def test_a_paid_source_that_times_out_leaves_the_free_behaviour(monkeypatch, tmp_path):
    import frankensurf.runtime as runtime
    # (Changed with search_paid_wait_seconds: the paid source is waited for that long, not the race
    # grace; the timeout now records anchor null with the note paid_timeout.)
    monkeypatch.setattr(runtime, "_paid_priors", lambda: {"exa": {"overall": [0, 0], "cost_usd": 0.005}})
    free = dated(3, "f")
    install(monkeypatch, Fake("free", free), Fake("exa", dated(3, "e"), paid=True, cost=0.005, delay=30))
    result = await run(tmp_path, recency="week", limit=3, policy=paid_policy(search_paid_wait_seconds=0.3))
    assert "exa" in CALLS
    ladder = result["receipt"]["search_ladder"]
    assert ladder["anchor"] is None and ladder["notes"] == ["paid_timeout"]
    assert result["receipt"]["cost_usd"] == 0.005
    assert [url[8] for url in urls(result)] == ["f", "f", "f"]
    statuses = {attempt["source"]: attempt["status"] for attempt in result["receipt"]["search_attempts"]}
    assert statuses["exa"] == "cancelled"


def test_market_blurbs_with_a_ticker_and_a_trading_word_or_a_blurb_phrase():
    from frankensurf.search_checks import stock_blurb
    for title in ["GE Aerospace (GE) stock sinks as market gains: Here's why",
                  "General Electric Co Stock (GE) Moved Up by 3.07% on Sep 16",
                  "GE Aerospace down 15% in 3 months: Is this a buying opportunity?",
                  "GE Aerospace (GE) is Attracting Investor Attention: Here is What You Should Know",
                  "GE Aerospace (GE) Stock News: Why It's Moving, Sep 2026"]:
        assert stock_blurb(title), title
    for title in ["GE Aerospace ships four more engines for Tejas Mk-1A",
                  "GE Aerospace commits $225M to modernize Niskayuna research center",
                  "Reuters: GE Aerospace says GE9X mid-seal issue won't delay 777X",
                  "Nvidia (NVDA) unveils new GPU architecture at developer conference"]:
        assert not stock_blurb(title), title


def test_a_forge_repository_page_is_never_a_search_page():
    from frankensurf.search_checks import url_kind
    assert url_kind("https://github.com/tanstack/query") == "item"
    assert url_kind("https://github.com/owner/search") == "item"
    assert url_kind("https://github.com/search?q=query") == "search_page"


def test_reserved_forge_paths_are_listings():
    from frankensurf.search_checks import url_kind
    assert url_kind("https://github.com/topics/svelte-kit") == "tag_listing"
    assert url_kind("https://github.com/trending/vue") == "tag_listing"
    assert url_kind("https://github.com/sveltejs/kit") == "item"


def test_kind_and_question_words_are_not_the_topic():
    from frankensurf.search_checks import key_terms
    assert key_terms("eBay latest announcement") == ["ebay"]
    assert key_terms("who wrote The Odyssey") == ["odyssey"]
    assert key_terms("DoorDash newest product launch") == ["doordash"]


def test_an_anchored_answer_keeps_paid_results_that_share_a_term():
    from frankensurf.search_checks import anchored_answer
    paid = [{"url": "https://www.britannica.com/biography/Homer-Greek-poet", "title": "Homer | Greek poet",
             "snippet": "Homer, presumed author of the Iliad and the Odyssey",
             "check": {"passed": False, "reasons": ["off_query"]}},
            {"url": "https://example.org/unrelated", "title": "Something else", "snippet": "nothing",
             "check": {"passed": False, "reasons": ["off_query"]}}]
    results, receipt = anchored_answer("who wrote The Odyssey", "exa", paid, [], limit=5, vertical="reference")
    assert [r["url"] for r in results] == ["https://www.britannica.com/biography/Homer-Greek-poet"]
    assert [r["reason"] for r in receipt["replaced"]] == ["off_query"]
