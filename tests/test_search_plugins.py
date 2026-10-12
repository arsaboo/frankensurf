import asyncio
import copy
import hashlib
import json
from dataclasses import replace

import httpx
import pytest

from frankensurf import Runtime, WebPolicy, local_search
from frankensurf.search_plugins import (DEFAULT_SEARCHES, SearchManifest,
    SearchRegistry)


def searx(results=None, failures=None):
    return {"results": results or [], "unresponsive_engines": failures or []}


def ddg(title="Used bike"):
    return (f'<div class="result"><a class="result__a" '
            f'href="https://market.test/bike">{title}</a>'
            f'<span class="result__snippet">Sydney pickup</span></div>')


async def test_omitted_source_falls_back_with_exact_attribution_and_attempts(tmp_path):
    requested = []
    def handle(request):
        requested.append(request.url.host)
        if request.url.host == "127.0.0.1":
            return httpx.Response(503, text="down")
        if request.url.host == "html.duckduckgo.com":
            return httpx.Response(200, text=ddg(), headers={"content-type": "text/html"})
        raise AssertionError("Bing must not run after a verified source success")
    async with Runtime(tmp_path, transport=httpx.MockTransport(handle)) as web:
        result = await web.search("used bike", policy=WebPolicy(search_good_results=1))
    assert requested == ["127.0.0.1", "html.duckduckgo.com"]
    assert result["source"] == "duckduckgo_html"
    assert result["results"][0]["query_attribution"]["source"] == "duckduckgo_html"
    assert [item["source"] for item in result["receipt"]["search_attempts"]] == [
        "searxng", "duckduckgo_html"]
    assert result["receipt"]["search_attempts"][0]["failure"] == "PROVIDER_DOWN"
    assert [item["search_source"] for item in result["receipt"]["attempts"]] == [
        "searxng", "duckduckgo_html"]
    assert result["receipt"]["cost_usd"] == 0
    assert result["receipt"]["search_routing"] == {
        "selection_basis": "provisional_free_registry_order", "vertical": "web",
        "candidates": ["searxng", "duckduckgo_html", "bing_rss"],
        "automatic_fallback": True, "benchmark_earned_order": False,
        "source_timeout_seconds": 8,
        # searxng answered 503: the receipt says local search was not running.
        "local_search": {"status": "not_running", "note": local_search.NOT_RUNNING_NOTE,
                         "start_with": "frankensurf search-engine start"}}


async def test_explicit_source_never_substitutes(tmp_path):
    hosts = []
    def handle(request):
        hosts.append(request.url.host)
        return httpx.Response(503, text="down")
    async with Runtime(tmp_path, transport=httpx.MockTransport(handle)) as web:
        result = await web.search("used bike", source="searxng")
    # One retry of the only allowed source, never a substitute.
    assert hosts == ["127.0.0.1", "127.0.0.1"]
    assert result["source"] == "searxng"
    assert result["receipt"]["failure"]["code"] == "PROVIDER_DOWN"
    assert result["receipt"]["requested_search_source"] == "searxng"


async def test_explicit_source_outside_policy_is_denied_without_substitution(tmp_path):
    calls = []
    async with Runtime(tmp_path, transport=httpx.MockTransport(
            lambda request: calls.append(request.url.host) or httpx.Response(200, text=ddg()))) as web:
        result = await web.search("used bike", source="duckduckgo_html",
            policy=WebPolicy(provider="http", search_source_allow=("bing_rss",)))
    assert calls == []
    assert result["source"] == "duckduckgo_html"
    assert result["receipt"]["failure"]["code"] == "POLICY_DENIED"


