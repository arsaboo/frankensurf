"""Lightning mode: the read and two different tools start at once; the first complete page wins."""
import asyncio

import pytest

from frankensurf import providers
from frankensurf.providers import ProviderManifest, ProviderRegistry
from frankensurf.runtime import Runtime, WebFailure, WebPolicy

SEARCH = "https://shop.example.com/search?q=lamp"
CHROME = "<p>Home Shop Help Sign in Cart Stores Gift cards Delivery Returns Contact us</p>" * 8
SHELL = "<html><title>Search</title><body>" + CHROME + "<div id='results'></div></body></html>"
RESULTS = ("<html><title>Search</title><body>" + CHROME
           + "".join(f'<a href="/product/brass-desk-lamp-{n}/SKU{n:06d}">Lamp {n} $49</a>' for n in range(12))
           + "</body></html>")


def tool(identifier, content, delay, log, failure=None, **manifest):
    class Plugin:
        def __init__(self):
            self.manifest = ProviderManifest(identifier, "1", **manifest)

        def available(self, configured):
            return True

        async def acquire(self, request, services):
            log.append(("start", identifier))
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                log.append(("cancelled", identifier))
                raise
            if failure:
                raise WebFailure(failure, "fixture outcome")
            return {"url": request.url, "content": content, "content_type": "text/html", "http_status": 200}
    return Plugin()


@pytest.fixture
def install(monkeypatch):
    monkeypatch.setattr(Runtime, "completeness_enabled", True)
    monkeypatch.delenv("FRANKENSURF_LIGHTNING", raising=False)

    def go(*plugins):
        registry = ProviderRegistry()
        for plugin in plugins:
            registry.register(plugin)
        monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    return go


POLICY = {"origin_route_hint_ttl_seconds": 0, "origin_min_interval_seconds": 0, "second_opinion_text_chars": 0,
          "completeness_ladder": ["reader", "stealth"], "warm_up_on_wall": False}


async def test_the_first_complete_page_wins_and_the_rest_are_cancelled(tmp_path, install):
    log = []
    install(tool("slow_main", RESULTS, 5, log), tool("reader", RESULTS, 0, log, rendering=True),
            tool("stealth", RESULTS, 3, log, rendering=True))
    async with Runtime(tmp_path) as web:
        started = asyncio.get_running_loop().time()
        result = await web.read(SEARCH, policy_overrides={**POLICY, "lightning": True})
        elapsed = asyncio.get_running_loop().time() - started
    receipt = result["receipt"]
    assert receipt["method"] == "reader" and elapsed < 3
    assert receipt["hedge"]["lightning"] is True and receipt["hedge"]["won"] is True
    assert receipt["hedge"]["providers"] == ["reader", "stealth"]
    assert ("cancelled", "slow_main") in log
    # Racers start half a second apart: the stealth browser was cancelled or never started.
    assert ("start", "stealth") not in log or ("cancelled", "stealth") in log


async def test_without_lightning_the_racers_wait_for_the_hedge(tmp_path, install):
    log = []
    install(tool("quick_main", RESULTS, 0.2, log), tool("reader", RESULTS, 0, log, rendering=True),
            tool("stealth", RESULTS, 0, log, rendering=True))
    async with Runtime(tmp_path) as web:
        result = await web.read(SEARCH, policy_overrides=POLICY)
    assert result["receipt"]["method"] == "quick_main"
    assert ("start", "reader") not in log and "hedge" not in result["receipt"]


async def test_a_complete_main_page_stands_and_an_incomplete_one_waits_for_the_racers(tmp_path, install):
    log = []
    install(tool("main", SHELL, 0, log), tool("reader", SHELL, 0, log, rendering=True),
            tool("stealth", RESULTS, 1, log, rendering=True))
    async with Runtime(tmp_path) as web:
        result = await web.read(SEARCH, policy_overrides={**POLICY, "lightning": True,
                                                          "completeness_escalation": False})
    # The main read's page was incomplete, so the racers ran on and the stealth
    # browser's complete page won.
    assert result["receipt"]["method"] == "stealth" and result["receipt"]["hedge"]["won"] is True


async def test_when_the_main_read_fails_the_most_complete_racer_page_stands(tmp_path, install):
    log = []

    class Flaky:
        """Gets the page once (the racer, at half a second), then meets the wall (the main read)."""
        manifest = ProviderManifest("reader", "1", rendering=True)
        calls = 0

        def available(self, configured):
            return True

        async def acquire(self, request, services):
            type(self).calls += 1
            if type(self).calls > 1:
                raise WebFailure("CAPTCHA", "fixture wall")
            return {"url": request.url, "content": SHELL, "content_type": "text/html", "http_status": 200}
    install(tool("walled", "", 1, log, failure="BLOCKED"), Flaky(),
            tool("stealth", SHELL, 0, log, rendering=True, failure="CAPTCHA"))
    async with Runtime(tmp_path) as web:
        result = await web.read(SEARCH, policy_overrides={**POLICY, "lightning": True,
                                                          "completeness_escalation": False,
                                                          "provider_max_attempts_per_candidate": 1})
    receipt = result["receipt"]
    assert receipt["status"] == "observed" and receipt["method"] == "reader"
    assert receipt["hedge"]["after_failure"] in ("BLOCKED", "CAPTCHA")


