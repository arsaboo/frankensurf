"""The search ladder: checks decide when a search is done, free sources climb,
paid ones only when allowed, slow ones are raced, and the receipt says what happened."""
import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from frankensurf import Runtime, WebPolicy
from frankensurf.search_plugins import SearchManifest, SearchRegistry

CALLS = []
SEEN_POLICY = {}


def good(count, prefix="s", words="rust async", date=None):
    return [{"url": "https://%s%d.test/rust/story-%d" % (prefix, index, index), "title": words + " story %d" % index,
             "snippet": "", "published": date} for index in range(count)]


class Fake:
    """A search source with a fixed answer, an optional delay and a cost."""

    def __init__(self, identifier, results=(), *, paid=False, cost=0, delay=0, fail=False, vertical="web",
                 transport=None):
        self.manifest = SearchManifest(identifier, "test", paid=paid, cost_bounded=True, vertical=vertical,
                                       transport_provider=transport)
        self.results, self.cost, self.delay, self.fail = list(results), cost, delay, fail

    async def search(self, request, services):
        CALLS.append(self.manifest.id)
        SEEN_POLICY[self.manifest.id] = (request.policy.provider, request.policy.provider_candidates)
        if self.delay:
            await asyncio.sleep(self.delay)
        failed = self.fail
        status = "failed" if failed else "observed"
        failure = {"code": "PROVIDER_DOWN", "message": "failed"} if failed else None
        attempt = {"provider": self.manifest.id + "-provider", "provider_version": "1", "status": status,
                   "cost_usd": self.cost, "latency_ms": 1}
        if failed:
            attempt["failure"] = "PROVIDER_DOWN"
        receipt = {"trace_id": hashlib.sha256(self.manifest.id.encode()).hexdigest()[:32], "operation": "search",
                   "status": status, "failure": failure, "cost_usd": self.cost, "latency_ms": 1,
                   "attempts": [attempt], "identity": None, "evidence": [],
                   "requested_url": "https://search.test/", "final_url": "https://search.test/"}
        attribution = {"query": request.query, "adapter": self.manifest.id, "source": self.manifest.id,
                       "source_version": "test", "request_url": "https://search.test/"}
        rows = [] if failed else [{"snippet": "", "indexed_date": None, **row, "engines": [self.manifest.id], "listing_state": "unknown",
                                   "verification": "indexed_discovery", "query_attribution": attribution}
                                  for row in self.results[:request.limit]]
        response = {"query": request.query, "source": self.manifest.id, "results": rows, "receipt": receipt,
                    "query_attribution": attribution, "coverage": "unknown" if failed else "returned-results",
                    "upstream_failures": []}
        return {"response": response, "acquisition": {"url": "https://search.test/", "content": "", "text": "",
                                                      "structured": {}, "image_urls": [], "receipt": receipt}}


@pytest.fixture(autouse=True)
def fresh_calls():
    CALLS.clear()


def install(monkeypatch, *plugins):
    import frankensurf.search_plugins as plugins_module
    registry = SearchRegistry()
    for plugin in plugins:
        registry.register(plugin)
    monkeypatch.setattr(plugins_module, "DEFAULT_SEARCHES", registry)


async def run(tmp_path, query="rust async", policy=None, **kwargs):
    async with Runtime(tmp_path) as web:
        return await web.search(query, policy=policy or WebPolicy(provider="http"), **kwargs)


async def test_stops_at_the_first_source_whose_set_passes_the_checks(monkeypatch, tmp_path):
    install(monkeypatch, Fake("one", good(4)), Fake("two", good(4, "t")))
    result = await run(tmp_path)
    assert CALLS == ["one"]
    assert result["source"] == "one" and len(result["results"]) == 4
    ladder = result["receipt"]["search_ladder"]
    assert ladder["winner"] == "one" and ladder["stopped"] == "good" and ladder["enabled"] is True
    assert result["receipt"]["search_checks"]["verdict"] == "good"


async def test_climbs_past_a_weak_set_to_a_good_one(monkeypatch, tmp_path):
    install(monkeypatch, Fake("weak", good(1)), Fake("strong", good(5, "t")), Fake("never", good(5, "u")))
    result = await run(tmp_path)
    assert CALLS == ["weak", "strong"]
    assert result["receipt"]["search_ladder"]["winner"] == "strong"
    assert [row["url"].split("/")[2] for row in result["results"]][0] == "t0.test"
    attempts = result["receipt"]["search_attempts"]
    assert [(row["source"], row["count"], row["passed"], row["check"]) for row in attempts] == [
        ("weak", 1, 1, "weak"), ("strong", 5, 5, "good")]


async def test_weak_sets_that_pass_together_are_merged_and_deduplicated(monkeypatch, tmp_path):
    shared = good(1, "shared")
    install(monkeypatch, Fake("one", shared + good(1, "a")), Fake("two", shared + good(1, "b")),
            Fake("never", good(5, "z")))
    result = await run(tmp_path)
    assert CALLS == ["one", "two"]
    urls = [row["url"] for row in result["results"]]
    assert len(urls) == 3 and len(set(urls)) == 3
    assert urls[0] == shared[0]["url"]  # two sources agree on it, so it rises
    ladder = result["receipt"]["search_ladder"]
    assert ladder["winner"] == "merged" and ladder["stopped"] == "good"
    assert result["receipt"]["search_checks"]["verdict"] == "good"
    assert result["results"][0]["fusion"]["sources"] == [{"source": "one", "rank": 1}, {"source": "two", "rank": 1}]