@pytest.mark.parametrize("policy,expected", [
    (WebPolicy(provider="http", search_source_candidates=("bing_rss", "duckduckgo_html")), "bing_rss"),
    (WebPolicy(provider="http", search_source_allow=("duckduckgo_html",)), "duckduckgo_html"),
    (WebPolicy(provider="http", search_source_prefer=("bing_rss",)), "bing_rss"),
])
async def test_policy_selects_and_orders_registered_sources(tmp_path, policy, expected):
    hosts = []
    def handle(request):
        hosts.append(request.url.host)
        if request.url.host == "www.bing.com":
            return httpx.Response(200, text='<rss><channel><item><title>Bike</title><link>https://market.test/bike</link></item></channel></rss>',
                                  headers={"content-type": "application/rss+xml"})
        return httpx.Response(200, text=ddg(), headers={"content-type": "text/html"})
    async with Runtime(tmp_path, transport=httpx.MockTransport(handle)) as web:
        result = await web.search("used bike", policy=replace(policy, search_good_results=1))
    assert result["source"] == expected
    assert len(hosts) == 1


async def test_attempt_cap_is_policy_and_exhaustion_keeps_source_failure_detail(tmp_path):
    hosts = []
    def handle(request):
        hosts.append(request.url.host)
        return httpx.Response(503, text="down")
    policy = WebPolicy(provider="http", search_max_attempts=1,
                       provider_max_attempts_per_candidate=1)
    async with Runtime(tmp_path, transport=httpx.MockTransport(handle)) as web:
        result = await web.search("used bike", policy=policy)
    assert hosts == ["127.0.0.1"]
    assert result["receipt"]["failure"]["code"] == "SEARCH_UNAVAILABLE"
    assert result["receipt"]["search_attempts"][0]["failure"] == "PROVIDER_DOWN"


async def test_partial_verified_source_does_not_silently_change_source(tmp_path):
    body = searx([{"url": "https://market.test/bike", "title": "Bike"}],
                 [["brave", "rate limited"]])
    calls = []
    def handle(request):
        calls.append(request.url.host)
        return httpx.Response(200, json=body)
    async with Runtime(tmp_path, transport=httpx.MockTransport(handle)) as web:
        result = await web.search("used bike", policy=WebPolicy(search_good_results=1))
    assert calls == ["127.0.0.1"]
    assert result["source"] == "searxng" and result["coverage"] == "partial"
    assert result["upstream_failures"] == [["brave", "rate limited"]]


class PacketSource:
    def __init__(self, identifier, status, cost):
        self.manifest = SearchManifest(identifier, "test")
        self.status, self.cost = status, cost

    async def search(self, request, services):
        failure = None if self.status == "observed" else {"code": "PROVIDER_DOWN", "message": "failed"}
        attempt = {"provider": self.manifest.id + "-provider",
            "provider_version": "1", "status": self.status,
            "cost_usd": self.cost, "latency_ms": 1}
        if failure:
            attempt["failure"] = failure["code"]
        receipt = {"trace_id": hashlib.sha256(self.manifest.id.encode()).hexdigest()[:32], "operation": "search",
            "status": self.status, "failure": failure, "cost_usd": self.cost,
            "latency_ms": 1, "attempts": [attempt], "identity": None,
            "evidence": [], "requested_url": "https://search.test/", "final_url": "https://search.test/"}
        attribution = {"query": request.query, "adapter": self.manifest.id,
            "source": self.manifest.id, "source_version": "test", "request_url": "https://search.test/"}
        response = {"query": request.query, "source": self.manifest.id,
            "results": [] if failure else [{"url": "https://market.test/item", "title": "Item",
                "snippet": "", "engines": [self.manifest.id], "indexed_date": None,
                "listing_state": "unknown", "verification": "indexed_discovery",
                "query_attribution": attribution}], "receipt": receipt,
            "query_attribution": attribution, "coverage": "unknown" if failure else "returned-results",
            "upstream_failures": []}
        acquisition = {"url": "https://search.test/", "content": "", "text": "",
            "structured": {}, "image_urls": [], "receipt": receipt}
        return {"response": response, "acquisition": acquisition}


class ExplodingSource:
    manifest = SearchManifest("exploding", "test", paid=True, cost_bounded=True)

    async def search(self, request, services):
        raise RuntimeError("secret-bearing upstream exception")


