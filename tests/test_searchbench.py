import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "searchbench", Path(__file__).resolve().parents[1] / "scripts" / "searchbench.py")
searchbench = importlib.util.module_from_spec(SPEC)
sys.modules["searchbench"] = searchbench
SPEC.loader.exec_module(searchbench)

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


def days_ago(days):
    return (NOW - timedelta(days=days)).isoformat()


def query(**fields):
    return {"id": "q", "set": "dev", "category": "news", "query": "q", "entity_terms": ["openai"],
            "needs_specific": True, **fields}


def item(url, title="OpenAI ships a model", snippet="", date=None):
    return {"url": url, "title": title, "snippet": snippet, "published": date}


def page(text="OpenAI announced a model.", title="OpenAI", date=None, status="observed", **extra):
    return {"status": status, "title": title, "text": text, "date": date, **extra}


def test_terms_need_every_entry_and_respect_word_edges():
    assert searchbench.terms_match(["pandas", "merge"], "How to merge DataFrames in Pandas")
    assert not searchbench.terms_match(["pandas", "merge"], "pandas groupby")
    assert not searchbench.terms_match(["meta"], "metadata standards")
    assert searchbench.terms_match(["agent"], "LLM agents survey")
    assert searchbench.terms_match(["aws|amazon web services"], "Amazon Web Services re:Invent")
    assert searchbench.terms_match(["validat*"], "Data validation in Python")
    assert not searchbench.terms_match([], "anything")


def test_listing_pages_are_not_specific():
    assert searchbench.is_listing("https://example.com/")
    assert searchbench.is_listing("https://example.com/en-us/")
    assert searchbench.is_listing("https://example.com/news")
    assert searchbench.is_listing("https://example.com/news/")
    assert searchbench.is_listing("https://example.com/tag/openai")
    assert searchbench.is_listing("https://example.com/blog/page/2")
    assert searchbench.is_listing("https://example.com/find?q=openai")
    assert not searchbench.is_listing("https://example.com/news/openai-ships-model")
    assert not searchbench.is_listing("https://github.com/pandas-dev/pandas")
    assert not searchbench.is_listing("https://en.wikipedia.org/wiki/Alan_Turing")


def test_challenge_and_error_pages():
    assert searchbench.is_challenge("Just a moment...", "")
    assert searchbench.is_challenge("Attention Required! | Cloudflare", "")
    assert searchbench.is_challenge("404 Not Found", "")
    assert searchbench.is_challenge("Page not found", "")
    assert searchbench.is_challenge("Site", "Please verify you are human to continue")
    assert searchbench.is_challenge("Site", "Checking your browser before accessing the site")
    assert not searchbench.is_challenge("Error handling in Python", "Exceptions are raised when...")
    assert not searchbench.is_challenge("OpenAI news", "OpenAI released a new model today. " * 40)


def test_date_from_meta_jsonld_and_time_tags():
    meta = '<html><head><meta property="article:published_time" content="2026-10-09T08:00:00Z"></head></html>'
    when, source = searchbench.extract_date(meta)
    assert when.date().isoformat() == "2026-10-09" and source == "meta:article:published_time"
    ld = ('<script type="application/ld+json">{"@graph": [{"@type": "WebSite"}, '
          '{"@type": "NewsArticle", "datePublished": "2026-10-01T00:00:00+00:00"}]}</script>')
    assert searchbench.extract_date(ld)[1] == "jsonld"
    assert searchbench.extract_date(ld)[0].day == 1
    assert searchbench.extract_date('<p><time datetime="2026-09-30">Sep 30</time></p>')[1] == "time"
    assert searchbench.extract_date('<meta name="citation_date" content="2026-10-02">')[1] == "meta:citation_date"
    assert searchbench.extract_date("<p>no dates</p>") == (None, None)
    assert searchbench.extract_date(None) == (None, None)
    assert searchbench.extract_date('<script type="application/ld+json">{broken</script>') == (None, None)