async def test_exhausted_ladder_returns_the_merged_passing_results_as_weak(monkeypatch, tmp_path):
    install(monkeypatch, Fake("one", good(1, "a")), Fake("two", [{"url": "https://b0.test/x/1", "title": "unrelated topic"}]),
            Fake("down", fail=True))
    result = await run(tmp_path)
    assert CALLS == ["one", "two", "down"]
    assert len(result["results"]) == 1
    assert result["receipt"]["search_ladder"]["stopped"] == "exhausted"
    assert result["receipt"]["search_checks"]["verdict"] == "weak"
    assert result["receipt"]["search_checks"]["reasons"] == {"off_query": 1}
    assert result["receipt"]["status"] == "observed"


async def test_off_query_homepage_and_challenge_results_are_dropped(monkeypatch, tmp_path):
    junk = [{"url": "https://home.test/", "title": "rust async home", "snippet": ""},
            {"url": "https://gossip.test/x/1", "title": "celebrity news", "snippet": ""},
            {"url": "https://wall.test/rust/async", "title": "Just a moment... rust", "snippet": ""},
            {"url": "https://tags.test/tag/rust-async", "title": "rust async", "snippet": ""}]
    install(monkeypatch, Fake("one", junk + good(3)))
    result = await run(tmp_path)
    assert [row["url"] for row in result["results"]] == [row["url"] for row in good(3)]
    reasons = result["receipt"]["search_checks"]["reasons"]
    assert reasons == {"homepage": 1, "off_query": 1, "challenge_page": 1, "tag_listing": 1}
    first = result["receipt"]["search_checks"]["results"][0]
    assert set(first) == {"url", "source", "in_window", "on_query", "published", "date_source", "kind"}
    assert first["on_query"] is True and first["source"] == "one"