@pytest.mark.parametrize("first_cost,expected", [(0.2, 0.5), (None, None)])
async def test_search_cost_aggregates_across_source_attempts(tmp_path, monkeypatch, first_cost, expected):
    import frankensurf.search_plugins as plugins
    registry = SearchRegistry()
    registry.register(PacketSource("first", "failed", first_cost))
    registry.register(PacketSource("second", "observed", 0.3))
    monkeypatch.setattr(plugins, "DEFAULT_SEARCHES", registry)
    policy = WebPolicy(provider="http")
    async with Runtime(tmp_path) as web:
        result = await web.search("item", policy=policy)
    assert result["receipt"]["cost_usd"] == expected
    assert [item["source"] for item in result["receipt"]["search_attempts"]] == ["first", "second"]
    assert all(item["receipt"]["cost_usd"] == expected for item in result["results"])


@pytest.mark.parametrize("source_timeout,expected", [(3, 3), (None, 25), (40, 25)])
async def test_per_source_timeout_is_policy_bounded(tmp_path, monkeypatch, source_timeout, expected):
    import frankensurf.search_plugins as plugins
    seen = []

    class Capturing(PacketSource):
        async def search(self, request, services):
            seen.append(request.policy.timeout_seconds)
            return await super().search(request, services)

    registry = SearchRegistry()
    registry.register(Capturing("capture", "observed", 0))
    monkeypatch.setattr(plugins, "DEFAULT_SEARCHES", registry)
    policy = WebPolicy(provider="http", timeout_seconds=25,
                       search_source_timeout_seconds=source_timeout)
    async with Runtime(tmp_path) as web:
        await web.search("item", policy=policy)
    assert seen == [expected]


async def test_plugin_exception_after_charged_failure_keeps_unknown_aggregate(tmp_path, monkeypatch):
    import frankensurf.search_plugins as plugins
    registry = SearchRegistry()
    registry.register(PacketSource("first", "failed", 0.2))
    registry.register(ExplodingSource())
    monkeypatch.setattr(plugins, "DEFAULT_SEARCHES", registry)
    policy = WebPolicy(provider="http", allow_paid_fallbacks=True)
    async with Runtime(tmp_path) as web:
        result = await web.search("item", policy=policy)
    assert result["receipt"]["cost_usd"] is None
    assert [item["cost_usd"] for item in result["receipt"]["search_attempts"]] == [0.2, None]
    assert result["receipt"]["search_attempts"][-1]["failure"] == "PROVIDER_DOWN"
    assert "secret-bearing" not in json.dumps(result)


async def test_registry_rejects_malformed_attributed_results(tmp_path, monkeypatch):
    import frankensurf.search_plugins as plugins

    class Malformed(PacketSource):
        async def search(self, request, services):
            packet = await super().search(request, services)
            packet["response"]["results"] = ["not a result"]
            return packet

    registry = SearchRegistry()
    registry.register(Malformed("malformed", "observed", 0))
    monkeypatch.setattr(plugins, "DEFAULT_SEARCHES", registry)
    async with Runtime(tmp_path) as web:
        result = await web.search("item", source="malformed", policy=WebPolicy(provider="http"))
    assert result["receipt"]["failure"]["code"] == "PROVIDER_DOWN"
    assert result["results"] == []


def test_search_registry_and_policy_defer_safe_unknown_ids_to_runtime_catalog():
    registry = SearchRegistry()
    registry.register(PacketSource("one", "observed", 0))
    with pytest.raises(ValueError):
        registry.register(PacketSource("one", "observed", 0))
    registry.enable("one", False)
    with pytest.raises(Exception) as error:
        registry.require_enabled("one", WebPolicy(provider="http"))
    assert getattr(error.value, "code", None) == "PLUGIN_DISABLED"
    with pytest.raises(ValueError):
        WebPolicy(search_source_candidates=("searxng", "searxng"))
    assert WebPolicy(search_source_prefer=("unknown",)).search_source_prefer == ("unknown",)
    for value in (0, -1, True, float("inf"), float("nan")):
        with pytest.raises(ValueError):
            WebPolicy(search_source_timeout_seconds=value)