def test_window_uses_recency_days_and_rejects_unknown():
    assert searchbench.in_window(None, None, NOW) is None
    assert searchbench.in_window(NOW - timedelta(days=3), "week", NOW) is True
    assert searchbench.in_window(NOW - timedelta(days=10), "week", NOW) is False
    assert searchbench.in_window(NOW - timedelta(days=10), "month", NOW) is True
    assert searchbench.in_window(NOW + timedelta(days=30), "month", NOW) is False
    assert searchbench.in_window(None, "year", NOW) is False


def test_right_result_passes_every_check():
    verdict = searchbench.judge(query(recency="week"), item("https://news.example/openai-ships", date=days_ago(2)),
                                page(), NOW)
    assert verdict["right"] and verdict["why"] == [] and verdict["date_source"] == "result"


def test_off_query_is_wrong_unless_the_page_mentions_the_entity():
    off = item("https://news.example/a", title="Weather today", snippet="rain")
    assert searchbench.judge(query(), off, None, NOW)["why"] == ["off_query"]
    assert searchbench.judge(query(), off, page(text="A story about OpenAI."), NOW)["on_query"] == "page"
    assert not searchbench.judge(query(), off, page(title="Rain", text="A story about rain."), NOW)["right"]


def test_date_comes_from_the_result_then_the_page_and_unknown_fails():
    q = query(recency="week")
    url = "https://news.example/openai-ships"
    assert searchbench.judge(q, item(url), None, NOW)["why"] == ["date_unverified"]
    from_page = searchbench.judge(q, item(url), page(date=days_ago(1)), NOW)
    assert from_page["right"] and from_page["date_source"] == "page"
    assert searchbench.judge(q, item(url), page(date=days_ago(40)), NOW)["why"] == ["outside_window"]
    # A result that carries its own date is not second-guessed by the page.
    old = searchbench.judge(q, item(url, date=days_ago(40)), page(date=days_ago(1)), NOW)
    assert old["why"] == ["outside_window"]


def test_listing_matters_only_when_a_specific_page_is_needed():
    home = item("https://example.com/")
    assert searchbench.judge(query(), home, page(), NOW)["why"] == ["listing"]
    assert searchbench.judge(query(needs_specific=False), home, page(), NOW)["right"]


def test_challenge_page_is_wrong_even_when_the_snippet_matches():
    url = "https://news.example/openai-ships"
    wall = page(title="Just a moment...", text="Checking your browser")
    assert "challenge" in searchbench.judge(query(), item(url), wall, NOW)["why"]
    gone = {"status": "failed", "failure": "NOT_FOUND", "http_status": 404}
    assert "challenge" in searchbench.judge(query(), item(url), gone, NOW)["why"]
    walled = {"status": "failed", "failure": "WALL_DETECTED", "http_status": 403}
    assert searchbench.judge(query(), item(url), walled, NOW)["right"]  # unread, not proven wrong


def test_needs_read_skips_results_already_ruled_out():
    q = query(recency="week")
    assert searchbench.needs_read(q, item("https://news.example/openai-ships"), NOW)
    assert not searchbench.needs_read(q, item("https://news.example/"), NOW)
    assert not searchbench.needs_read(q, item("https://news.example/openai-ships", date=days_ago(90)), NOW)
    assert not searchbench.needs_read(q, item("https://news.example/x", title="Just a moment..."), NOW)


def test_score_hit_precision_and_expect_domain():
    results = [item("https://news.example/openai-1", date=days_ago(1)),
               item("https://news.example/", date=days_ago(1)),
               item("https://news.example/weather", title="Weather", date=days_ago(1)),
               item("https://news.example/openai-old", date=days_ago(90)),
               item("https://openai.com/blog/x", date=days_ago(2)),
               item("https://news.example/sixth", date=days_ago(1))]
    q = query(recency="week", expect_domains=["openai.com"])
    scored = searchbench.score(q, results, {}, NOW)
    assert len(scored["verdicts"]) == 5
    assert scored["hit"] and scored["right"] == 2 and scored["precision"] == 0.4
    assert scored["domain_hit"] is True
    assert searchbench.score(query(), [], {}, NOW) == {"verdicts": [], "hit": False, "right": 0,
                                                       "precision": 0.0, "domain_hit": None}
    assert searchbench.score(query(expect_domains=["openai.com"]), results[:2], {}, NOW)["domain_hit"] is False