async def test_recency_window_drops_old_results_and_ranks_unknown_dates_last(monkeypatch, tmp_path):
    now = datetime.now(timezone.utc)
    fresh = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    old = (now - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [{"url": "https://u.test/rust/1", "title": "rust async", "snippet": "no date"},
            {"url": "https://o.test/rust/2", "title": "rust async", "snippet": "", "published": old},
            {"url": "https://f.test/rust/3", "title": "rust async", "snippet": "", "published": fresh},
            {"url": "https://g.test/rust/4", "title": "rust async", "snippet": "", "published": fresh},
            {"url": "https://w.test/rust/5", "title": "rust async", "snippet": "6 months ago"}]
    install(monkeypatch, Fake("one", rows))
    result = await run(tmp_path, recency="week", policy=WebPolicy(provider="http", search_hold_undated=False))
    urls = [row["url"] for row in result["results"]]
    assert urls == ["https://f.test/rust/3", "https://g.test/rust/4", "https://u.test/rust/1"]
    assert [row["check"]["in_window"] for row in result["results"]] == [True, True, None]
    checks = result["receipt"]["search_checks"]
    assert checks["reasons"] == {"out_of_window": 1}  # "6 months ago"; the 90-day-old one was filtered earlier
    assert result["receipt"]["search_options"]["filtered"]["too_old"] == 1
    assert checks["unknown_dates"] == 1
    assert result["receipt"]["search_checks"]["results"][0]["in_window"] is True


async def test_ladder_off_keeps_the_first_answer_and_still_annotates(monkeypatch, tmp_path):
    install(monkeypatch, Fake("one", good(1)), Fake("two", good(5, "t")))
    result = await run(tmp_path, policy=WebPolicy(provider="http", search_ladder=False))
    assert CALLS == ["one"] and result["source"] == "one"
    assert result["results"][0]["check"]["passed"] is True
    assert result["receipt"]["search_ladder"]["enabled"] is False


async def test_explicit_source_never_climbs_and_keeps_all_results(monkeypatch, tmp_path):
    rows = good(1) + [{"url": "https://x.test/y/1", "title": "unrelated", "snippet": ""}]
    install(monkeypatch, Fake("one", rows), Fake("two", good(5, "t")))
    result = await run(tmp_path, source="one")
    assert CALLS == ["one"] and len(result["results"]) == 2
    assert [row["check"]["passed"] for row in result["results"]] == [True, False]


async def test_failed_sources_are_skipped_and_recorded(monkeypatch, tmp_path):
    install(monkeypatch, Fake("down", fail=True), Fake("up", good(3)))
    result = await run(tmp_path)
    assert CALLS == ["down", "up"]
    assert [(row["source"], row["status"]) for row in result["receipt"]["search_attempts"]] == [
        ("down", "failed"), ("up", "observed")]


# --- paid sources ------------------------------------------------------------

async def test_paid_sources_are_not_tried_without_the_opt_in(monkeypatch, tmp_path):
    install(monkeypatch, Fake("free", good(1)), Fake("paid", good(5, "p"), paid=True, cost=0.01))
    result = await run(tmp_path)
    assert CALLS == ["free"] and result["receipt"]["cost_usd"] == 0
    assert len(result["results"]) == 1


async def test_paid_sources_join_after_weak_free_ones_when_allowed(monkeypatch, tmp_path):
    install(monkeypatch, Fake("free_a", good(1)), Fake("free_b", [{"url": "https://b0.test/x/1", "title": "off topic"}]),
            Fake("free_c", good(1, "c")), Fake("paid", good(5, "p"), paid=True, cost=0.01))
    policy = WebPolicy(provider="http", allow_paid_fallbacks=True, search_paid_after_weak=2,
                       search_hedge_after_seconds=0)
    result = await run(tmp_path, policy=policy)
    assert CALLS == ["free_a", "free_b", "paid"]
    assert result["receipt"]["search_ladder"]["order"] == ["free_a", "free_b", "paid", "free_c"]
    assert result["receipt"]["search_ladder"]["winner"] == "paid"
    assert result["receipt"]["cost_usd"] == 0.01


async def test_paid_after_weak_zero_waits_for_every_free_source(monkeypatch, tmp_path):
    install(monkeypatch, Fake("free_a", good(1)), Fake("free_b", good(1, "b")),
            Fake("paid", good(5, "p"), paid=True, cost=0.01))
    policy = WebPolicy(provider="http", allow_paid_fallbacks=True, search_paid_after_weak=0)
    result = await run(tmp_path, policy=policy)
    assert result["receipt"]["search_ladder"]["order"] == ["free_a", "free_b", "paid"]


async def test_paid_sources_are_ordered_by_cost_per_likely_success(monkeypatch, tmp_path):
    import frankensurf.runtime as runtime
    monkeypatch.setattr(runtime, "_paid_priors", lambda: {"cheap": {"overall": [0, 0], "cost_usd": 0.001},
                                                          "dear": {"overall": [0, 0], "cost_usd": 0.01},
                                                          "unknown_cost": {"overall": [0, 0], "cost_usd": None}})
    install(monkeypatch, Fake("unknown_cost", good(1), paid=True), Fake("dear", good(1, "d"), paid=True),
            Fake("cheap", good(5, "c"), paid=True, cost=0.001))
    policy = WebPolicy(provider="http", allow_paid_fallbacks=True, search_paid_after_weak=0)
    result = await run(tmp_path, policy=policy)
    assert result["receipt"]["search_ladder"]["paid_order"] == ["cheap", "dear", "unknown_cost"]
    assert CALLS == ["cheap"]


async def test_a_paid_source_that_usually_passes_outranks_one_that_usually_does_not(monkeypatch, tmp_path):
    import frankensurf.runtime as runtime
    monkeypatch.setattr(runtime, "_paid_priors", lambda: {"a": {"overall": [0, 0], "cost_usd": 0.01},
                                                          "b": {"overall": [0, 0], "cost_usd": 0.01}})
    install(monkeypatch, Fake("a", good(1), paid=True), Fake("b", good(5), paid=True))
    (tmp_path / "search-stats.json").write_text(json.dumps({"web|any|open": {"a": [0, 8, 0], "b": [8, 8, 0]}}))
    policy = WebPolicy(provider="http", allow_paid_fallbacks=True, search_paid_after_weak=0)
    result = await run(tmp_path, policy=policy)
    assert result["receipt"]["search_ladder"]["paid_order"] == ["b", "a"] and CALLS == ["b"]


async def test_paid_sources_respect_the_cost_cap(monkeypatch, tmp_path):
    install(monkeypatch, Fake("free", good(1)), Fake("paid", good(5, "p"), paid=True, cost=0.5))
    policy = WebPolicy(provider="http", allow_paid_fallbacks=True, max_cost_usd=0.1, search_paid_after_weak=0)
    result = await run(tmp_path, policy=policy)
    # The plugin spent more than the cap: the registry refuses its answer.
    assert result["receipt"]["cost_usd"] is not None
    assert result["receipt"]["search_attempts"][-1]["source"] == "paid"
    assert result["receipt"]["search_attempts"][-1]["status"] == "failed"
    assert len(result["results"]) == 1


# --- racing and caps ---------------------------------------------------------

async def test_slow_source_is_raced_by_the_next_free_one(monkeypatch, tmp_path):
    install(monkeypatch, Fake("slow", good(5), delay=5), Fake("quick", good(5, "q")))
    policy = WebPolicy(provider="http", search_hedge_after_seconds=0.1)
    result = await asyncio.wait_for(run(tmp_path, policy=policy), 4)
    ladder = result["receipt"]["search_ladder"]
    assert ladder["winner"] == "quick" and ladder["hedged"] == ["quick"]
    attempts = {row["source"]: row for row in result["receipt"]["search_attempts"]}
    assert attempts["quick"]["raced"] is True
    assert attempts["slow"]["status"] == "cancelled"


async def test_hedging_off_waits_for_the_slow_source(monkeypatch, tmp_path):
    install(monkeypatch, Fake("slow", good(5), delay=0.3), Fake("quick", good(5, "q")))
    policy = WebPolicy(provider="http", search_hedge_after_seconds=0)
    result = await run(tmp_path, policy=policy)
    assert CALLS == ["slow"] and result["receipt"]["search_ladder"]["winner"] == "slow"
    assert result["receipt"]["search_ladder"]["hedged"] == []


async def test_paid_sources_are_never_raced(monkeypatch, tmp_path):
    install(monkeypatch, Fake("slow", good(1), delay=0.4), Fake("paid", good(5, "p"), paid=True, cost=0.01))
    policy = WebPolicy(provider="http", allow_paid_fallbacks=True, search_hedge_after_seconds=0.05,
                       search_paid_after_weak=1)
    result = await run(tmp_path, policy=policy)
    assert result["receipt"]["search_ladder"]["hedged"] == []
    assert CALLS == ["slow", "paid"]


async def test_first_set_to_pass_wins_a_race_even_if_the_earlier_source_is_weak(monkeypatch, tmp_path):
    install(monkeypatch, Fake("slow_weak", good(1), delay=0.4), Fake("quick_good", good(4, "q")))
    policy = WebPolicy(provider="http", search_hedge_after_seconds=0.05)
    result = await run(tmp_path, policy=policy)
    assert result["receipt"]["search_ladder"]["winner"] == "quick_good"
    assert result["receipt"]["search_attempts"][-1]["status"] == "cancelled"


async def test_time_cap_stops_a_search_stuck_on_a_slow_source(monkeypatch, tmp_path):
    install(monkeypatch, Fake("stuck", good(5), delay=10), Fake("next", good(5, "n")))
    policy = WebPolicy(provider="http", search_max_seconds=0.3, search_hedge_after_seconds=0)
    result = await asyncio.wait_for(run(tmp_path, policy=policy), 5)
    assert result["receipt"]["search_ladder"]["stopped"] == "time_cap"
    assert CALLS == ["stuck"] and result["results"] == []
    assert result["receipt"]["search_attempts"][0]["status"] == "cancelled"


async def test_source_cap_limits_how_many_sources_are_tried(monkeypatch, tmp_path):
    install(monkeypatch, *(Fake("s%d" % index, good(1, "s%d" % index)) for index in range(5)))
    policy = WebPolicy(provider="http", search_max_sources=2, search_hedge_after_seconds=0)
    result = await run(tmp_path, policy=policy)
    assert CALLS == ["s0", "s1"] and result["receipt"]["search_ladder"]["stopped"] == "source_cap"
    assert len(result["results"]) == 2


async def test_terminal_failure_stops_the_climb(monkeypatch, tmp_path):
    install(monkeypatch, Fake("free_a", good(1)), Fake("paid", good(5, "p"), paid=True, cost=0.5),
            Fake("free_b", good(5, "f")))
    policy = WebPolicy(provider="http", allow_paid_fallbacks=True, max_cost_usd=0.1, search_paid_after_weak=1)
    result = await run(tmp_path, policy=policy)
    # The paid source blew through the cap (BUDGET_EXHAUSTED): nothing further is tried.
    assert CALLS == ["free_a", "paid"]
    assert result["receipt"]["search_ladder"]["stopped"] == "terminal"
    assert len(result["results"]) == 1


# --- learned routing ---------------------------------------------------------

async def test_the_source_that_passed_before_is_tried_first_next_time(monkeypatch, tmp_path):
    install(monkeypatch, Fake("a", good(1)), Fake("b", [{"url": "https://b0.test/x/1", "title": "off topic"}]), Fake("c", good(5, "c")))
    policy = WebPolicy(provider="http", search_hedge_after_seconds=0)
    for _ in range(2):
        await run(tmp_path, policy=policy)
    assert CALLS == ["a", "b", "c", "a", "b", "c"]
    stats = json.loads((tmp_path / "search-stats.json").read_text())["web|any|open"]
    assert stats["c"][:2] == [2, 2] and stats["a"][:2] == [0, 2]
    CALLS.clear()
    third = await run(tmp_path, policy=policy)
    assert CALLS == ["c"]
    assert third["receipt"]["search_ladder"]["order"][0] == "c"
    assert third["receipt"]["search_ladder"]["learned_first"] == "c"


async def test_learning_is_per_query_type(monkeypatch, tmp_path):
    install(monkeypatch, Fake("a", good(1)), Fake("c", good(5, "c")))
    policy = WebPolicy(provider="http", search_hedge_after_seconds=0)
    for _ in range(2):
        await run(tmp_path, policy=policy)
    CALLS.clear()
    other = await run(tmp_path, policy=policy, recency="week", site="rust.test")
    assert other["receipt"]["search_ladder"]["query_type"] == "web|recent|site"
    assert other["receipt"]["search_ladder"]["order"] == ["a", "c"]
    stats = json.loads((tmp_path / "search-stats.json").read_text())
    assert set(stats) == {"web|any|open", "web|recent|site"}


async def test_a_source_that_never_passes_moves_to_the_back(monkeypatch, tmp_path):
    install(monkeypatch, Fake("dud", good(1)), Fake("fine", good(5, "f")))
    (tmp_path / "search-stats.json").write_text(json.dumps({"web|any|open": {"dud": [0, 9, 0]}}))
    result = await run(tmp_path)
    assert result["receipt"]["search_ladder"]["order"] == ["fine", "dud"]
    assert result["receipt"]["search_ladder"]["learned_last"] == ["dud"]


async def test_preferred_and_explicit_routes_are_not_reordered_by_learning(monkeypatch, tmp_path):
    install(monkeypatch, Fake("a", good(1)), Fake("b", good(1, "b")), Fake("c", good(5, "c")))
    (tmp_path / "search-stats.json").write_text(json.dumps({"web|any|open": {"c": [9, 9, 0]}}))
    policy = WebPolicy(provider="http", search_source_candidates=("a", "b", "c"), search_hedge_after_seconds=0)
    result = await run(tmp_path, policy=policy)
    assert result["receipt"]["search_ladder"]["order"] == ["a", "b", "c"]
    policy = WebPolicy(provider="http", search_source_prefer=("b",), search_hedge_after_seconds=0)
    result = await run(tmp_path, policy=policy)
    assert result["receipt"]["search_ladder"]["order"][:2] == ["b", "c"]


async def test_learning_can_be_turned_off(monkeypatch, tmp_path):
    install(monkeypatch, Fake("a", good(5)))
    await run(tmp_path, policy=WebPolicy(provider="http", search_learned_routing=False))
    assert not (tmp_path / "search-stats.json").exists()


# --- receipt shape and exclusion ---------------------------------------------

async def test_receipt_shape(monkeypatch, tmp_path):
    install(monkeypatch, Fake("weak", good(1)), Fake("strong", good(3, "t", date="2026-10-11T00:00:00Z")))
    result = await run(tmp_path)
    receipt = result["receipt"]
    assert receipt["search_routing"]["candidates"] == ["weak", "strong"]  # existing field, unchanged
    assert receipt["search_ladder"] == {"enabled": True, "query_type": "web|any|open", "order": ["weak", "strong"],
                                        "winner": "strong", "stopped": "good", "hedged": []}
    checks = receipt["search_checks"]
    assert {key: checks[key] for key in ("verdict", "need", "passed", "reasons", "unknown_dates")} == {
        "verdict": "good", "need": 3, "passed": 3, "reasons": {}, "unknown_dates": 0}
    assert checks["results"][0] == {"url": "https://t0.test/rust/story-0", "source": "strong", "in_window": None,
                                    "on_query": True, "published": "2026-10-11T00:00:00Z",
                                    "date_source": "source", "kind": "item"}
    attempt = receipt["search_attempts"][1]
    assert {"source", "status", "cost_usd", "latency_ms", "count", "passed", "check"} <= set(attempt)
    json.dumps(result)


async def test_long_exclude_list_is_post_filtered_and_the_limit_still_met(monkeypatch, tmp_path):
    domains = ["bad%02d.example" % index for index in range(36)]
    seen = {}

    class Recording(Fake):
        async def search(self, request, services):
            seen["config"], seen["limit"] = request.config, request.limit
            return await super().search(request, services)
    rows = [{"url": "https://sub.bad%02d.example/rust/%d" % (index % 36, index), "title": "rust async",
             "snippet": ""} for index in range(10)] + good(5, "ok")
    install(monkeypatch, Recording("one", rows))
    result = await run(tmp_path, limit=5, exclude_domains=domains)
    assert seen["limit"] == 20 and seen["config"]["exclude_domains"] == sorted(domains)
    assert [row["url"] for row in result["results"]] == [row["url"] for row in good(5, "ok")]
    assert result["receipt"]["search_options"]["filtered"]["excluded"] == 10


async def test_exclude_operator_cap_policy_reaches_http_engines(tmp_path):
    import httpx
    queries = []

    def handle(request):
        queries.append(request.url.params["q"])
        return httpx.Response(200, json={"results": [
            {"url": "https://x%d.test/rust/%d" % (index, index), "title": "rust async"} for index in range(5)]})
    domains = ["bad%02d.example" % index for index in range(36)]
    async with Runtime(tmp_path, transport=httpx.MockTransport(handle)) as web:
        await web.search("rust async", source="searxng", exclude_domains=domains, policy=WebPolicy(provider="http"))
        await web.search("rust async", source="searxng", exclude_domains=domains,
                         policy=WebPolicy(provider="http", search_exclude_operators=5))
    assert [query.count("-site:") for query in queries] == [3, 5]


@pytest.mark.parametrize("field,value", [("search_hedge_racers", 0), ("search_hedge_racers", 4),
                                         ("search_max_seconds", 0), ("search_max_sources", 0),
                                         ("search_good_results", 0), ("search_paid_after_weak", -1),
                                         ("search_exclude_operators", -1), ("search_ladder", 1),
                                         ("search_learned_routing", "yes"), ("search_hedge_after_seconds", -1),
                                         ("search_date_reads", -1), ("search_date_reads", 21),
                                         ("search_date_reads", True), ("search_date_read_timeout_seconds", 0)])
def test_policy_validates_ladder_settings(field, value):
    with pytest.raises(ValueError):
        WebPolicy(**{field: value})


def test_policy_defaults():
    policy = WebPolicy()
    assert (policy.search_ladder, policy.search_good_results, policy.search_hedge_after_seconds,
            policy.search_hedge_racers, policy.search_max_seconds, policy.search_max_sources,
            policy.search_paid_after_weak, policy.search_learned_routing, policy.search_exclude_operators) == (
        True, 3, 3.0, 2, 30.0, 6, 2, True, 3)
    assert (policy.search_date_reads, policy.search_date_read_timeout_seconds) == (6, 5.0)


async def test_a_sources_transport_provider_wins_over_provider_candidates(monkeypatch, tmp_path):
    # Regression: with --paid the policy carries provider_candidates, the transport pin was
    # skipped and a hosted search URL was read by the plain http provider.
    install(monkeypatch, Fake("hosted", good(4), paid=True, transport="hosted_api"), Fake("plain", good(1, "p")))
    policy = WebPolicy(allow_paid_fallbacks=True, provider_candidates=("http", "jina_reader"),
                       search_paid_after_weak=1)
    await run(tmp_path, policy=policy)
    assert SEEN_POLICY["hosted"] == ("hosted_api", None)
    CALLS.clear()
    install(monkeypatch, Fake("hosted", good(4), transport="hosted_api"))
    await run(tmp_path, policy=WebPolicy(provider="http"))
    assert SEEN_POLICY["hosted"] == ("hosted_api", None)


# --- date enrichment: undated results are read for their publication date ------

def stamp(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def page_with(date=None, jsonld=False):
    if date is None:
        return "<html><head><title>t</title></head><body>no date here at all</body></html>"
    meta = ('<script type="application/ld+json">{"@type":"NewsArticle","datePublished":"%s"}</script>' % date if jsonld
            else '<meta property="article:published_time" content="%s">' % date)
    return "<html><head><title>t</title>%s</head><body>some article text</body></html>" % meta


class Site:
    """Synthetic pages by host, counting the requests made to them."""

    def __init__(self, pages):
        self.pages, self.hits = pages, []

    def __call__(self, request):
        import httpx
        self.hits.append(request.url.host)
        body = self.pages.get(request.url.host)
        if body is None:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})