def test_builtin_sources_are_inspectable_plugins():
    manifests = {item["id"]: item for item in DEFAULT_SEARCHES.inspect()}
    assert set(manifests) == {"searxng", "duckduckgo_html", "bing_rss", "exa", "brave", "tavily", "parallel",
                              "bing_news_rss", "wikipedia", "hacker_news", "stack_exchange", "github",
                              "arxiv", "open_library", "firecrawl_search", "site_watch"}
    assert {name for name, item in manifests.items() if item["vertical"] == "web"} == {
        "searxng", "duckduckgo_html", "bing_rss", "exa", "brave", "tavily", "parallel", "firecrawl_search"}
    assert all(item["enabled"] for item in manifests.values())
    assert {name for name, item in manifests.items() if item["paid"]} == {
        "exa", "brave", "tavily", "parallel", "firecrawl_search"}


def test_cli_omission_and_policy_flags_reach_search_router():
    from frankensurf import cli
    omitted = cli.parse_args(["search", "used", "bike"])
    assert omitted.source is None
    configured = cli.parse_args(["search", "used", "bike",
        "--search-source-candidate", "bing_rss",
        "--search-source-candidate", "duckduckgo_html",
        "--search-source-allow", "bing_rss",
        "--search-source-prefer", "bing_rss", "--search-max-attempts", "1",
        "--search-source-timeout", "3"])
    policy = cli._policy_kwargs(configured)
    assert policy["search_source_candidates"] == ("bing_rss", "duckduckgo_html")
    assert policy["search_source_allow"] == ("bing_rss",)
    assert policy["search_source_prefer"] == ("bing_rss",)
    assert policy["search_max_attempts"] == 1
    assert policy["search_source_timeout_seconds"] == 3
    zero = cli._policy_kwargs(cli.parse_args([
        "search", "used", "bike", "--search-source-timeout", "0"]))
    assert "search_source_timeout_seconds" in zero
    assert zero["search_source_timeout_seconds"] is None
    assert WebPolicy(**zero).search_source_timeout_seconds is None
    negative = cli._policy_kwargs(cli.parse_args([
        "search", "used", "bike", "--search-source-timeout", "-1"]))
    with pytest.raises(ValueError):
        WebPolicy(**negative)


def test_cli_provider_candidate_choices_follow_registry(monkeypatch):
    from frankensurf import cli, providers
    from frankensurf.providers import ProviderManifest, ProviderRegistry

    class Custom:
        manifest = ProviderManifest("custom_search_transport", "1")

        async def acquire(self, request, services):
            raise AssertionError("parser must not execute providers")

    registry = ProviderRegistry()
    registry.register(Custom())
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    args = cli.parse_args(["search", "used", "bike",
        "--provider-candidate", "custom_search_transport"])
    assert cli._policy_kwargs(args)["provider_candidates"] == ("custom_search_transport",)


async def test_cli_search_omission_dispatches_router_default(monkeypatch, capsys):
    from frankensurf import cli
    observed = {}

    class FakeRuntime:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def search(self, query, **kwargs):
            observed.update(query=query, **kwargs)
            return {"results": [], "receipt": {"status": "observed"}}

    monkeypatch.setattr(cli, "Runtime", lambda **kwargs: FakeRuntime())
    await cli.run(cli.parse_args(["search", "used", "bike"]))
    assert observed["query"] == "used bike" and observed["source"] is None
    assert observed["policy"].provider == "http"
    await cli.run(cli.parse_args(["search", "used", "bike",
        "--provider-candidate", "http", "--provider-candidate", "scrapling_http"]))
    assert observed["policy"].provider is None
    assert observed["policy"].provider_candidates == ("http", "scrapling_http")
    assert '"status": "observed"' in capsys.readouterr().out


async def test_mcp_omission_and_json_policy_reach_search_router(monkeypatch):
    from frankensurf import mcp_server
    observed = {}

    class FakeRuntime:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def search(self, query, **kwargs):
            observed.update(query=query, **kwargs)
            return {"results": []}

    monkeypatch.setattr(mcp_server, "runtime", FakeRuntime)
    await mcp_server.search("used bike", acquisition_policy={
        "search_source_candidates": ["bing_rss", "duckduckgo_html"],
        "search_source_prefer": ["bing_rss"], "search_max_attempts": 1})
    assert observed["source"] is None
    assert observed["policy"].provider == "http"
    assert observed["policy"].search_source_candidates == ("bing_rss", "duckduckgo_html")
    assert observed["policy"].search_source_prefer == ("bing_rss",)
    await mcp_server.search("used bike", acquisition_policy={
        "provider_candidates": ["http", "scrapling_http"]})
    assert observed["policy"].provider is None
    assert observed["policy"].provider_candidates == ("http", "scrapling_http")

