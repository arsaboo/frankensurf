"""Block-aware escalation to paid rungs and per-origin route hints."""
import httpx
import pytest

from frankensurf import hosted_providers, providers
from frankensurf.providers import ProviderManifest, ProviderRegistry
from frankensurf.runtime import Runtime, WebFailure, WebPolicy

URL = "https://walled.example.com/item"
PAGE = "<html><head><title>Item</title></head><body>" + "x" * 200 + "</body></html>"


def provider(identifier, outcome, paid=False, remote=False):
    class Plugin:
        manifest = ProviderManifest(identifier, "1", paid=paid, remote=remote)

        async def acquire(self, request, services):
            if outcome != "ok":
                raise WebFailure(outcome, "fixture outcome")
            return {"url": request.url, "content": PAGE, "content_type": "text/html",
                    "http_status": 200}
    Plugin.__name__ = identifier
    return Plugin()


@pytest.fixture
def registry(monkeypatch):
    def install(*plugins):
        registry = ProviderRegistry()
        for plugin in plugins:
            registry.register(plugin)
        monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    return install


def tried(result):
    return [attempt["provider"] for attempt in result["receipt"]["attempts"]]


async def test_walls_escalate_to_paid_rungs_when_allowed(tmp_path, registry):
    registry(provider("w1", "BLOCKED"), provider("w2", "CAPTCHA"), provider("w3", "BLOCKED"),
             provider("free_ok", "ok"), provider("paid_ok", "ok", paid=True))
    policy = dict(escalate_after_walls=3, origin_route_hint_ttl_seconds=0)
    async with Runtime(tmp_path) as web:
        paid = await web.read(URL, WebPolicy(allow_paid_fallbacks=True, **policy))
        later = await web.read(URL, WebPolicy(allow_paid_fallbacks=True, paid_after_walls=0, **policy))
        free = await web.read(URL, WebPolicy(**policy))
    # Paid tools move ahead after the first wall by default (paid_after_walls).
    assert tried(paid) == ["w1", "paid_ok"]
    assert paid["receipt"]["routing"]["escalated_to"] == "paid_ok"
    # paid_after_walls=0 leaves them to escalate_after_walls, as before.
    assert tried(later) == ["w1", "w2", "w3", "paid_ok"]
    assert tried(free) == ["w1", "w2", "w3", "free_ok"]


async def test_walls_move_tools_that_fetch_from_elsewhere_ahead(tmp_path, registry):
    registry(provider("w1", "BLOCKED"), provider("w2", "CAPTCHA"), provider("w3", "BLOCKED"),
             provider("remote_ok", "ok", remote=True), provider("paid_ok", "ok", paid=True, remote=True))
    policy = dict(escalate_after_walls=2, origin_route_hint_ttl_seconds=0,
                  provider_max_attempts_per_candidate=1)
    async with Runtime(tmp_path) as web:
        free = await web.read(URL, WebPolicy(**policy))
        paid = await web.read(URL, WebPolicy(allow_paid_fallbacks=True, **policy))
    assert tried(free) == ["w1", "w2", "remote_ok"]
    assert free["receipt"]["routing"]["escalated_to"] == "remote_ok"
    assert tried(paid) == ["w1", "paid_ok"]


async def test_pages_this_machine_cannot_get_move_remote_tools_ahead_too(tmp_path, registry):
    registry(provider("timed_out", "TIMEOUT"), provider("unrendered", "VISUAL_REQUIRED"),
             provider("empty", "EMPTY_PAGE"), provider("remote_ok", "ok", remote=True))
    async with Runtime(tmp_path) as web:
        result = await web.read(URL, WebPolicy(escalate_after_walls=2, origin_route_hint_ttl_seconds=0,
                                               provider_max_attempts_per_candidate=1))
    assert tried(result) == ["timed_out", "unrendered", "remote_ok"]


def test_hosted_providers_fetch_from_elsewhere():
    from frankensurf.providers import DEFAULT_PROVIDERS
    remote = {item["id"] for item in DEFAULT_PROVIDERS.inspect() if item["remote"]}
    assert {"jina_reader", "firecrawl"} <= remote
    assert not remote & {"http", "local", "camoufox", "scrapling"}


async def test_a_read_that_ends_on_a_down_tool_reports_the_wall_it_met(tmp_path, registry):
    registry(provider("walled", "CAPTCHA"), provider("broken", "PROVIDER_DOWN"))
    async with Runtime(tmp_path) as web:
        result = await web.read(URL, WebPolicy(origin_route_hint_ttl_seconds=0,
                                               provider_max_attempts_per_candidate=1))
    assert tried(result) == ["walled", "broken"]
    assert result["receipt"]["failure"]["code"] == "CAPTCHA"


async def test_a_site_every_tool_finds_down_is_not_retried_tool_by_tool(tmp_path, registry):
    registry(provider("down1", "PROVIDER_DOWN"), provider("down2", "PROVIDER_DOWN"),
             provider("down3", "PROVIDER_DOWN"))
    async with Runtime(tmp_path) as web:
        result = await web.read(URL, WebPolicy(origin_route_hint_ttl_seconds=0,
                                               provider_retry_delay_seconds=0))
    # The first tool is retried (it may be flaky); once a second tool is down
    # too, the rest get one try each.
    assert tried(result) == ["down1", "down1", "down2", "down3"]