async def run_dated(tmp_path, site, query="rust async", policy=None, **kwargs):
    import httpx
    async with Runtime(tmp_path, transport=httpx.MockTransport(site)) as web:
        return await web.search(query, policy=policy or WebPolicy(provider="http", origin_min_interval_seconds=0,
                                                              search_hold_undated=False),
                                **kwargs)


async def test_page_dates_keep_the_in_window_drop_the_old_and_tolerate_failures(monkeypatch, tmp_path):
    # s0 meta in window, s1 JSON-LD in window, s2 months old, s3 server error, s4 no date.
    site = Site({"s0.test": page_with(stamp(3)), "s1.test": page_with(stamp(10), jsonld=True),
                 "s2.test": page_with(stamp(150)), "s4.test": page_with()})
    install(monkeypatch, Fake("one", good(5)))
    result = await run_dated(tmp_path, site, recency="month", limit=5)
    urls = [row["url"].split("/")[2] for row in result["results"]]
    assert urls == ["s0.test", "s1.test", "s3.test", "s4.test"]  # verified first, still-undated after, s2 dropped
    by_host = {row["url"].split("/")[2]: row for row in result["results"]}
    assert by_host["s0.test"]["published"][:10] == stamp(3)[:10] and by_host["s1.test"]["published"][:10] == stamp(10)[:10]
    assert by_host["s0.test"]["check"]["date_source"] == "page" and by_host["s0.test"]["check"]["in_window"] is True
    assert not by_host["s3.test"].get("published") and by_host["s3.test"]["check"]["in_window"] is None
    checks = result["receipt"]["search_checks"]
    assert checks["date_reads"]["tried"] == 5 and checks["date_reads"]["dated"] == 3
    assert checks["date_reads"]["dropped"] == 1 and checks["date_reads"]["cost_usd"] == 0
    assert checks["date_reads"]["latency_ms"] >= 0
    assert [row["date_source"] for row in checks["results"]] == ["page", "page", None, None]
    assert checks["verdict"] == "good"  # two verified plus one undated (half the quota of 3)
    assert sorted(site.hits) == ["s0.test", "s1.test", "s2.test", "s3.test", "s4.test"]