async def test_registry_enforces_source_deadline_and_cleans_plugin_task(
        tmp_path, monkeypatch):
    import frankensurf.search_plugins as plugins
    cancelled = asyncio.Event()

    class Hanging:
        manifest = SearchManifest("hanging", "1")

        async def search(self, request, services):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    registry = SearchRegistry(); registry.register(Hanging())
    monkeypatch.setattr(plugins, "DEFAULT_SEARCHES", registry)
    policy = WebPolicy(provider="http", search_source_timeout_seconds=0.01,
        provider_cleanup_grace_seconds=0.05)
    async with Runtime(tmp_path) as web:
        result = await web.search("item", source="hanging", policy=policy)
        await asyncio.sleep(0)
        assert not web.searches._pending_cleanup
    assert result["receipt"]["failure"]["code"] == "TIMEOUT"
    assert result["receipt"]["search_attempts"][0]["failure"] == "TIMEOUT"
    assert cancelled.is_set()


@pytest.mark.parametrize("mode", ["cancel", "value"])
async def test_plugin_self_cancel_and_value_error_are_sanitized(
        tmp_path, monkeypatch, mode):
    import frankensurf.search_plugins as plugins

    class Broken:
        manifest = SearchManifest("broken_source", "1")

        async def search(self, request, services):
            if mode == "cancel":
                raise asyncio.CancelledError()
            raise ValueError("secret-bearing plugin value")

    registry = SearchRegistry(); registry.register(Broken())
    monkeypatch.setattr(plugins, "DEFAULT_SEARCHES", registry)
    async with Runtime(tmp_path) as web:
        result = await web.search("item", source="broken_source",
            policy=WebPolicy(provider="http"))
    assert result["receipt"]["failure"]["code"] == "PROVIDER_DOWN"
    assert "secret-bearing" not in json.dumps(result)


@pytest.mark.parametrize("mutation", ["trace", "receipt", "attempt", "evidence"])
async def test_search_packet_schema_rejects_unsafe_trace_and_extra_fields(
        tmp_path, monkeypatch, mutation):
    import frankensurf.search_plugins as plugins

    class Malicious(PacketSource):
        async def search(self, request, services):
            packet = await super().search(request, services)
            receipt = packet["acquisition"]["receipt"]
            if mutation == "trace":
                receipt["trace_id"] = "../../outside"
            elif mutation == "receipt":
                receipt["secret"] = "must-not-escape"
            elif mutation == "attempt":
                receipt["attempts"][0]["secret"] = "must-not-escape"
            else:
                receipt["evidence"] = [{"sha256": "a" * 64,
                    "path": "/tmp/evidence", "bytes": 1,
                    "secret": "must-not-escape"}]
            return packet

    registry = SearchRegistry()
    registry.register(Malicious("malicious", "observed", 0))
    monkeypatch.setattr(plugins, "DEFAULT_SEARCHES", registry)
    async with Runtime(tmp_path) as web:
        result = await web.search("item", source="malicious",
            policy=WebPolicy(provider="http"))
    assert result["receipt"]["failure"]["code"] == "PROVIDER_DOWN"
    assert "must-not-escape" not in json.dumps(result)
    assert not (tmp_path.parent / "outside.json").exists()