async def test_lightning_can_be_switched_on_for_every_read(tmp_path, install, monkeypatch):
    log = []
    monkeypatch.setenv("FRANKENSURF_LIGHTNING", "1")
    install(tool("slow_main", RESULTS, 5, log), tool("reader", RESULTS, 0, log, rendering=True),
            tool("stealth", RESULTS, 3, log, rendering=True))
    async with Runtime(tmp_path) as web:
        result = await web.read(SEARCH, policy_overrides=POLICY)
    assert result["receipt"]["hedge"]["lightning"] is True
    with pytest.raises(ValueError):
        WebPolicy(lightning="yes")


PARTIAL = ("<html><title>Search</title><body>" + CHROME
           + "".join(f'<a href="/product/brass-desk-lamp-{n}/SKU{n:06d}">Lamp {n}</a>' for n in range(6))
           + "</body></html>")


async def test_an_explicit_no_beats_the_switch_and_no_tool_reads_the_page_twice(tmp_path, install, monkeypatch):
    log = []
    monkeypatch.setenv("FRANKENSURF_LIGHTNING", "1")
    install(tool("slow_main", RESULTS, 1, log), tool("reader", RESULTS, 3, log, rendering=True),
            tool("stealth", RESULTS, 3, log, rendering=True))
    async with Runtime(tmp_path) as web:
        declined = await web.read(SEARCH, policy_overrides={**POLICY, "lightning": False})
        log.clear()
        raced = await web.read(SEARCH + "&p=2", policy_overrides=POLICY)
    assert "hedge" not in declined["receipt"]
    # The main read leaves the racers' tools to the racers.
    assert raced["receipt"]["hedge"]["lightning"] is True
    assert log.count(("start", "reader")) <= 1 and log.count(("start", "stealth")) <= 1


async def test_when_nothing_is_complete_the_more_complete_page_stands(tmp_path, install):
    log = []
    install(tool("main", SHELL, 0, log), tool("reader", PARTIAL, 0.2, log, rendering=True),
            tool("stealth", SHELL, 0, log, rendering=True))
    async with Runtime(tmp_path) as web:
        result = await web.read(SEARCH, policy_overrides={**POLICY, "lightning": True,
                                                          "completeness_escalation": False})
    hedge = result["receipt"]["hedge"]
    assert result["receipt"]["method"] == "reader" and hedge["won"] is True and hedge["provider"] == "reader"


async def test_a_site_module_read_is_not_raced(tmp_path, install):
    from frankensurf.site_modules import ACTIVE
    log = []
    install(tool("slow_main", RESULTS, 0.5, log), tool("reader", RESULTS, 0, log, rendering=True),
            tool("stealth", RESULTS, 0, log, rendering=True))
    class Module:
        """Stands in for the site module a read is shaped by."""
        def invalid_page(self, *args):
            return False
    token = ACTIVE.set(Module())
    try:
        async with Runtime(tmp_path) as web:
            result = await web.read(SEARCH, policy_overrides={**POLICY, "lightning": True,
                                                              "completeness_escalation": False}, module=False)
    finally:
        ACTIVE.reset(token)
    assert result["receipt"]["method"] == "slow_main" and ("start", "reader") not in log


async def test_by_default_a_slow_read_races_two_tools_and_a_quick_one_none(tmp_path, install):
    log = []
    install(tool("slow_main", RESULTS, 5, log), tool("reader", SHELL, 0, log, rendering=True),
            tool("stealth", RESULTS, 0.1, log, rendering=True))
    async with Runtime(tmp_path) as web:
        slow = await web.read(SEARCH, policy_overrides={**POLICY, "hedge_after_seconds": 0.2})
    receipt = slow["receipt"]
    assert receipt["method"] == "stealth" and receipt["hedge"]["providers"] == ["reader", "stealth"]
    assert "lightning" not in receipt["hedge"] and ("cancelled", "slow_main") in log
    log.clear()
    install(tool("quick_main", RESULTS, 0.05, log), tool("reader", SHELL, 0, log, rendering=True),
            tool("stealth", RESULTS, 0, log, rendering=True))
    async with Runtime(tmp_path / "quick") as web:
        quick = await web.read(SEARCH, policy_overrides={**POLICY, "hedge_after_seconds": 0.5})
    assert quick["receipt"]["method"] == "quick_main" and ("start", "reader") not in log
    assert WebPolicy().hedge_after_seconds == 3.0 and WebPolicy().hedge_racers == 2