async def test_date_reads_stop_at_the_cap_and_only_cover_results_in_the_top(monkeypatch, tmp_path):
    site = Site({"s%d.test" % index: page_with(stamp(2)) for index in range(12)})
    install(monkeypatch, Fake("one", good(12)))
    result = await run_dated(tmp_path, site, recency="month", limit=10,
                             policy=WebPolicy(provider="http", origin_min_interval_seconds=0, search_date_reads=4,
                                             search_hold_undated=False))
    assert len(site.hits) == 4 and result["receipt"]["search_checks"]["date_reads"]["tried"] == 4
    dated = [row for row in result["results"] if row["check"]["date_source"] == "page"]
    assert len(dated) == 4 and len(result["results"]) == 10
    # Only results that would make the top `limit` are read: a limit of 2 reads 2 pages.
    site.hits.clear()
    install(monkeypatch, Fake("one", good(12)))
    result = await run_dated(tmp_path, site, recency="month", limit=2)
    assert len(site.hits) == 2 and result["receipt"]["search_checks"]["date_reads"]["tried"] == 2


async def test_date_reads_are_off_without_a_window_an_explicit_source_or_a_zero_cap(monkeypatch, tmp_path):
    site = Site({"s%d.test" % index: page_with(stamp(2)) for index in range(6)})
    install(monkeypatch, Fake("one", good(5)))
    result = await run_dated(tmp_path, site)
    assert site.hits == [] and "date_reads" not in result["receipt"]["search_checks"]
    result = await run_dated(tmp_path, site, recency="month", source="one")
    assert site.hits == [] and "date_reads" not in result["receipt"]["search_checks"]
    result = await run_dated(tmp_path, site, recency="month",
                             policy=WebPolicy(provider="http", search_date_reads=0))
    assert site.hits == [] and "date_reads" not in result["receipt"]["search_checks"]
    assert all(not row.get("published") for row in result["results"])