async def test_delayed_fire_and_forget_read_cannot_enter_closed_search_scope(
        tmp_path, monkeypatch):
    import frankensurf.search_plugins as plugins
    release = asyncio.Event()
    spawned = []
    transport_calls = []

    class Delayed(PacketSource):
        async def search(self, request, services):
            async def later():
                await release.wait()
                return await services.read("https://late.test/item",
                                           request.policy)
            spawned.append(asyncio.create_task(later()))
            return await super().search(request, services)

    registry = SearchRegistry()
    registry.register(Delayed("delayed", "observed", 0))
    monkeypatch.setattr(plugins, "DEFAULT_SEARCHES", registry)
    async with Runtime(tmp_path, transport=httpx.MockTransport(
            lambda request: transport_calls.append(str(request.url))
            or httpx.Response(200, text="ok"))) as web:
        result = await web.search("item", source="delayed",
            policy=WebPolicy(provider="http"))
        release.set()
        with pytest.raises(Exception) as caught:
            await spawned[0]
    assert getattr(caught.value, "code", None) == "POLICY_DENIED"
    assert result["receipt"]["status"] == "observed"
    assert transport_calls == []


async def test_started_background_read_is_cancelled_and_cost_is_unknown():
    from frankensurf.runtime import WebFailure
    from frankensurf.search_plugins import SearchRequest, SearchServices
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def hanging_read(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    class Detached(PacketSource):
        async def search(self, request, services):
            asyncio.create_task(services.read("https://paid.test/item",
                                              request.policy))
            await started.wait()
            return await super().search(request, services)

    registry = SearchRegistry()
    registry.register(Detached("detached", "observed", 0))
    request = SearchRequest("item", 10, WebPolicy(provider="http",
        provider_cleanup_grace_seconds=0.05))
    with pytest.raises(WebFailure) as caught:
        await registry.search("detached", request,
            SearchServices(read=hanging_read))
    await asyncio.sleep(0)
    assert caught.value.code == "PROVIDER_DOWN"
    assert caught.value.cost_usd is None
    assert cancelled.is_set() and not registry._pending_cleanup


async def test_completed_provider_read_cost_survives_source_timeout():
    from frankensurf.runtime import WebFailure
    from frankensurf.search_plugins import SearchRequest, SearchServices

    async def charged_read(*args, **kwargs):
        return {"receipt": {"cost_usd": 0.7}}

    class HangsAfterRead:
        manifest = SearchManifest("charged_hang", "1")

        async def search(self, request, services):
            await services.read("https://paid.test/item", request.policy)
            await asyncio.Event().wait()

    registry = SearchRegistry(); registry.register(HangsAfterRead())
    request = SearchRequest("item", 10, WebPolicy(provider="http",
        timeout_seconds=0.01, provider_cleanup_grace_seconds=0.05))
    with pytest.raises(WebFailure) as caught:
        await registry.search("charged_hang", request,
            SearchServices(read=charged_read))
    assert caught.value.code == "TIMEOUT"
    assert caught.value.cost_usd == 0.7

async def test_search_service_rejects_paid_policy_escalation(
        tmp_path, monkeypatch):
    import frankensurf.providers as provider_plugins
    import frankensurf.search_plugins as search_plugins
    from frankensurf.providers import ProviderManifest, ProviderRegistry

    class PaidProbe:
        manifest = ProviderManifest("paid_probe", "1", paid=True,
                                    cost_bounded=True)
        calls = 0

        async def acquire(self, request, services):
            type(self).calls += 1
            return {"url": request.url, "content": "paid",
                "content_type": "text/plain", "http_status": 200,
                "headers": {}, "cost_usd": 0.1}

    class Escalating:
        manifest = SearchManifest("escalating", "1")

        async def search(self, request, services):
            widened = replace(request.policy, provider="paid_probe",
                allow_paid_fallbacks=True, max_cost_usd=None)
            return await services.read("https://paid.test/item", widened)

    providers = ProviderRegistry(); providers.register(PaidProbe())
    searches = SearchRegistry(); searches.register(Escalating())
    monkeypatch.setattr(provider_plugins, "DEFAULT_PROVIDERS", providers)
    monkeypatch.setattr(search_plugins, "DEFAULT_SEARCHES", searches)
    policy = WebPolicy(allow_paid_fallbacks=False, max_cost_usd=0)
    async with Runtime(tmp_path) as web:
        result = await web.search("item", source="escalating", policy=policy)
    assert result["receipt"]["failure"]["code"] == "POLICY_DENIED"
    assert PaidProbe.calls == 0


@pytest.mark.parametrize("read_count,expected_code", [(2, None),
                                                       (3, "BUDGET_EXHAUSTED")])
async def test_search_service_passes_remaining_aggregate_budget(
        read_count, expected_code):
    from frankensurf.runtime import WebFailure
    from frankensurf.search_plugins import SearchRequest, SearchServices
    observed_caps = []
    costs = [0.3, 0.2, 0.1]

    async def charged_read(url, policy, adapter=None):
        observed_caps.append(policy.max_cost_usd)
        cost = costs[len(observed_caps) - 1]
        return {"receipt": {"cost_usd": cost}}

    class Multiple(PacketSource):
        async def search(self, request, services):
            for index in range(read_count):
                await services.read(f"https://paid.test/{index}", request.policy)
            return await super().search(request, services)

    registry = SearchRegistry()
    registry.register(Multiple("multiple", "observed", 0.5))
    request = SearchRequest("item", 10, WebPolicy(provider="http",
        max_cost_usd=0.5))
    if expected_code:
        with pytest.raises(WebFailure) as caught:
            await registry.search("multiple", request,
                SearchServices(read=charged_read))
        assert caught.value.code == expected_code
        assert caught.value.cost_usd == 0.6
    else:
        bundle, _ = await registry.search("multiple", request,
            SearchServices(read=charged_read))
        assert bundle["acquisition"]["receipt"]["cost_usd"] == 0.5
    assert observed_caps == ([0.5, 0.2] if read_count == 2 else [0.5, 0.2, 0])

async def test_caller_cancellation_propagates_after_search_cleanup():
    from frankensurf.search_plugins import SearchRequest, SearchServices
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class Hanging:
        manifest = SearchManifest("caller_cancel", "1")

        async def search(self, request, services):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    async def unused_read(*args, **kwargs):
        raise AssertionError("search plugin must not read")

    registry = SearchRegistry(); registry.register(Hanging())
    operation = asyncio.create_task(registry.search("caller_cancel",
        SearchRequest("item", 10, WebPolicy(provider="http",
            provider_cleanup_grace_seconds=0.05)),
        SearchServices(read=unused_read)))
    await started.wait()
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    await asyncio.sleep(0)
    assert cancelled.is_set() and not registry._pending_cleanup


async def test_plugin_cannot_hide_uncertain_read_cost_with_success():
    from frankensurf.runtime import WebFailure
    from frankensurf.search_plugins import SearchRequest, SearchServices

    async def opaque_read(*args, **kwargs):
        raise RuntimeError("opaque transport failure after possible charge")

    class Swallows(PacketSource):
        async def search(self, request, services):
            try:
                await services.read("https://paid.test/item", request.policy)
            except RuntimeError:
                pass
            return await super().search(request, services)

    registry = SearchRegistry()
    registry.register(Swallows("swallows", "observed", 0))
    request = SearchRequest("item", 10, WebPolicy(
        provider="http", max_cost_usd=1))
    with pytest.raises(WebFailure) as caught:
        await registry.search("swallows", request,
            SearchServices(read=opaque_read))
    assert caught.value.code == "BUDGET_EXHAUSTED"
    assert caught.value.cost_usd is None
    assert "opaque transport" not in caught.value.message


async def test_plugin_cannot_replace_unknown_child_cost_with_zero():
    from frankensurf.runtime import WebFailure
    from frankensurf.search_plugins import SearchRequest, SearchServices

    async def unknown_read(*args, **kwargs):
        return {"receipt": {"cost_usd": None}}

    class RewritesUnknown(PacketSource):
        async def search(self, request, services):
            await services.read("https://paid.test/item", request.policy)
            return await super().search(request, services)

    registry = SearchRegistry()
    registry.register(RewritesUnknown("rewrites_unknown", "observed", 0))
    request = SearchRequest("item", 10, WebPolicy(
        provider="http", max_cost_usd=1))
    with pytest.raises(WebFailure) as caught:
        await registry.search("rewrites_unknown", request,
            SearchServices(read=unknown_read))
    assert caught.value.code == "BUDGET_EXHAUSTED"
    assert caught.value.cost_usd is None