def test_subdomains_count_for_expect_domains():
    results = [item("https://en.wikipedia.org/wiki/OpenAI")]
    assert searchbench.score(query(expect_domains=["wikipedia.org"]), results, {}, NOW)["domain_hit"] is True


def test_mcnemar_exact_matches_known_values():
    assert searchbench.mcnemar_exact(0, 0) == 1.0
    assert searchbench.mcnemar_exact(5, 5) == 1.0
    assert abs(searchbench.mcnemar_exact(0, 10) - 2 / 1024) < 1e-12
    assert abs(searchbench.mcnemar_exact(3, 9) - 2 * 299 / 4096) < 1e-12


def rows_for():
    def row(arm, qid, hit, category="news", precision=None):
        return {"arm": arm, "query_id": qid, "category": category, "hit": hit, "status": "observed",
                "precision": precision if precision is not None else float(hit), "domain_hit": None,
                "n_results": 5, "latency_ms": 100, "cost_usd": 0.0}
    return [row("a", "q1", True), row("a", "q2", True), row("a", "q3", False, "code"),
            row("b", "q1", True), row("b", "q2", False), row("b", "q3", False, "code"), row("b", "q4", True)]


def test_compare_pairs_only_shared_queries():
    result = searchbench.compare(rows_for(), "a", "b")
    assert (result["paired"], result["only_a"], result["only_b"], result["both"], result["neither"]) == (3, 1, 0, 1, 1)
    assert result["p_value"] == 1.0


def test_summary_per_arm_and_category():
    summary = searchbench.summarize(rows_for())
    assert summary["arms"]["a"]["hit"] == round(2 / 3, 3)
    assert summary["by_category"]["a"]["code"]["hit"] == 0.0
    assert summary["arms"]["b"]["queries"] == 4


def test_interleave_touches_every_category_first():
    queries = [{"id": f"{c}{i}", "category": c} for c in "abc" for i in range(3)]
    assert [q["category"] for q in searchbench.interleave(queries)[:3]] == ["a", "b", "c"]


def test_query_file_is_balanced_and_well_formed():
    data = json.loads(searchbench.QUERIES.read_text(encoding="utf-8"))
    queries = data["queries"]
    assert data["shuffle_seed"] and len({q["id"] for q in queries}) == len(queries)
    assert {q["category"] for q in queries} == {"news", "announcement", "howto", "paper", "code", "fact"}
    assert {q["set"] for q in queries} == {"dev", "heldout", "blind2", "blind3", "blind4", "blind5", "blind6"}
    for q in queries:
        assert q["entity_terms"] and isinstance(q["needs_specific"], bool)
        assert q.get("recency", "week") in searchbench.RECENCY
        assert q["query"].strip()


def test_reader_caches_by_url_and_extracts_the_date(tmp_path):
    import asyncio

    class Web:
        calls = 0

        async def read(self, url, policy_overrides=None):
            Web.calls += 1
            html = '<meta property="article:published_time" content="2026-10-08T00:00:00Z">'
            return {"title": "T", "text": "body", "content": html, "content_type": "text/html",
                    "receipt": {"status": "observed"}}

    async def go():
        reader = searchbench.Reader(Web(), tmp_path, gap=0, concurrency=2, provider="http")
        first, second, third = await asyncio.gather(*(reader.get("https://x.example/a") for _ in range(3)))
        again = searchbench.Reader(Web(), tmp_path, gap=0, concurrency=2, provider="http")
        return first, await again.get("https://x.example/a")

    first, cached = asyncio.run(go())
    assert Web.calls == 1 and first == cached
    assert first["date"].startswith("2026-10-08") and first["status"] == "observed"