async def test_undated_filler_does_not_end_the_search(monkeypatch, tmp_path):
    # Pages that cannot be dated count for at most half the quota, so the ladder climbs.
    site = Site({"s%d.test" % index: page_with() for index in range(5)}
                | {"t%d.test" % index: page_with(stamp(1)) for index in range(5)})
    install(monkeypatch, Fake("filler", good(5)), Fake("dated", good(5, "t")), Fake("never", good(5, "u")))
    result = await run_dated(tmp_path, site, recency="month", limit=5)
    assert CALLS == ["filler", "dated"]
    assert result["receipt"]["search_ladder"]["winner"] == "dated"
    assert [row["check"]["date_source"] for row in result["results"]] == ["page"] * 5


async def test_a_page_dated_outside_the_window_leaves_the_set_weak_and_the_ladder_climbing(monkeypatch, tmp_path):
    site = Site({"s%d.test" % index: page_with(stamp(300)) for index in range(5)}
                | {"t%d.test" % index: page_with(stamp(2)) for index in range(5)})
    install(monkeypatch, Fake("stale", good(5)), Fake("fresh", good(5, "t")))
    result = await run_dated(tmp_path, site, recency="week", limit=5,
                             policy=WebPolicy(provider="http", origin_min_interval_seconds=0, search_date_reads=20))
    assert CALLS == ["stale", "fresh"]
    assert all(row["url"].split("/")[2].startswith("t") for row in result["results"])
    reads = result["receipt"]["search_checks"]["date_reads"]
    assert reads["tried"] == 10 and reads["dropped"] == 5 and reads["dated"] == 10  # 5 stale pages read, 5 fresh