async def test_outages_do_not_count_as_walls(tmp_path, registry):
    registry(provider("down1", "PROVIDER_DOWN"), provider("down2", "PROVIDER_DOWN"),
             provider("free_ok", "ok"), provider("paid_ok", "ok", paid=True))
    async with Runtime(tmp_path) as web:
        result = await web.read(URL, WebPolicy(allow_paid_fallbacks=True, escalate_after_walls=1,
                                               provider_max_attempts_per_candidate=1,
                                               origin_route_hint_ttl_seconds=0))
    assert tried(result) == ["down1", "down2", "free_ok"]


async def test_origin_hint_puts_the_provider_that_got_through_first(tmp_path, registry):
    registry(provider("blocked", "BLOCKED"), provider("works", "ok"))
    async with Runtime(tmp_path) as web:
        first = await web.read(URL)
        second = await web.read(URL)
        other = await web.read("https://other.example.com/")
    assert tried(first) == ["blocked", "works"]
    assert tried(second) == ["works"]
    assert second["receipt"]["routing"]["provider_plan"]["origin_hint"] == "works"
    assert tried(other) == ["blocked", "works"]


async def test_origin_hint_is_dropped_when_it_fails_and_can_be_disabled(tmp_path, registry, monkeypatch):
    state = {"works": "ok"}

    class Flaky:
        manifest = ProviderManifest("works", "1")

        async def acquire(self, request, services):
            if state["works"] != "ok":
                raise WebFailure(state["works"], "fixture outcome")
            return {"url": request.url, "content": PAGE, "content_type": "text/html", "http_status": 200}

    registry(provider("blocked", "BLOCKED"), Flaky(), provider("last", "ok"))
    async with Runtime(tmp_path) as web:
        await web.read(URL)
        state["works"] = "BLOCKED"
        failed_hint = await web.read(URL)
        state["works"] = "ok"
        after = await web.read(URL, WebPolicy(origin_route_hint_ttl_seconds=0))
    assert tried(failed_hint)[0] == "works"
    assert tried(after) == ["blocked", "works"]
    assert WebPolicy().escalate_after_walls == 4
    with pytest.raises(ValueError):
        WebPolicy(escalate_after_walls=-1)


async def test_jina_challenge_is_a_wall_not_an_outage(tmp_path, monkeypatch):
    monkeypatch.setattr(hosted_providers, "TRANSPORT", httpx.MockTransport(
        lambda request: httpx.Response(200, json={"code": 200, "data": {
            "title": "x", "url": URL, "content": "", "httpStatus": 200,
            "warning": "This page maybe requiring CAPTCHA"}})))
    async with Runtime(tmp_path) as web:
        result = await web.read(URL, policy_overrides={"provider": "jina_reader"})
    assert result["receipt"]["failure"]["code"] == "CAPTCHA"
    assert len(result["receipt"]["attempts"]) == 1


async def test_not_found_after_a_wall_does_not_end_the_read(tmp_path, registry):
    registry(provider("walled", "CAPTCHA"), provider("fooled", "NOT_FOUND", paid=True),
             provider("unblocker", "ok", paid=True))
    async with Runtime(tmp_path) as web:
        result = await web.read(URL, WebPolicy(allow_paid_fallbacks=True, origin_route_hint_ttl_seconds=0))
    assert tried(result) == ["walled", "fooled", "unblocker"]
    assert result["receipt"]["status"] == "observed"


async def test_plain_not_found_still_ends_the_read(tmp_path, registry):
    registry(provider("gone", "NOT_FOUND"), provider("never", "ok"))
    async with Runtime(tmp_path) as web:
        result = await web.read(URL, WebPolicy(origin_route_hint_ttl_seconds=0))
    assert tried(result) == ["gone"]
    assert result["receipt"]["failure"]["code"] == "NOT_FOUND"


async def test_paid_tools_go_cheapest_likely_success_first_and_wait_for_a_free_favourite(tmp_path, registry, monkeypatch):
    from frankensurf import runtime

    def walled(identifier, *, paid=False, outcome="BLOCKED"):
        class Plugin:
            manifest = ProviderManifest(identifier, "1", paid=paid)

            async def acquire(self, request, services):
                if outcome == "ok":
                    return {"url": request.url, "content": PAGE, "content_type": "text/html", "http_status": 200}
                failure = WebFailure(outcome, "fixture outcome")
                failure.wall_vendor = "datadome"
                raise failure
        return Plugin()
    monkeypatch.setattr(runtime, "_paid_priors", lambda: {
        "dear": {"overall": [7, 8], "cost_usd": 0.05}, "cheap": {"overall": [6, 8], "cost_usd": 0.004}})
    # A paid tool with a better record against the vendor than the free favourite
    # still waits for the paid escalation.
    monkeypatch.setattr(runtime, "_wall_priors", lambda: {"datadome": {"favourite": [6, 8], "dear": [8, 8]}})
    registry(walled("w1"), walled("favourite"), walled("unknown", paid=True), walled("dear", paid=True),
             walled("cheap", paid=True, outcome="ok"))
    policy = dict(origin_route_hint_ttl_seconds=0, provider_max_attempts_per_candidate=1, warm_up_on_wall=False)
    async with Runtime(tmp_path) as web:
        result = await web.read(URL, policy_overrides={"allow_paid_fallbacks": True, **policy})
        later = await web.read(URL, policy_overrides={"allow_paid_fallbacks": True, "paid_after_walls": 0,
                                                      "freshness": "now", **policy})
    # The free favourite for this vendor gets its try; then the cheapest likely
    # success (0.004 at 6/8 beats 0.05 at 8/8); a tool with no known cost last.
    assert tried(result) == ["w1", "favourite", "cheap"]
    # paid_after_walls=0: paid tools wait for escalate_after_walls, whatever their record.
    assert tried(later)[:3] == ["w1", "favourite", "unknown"]
    assert WebPolicy().paid_after_walls == 1