async def test_snippet_and_url_dates_are_set_as_published_without_reading_pages(monkeypatch, tmp_path):
    site = Site({})
    rows = [{"url": "https://a.test/2026/10/09/rust-async-story", "title": "rust async", "snippet": ""},
            {"url": "https://b.test/rust/async", "title": "rust async", "snippet": "2 days ago - rust async"}]
    install(monkeypatch, Fake("one", rows + good(2, "c", date=stamp(1))))
    result = await run_dated(tmp_path, site, recency="month", limit=4,
                             policy=WebPolicy(provider="http", search_date_reads=0))
    by_host = {row["url"].split("/")[2]: row for row in result["results"]}
    assert by_host["a.test"]["published"] == "2026-10-09T23:59:59Z" and by_host["a.test"]["check"]["date_source"] == "url"
    assert by_host["b.test"]["published"] is not None and by_host["b.test"]["check"]["date_source"] == "snippet"
    assert site.hits == []


async def test_date_reads_use_the_cheap_provider_even_when_paid_tools_are_allowed(monkeypatch, tmp_path):
    seen = []

    async def spy(self, url, policy=None, *args, **kwargs):
        seen.append((policy.provider, policy.provider_candidates, policy.allow_paid_fallbacks, policy.identity,
                     policy.timeout_seconds))
        return {"content": page_with(stamp(1)), "receipt": {"status": "observed", "cost_usd": 0}}
    monkeypatch.setattr(Runtime, "read", spy)
    install(monkeypatch, Fake("one", good(4)))
    policy = WebPolicy(allow_paid_fallbacks=True, provider_candidates=("http", "zyte"), identity="me",
                       search_date_read_timeout_seconds=2.5)
    result = await run(tmp_path, policy=policy, recency="month", limit=4)
    assert seen and set(seen) == {("http", None, False, None, 2.5)}
    assert result["receipt"]["search_checks"]["date_reads"]["dated"] == len(seen)


async def test_a_vertical_falls_back_to_the_web_engines_when_its_own_sources_fail(monkeypatch, tmp_path):
    install(monkeypatch, Fake("papers_api", fail=True, vertical="papers"), Fake("engine", good(5, "w")))
    result = await run(tmp_path, vertical="papers")
    assert CALLS == ["papers_api", "engine"]
    assert result["receipt"]["search_ladder"]["winner"] == "engine" and len(result["results"]) == 5


async def test_a_vertical_that_passes_never_reaches_the_web_engines(monkeypatch, tmp_path):
    install(monkeypatch, Fake("papers_api", good(5), vertical="papers"), Fake("engine", good(5, "w")))
    result = await run(tmp_path, vertical="papers")
    assert CALLS == ["papers_api"]


async def test_homepages_and_wrong_company_results_make_a_free_set_weak_so_the_ladder_reaches_paid(monkeypatch, tmp_path):
    junk = [{"url": "https://www.nintendo.com/", "title": "Nintendo", "snippet": ""},
            {"url": "https://www.nintendo.com/en-us/", "title": "Nintendo", "snippet": ""},
            {"url": "https://sony.test/news/2026/ps-launch", "title": "Sony latest announcement", "snippet": ""},
            {"url": "https://alarm.test/news/2026/latest", "title": "Alarm systems latest announcement", "snippet": ""},
            {"url": "https://wiki.test/nintendo", "title": "Nintendo", "snippet": ""}]
    best = [{"url": "https://www.nintendo.com/en-us/news/2026/10/announcement-%d" % index,
             "title": "Nintendo announcement %d" % index, "snippet": ""} for index in range(4)]
    install(monkeypatch, Fake("free", junk), Fake("paid", best, paid=True, cost=0.01))
    policy = WebPolicy(provider="http", allow_paid_fallbacks=True, search_paid_after_weak=1, search_hedge_after_seconds=0)
    result = await run(tmp_path, query="Nintendo latest announcement", policy=policy)
    assert CALLS == ["free", "paid"]
    assert result["receipt"]["search_ladder"]["winner"] == "paid"
    assert {row["url"] for row in result["results"]} == {row["url"] for row in best}
    free = [row for row in result["receipt"]["search_attempts"] if row["source"] == "free"][0]
    assert free["passed"] == 0


async def test_paid_sources_stay_unreached_when_the_homepage_set_is_not_allowed_to_climb(monkeypatch, tmp_path):
    junk = [{"url": "https://www.nintendo.com/", "title": "Nintendo", "snippet": ""}]
    install(monkeypatch, Fake("free", junk), Fake("paid", junk, paid=True, cost=0.01))
    result = await run(tmp_path, query="Nintendo latest announcement")
    assert CALLS == ["free"] and result["results"] == []


async def test_windowed_search_holds_undated_results_apart_when_verified_ones_exist(monkeypatch, tmp_path):
    now = datetime.now(timezone.utc)
    fresh = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [{"url": "https://a.test/rust/1", "title": "rust async one", "snippet": "", "published": fresh},
            {"url": "https://b.test/rust/2", "title": "rust async two", "snippet": "no date here"},
            {"url": "https://c.test/rust/3", "title": "rust async three", "snippet": "", "published": fresh}]
    install(monkeypatch, Fake("one", rows))
    result = await run(tmp_path, recency="week", policy=WebPolicy(provider="http", search_date_reads=0))
    assert [row["url"] for row in result["results"]] == ["https://a.test/rust/1", "https://c.test/rust/3"]
    assert [row["url"] for row in result["unverified_results"]] == ["https://b.test/rust/2"]


async def test_holding_undated_results_can_be_turned_off(monkeypatch, tmp_path):
    now = datetime.now(timezone.utc)
    fresh = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [{"url": "https://a.test/rust/1", "title": "rust async one", "snippet": "", "published": fresh},
            {"url": "https://b.test/rust/2", "title": "rust async two", "snippet": "no date here"}]
    install(monkeypatch, Fake("one", rows))
    result = await run(tmp_path, recency="week",
                       policy=WebPolicy(provider="http", search_date_reads=0, search_hold_undated=False))
    assert len(result["results"]) == 2 and "unverified_results" not in result


def dated(count, prefix, days=1):
    when = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return [{"url": "https://%s%d.test/rust/story-%d" % (prefix, index, index), "title": "rust async story %d" % index,
             "snippet": "", "published": when} for index in range(count)]


async def test_a_good_but_short_windowed_answer_is_filled_from_the_next_free_source(monkeypatch, tmp_path):
    install(monkeypatch, Fake("one", dated(3, "a")), Fake("two", dated(3, "b")), Fake("three", dated(3, "c")))
    result = await run(tmp_path, recency="week", limit=5, policy=WebPolicy(provider="http", search_date_reads=0))
    assert CALLS == ["one", "two"]
    assert len(result["results"]) == 5 and result["receipt"]["search_ladder"]["winner"] == "merged"


async def test_a_full_answer_is_not_filled_and_filling_can_be_turned_off(monkeypatch, tmp_path):
    install(monkeypatch, Fake("one", dated(5, "a")), Fake("two", dated(3, "b")))
    await run(tmp_path, recency="week", limit=5, policy=WebPolicy(provider="http", search_date_reads=0))
    assert CALLS == ["one"]
    CALLS.clear()
    install(monkeypatch, Fake("one", dated(3, "a")), Fake("two", dated(3, "b")))
    await run(tmp_path, recency="week", limit=5,
              policy=WebPolicy(provider="http", search_date_reads=0, search_fill_rungs=0))
    assert CALLS == ["one"]


async def test_filling_never_reaches_a_paid_source(monkeypatch, tmp_path):
    install(monkeypatch, Fake("one", dated(3, "a")), Fake("paid", dated(5, "p"), paid=True, cost=0.01))
    # A windowed search is an announcement-type query, so paid sources start beside the first free
    # rung unless search_paid_parallel is off; filling itself still never asks one.
    await run(tmp_path, recency="week", limit=5,
              policy=WebPolicy(provider="http", search_date_reads=0, allow_paid_fallbacks=True,
                               search_paid_parallel=False))
    assert CALLS == ["one"]


async def test_a_search_without_a_window_is_never_filled(monkeypatch, tmp_path):
    install(monkeypatch, Fake("one", good(3)), Fake("two", good(3, "t")))
    await run(tmp_path, limit=5)
    assert CALLS == ["one"]
