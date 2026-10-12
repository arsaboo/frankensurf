"""Charges survive failed acquisitions and semantic rejection before fallback."""
import math

import pytest

from frankensurf import Runtime, WebPolicy, providers, adapters
from frankensurf.providers import ProviderManifest, ProviderRegistry
from frankensurf.adapters import AdapterManifest, AdapterRegistry
from frankensurf.runtime import WebFailure


def install(monkeypatch, definitions, *, bounded=False, calls=None):
    class Plugin:
        def __init__(self, name, paid, outcome):
            self.manifest = ProviderManifest(name, "1", paid=paid, cost_bounded=bounded)
            self.outcome = outcome

        async def acquire(self, request, services):
            if calls is not None:
                calls.append((self.manifest.id, request.policy.max_cost_usd))
            if isinstance(self.outcome, WebFailure):
                raise self.outcome
            return {"url": request.url, "content_type": "text/html", "http_status": 200,
                    "content": "<p>" + self.outcome.get("subject", "correct") + "</p>",
                    **({"cost_usd": self.outcome["cost_usd"]} if "cost_usd" in self.outcome else {})}

    class ExactAdapter:
        manifest = AdapterManifest("exact", "1")

        def extract(self, request):
            if "correct" not in request.content:
                raise WebFailure("SCHEMA_CHANGED", "Wrong subject")
            return {"title": "Correct subject", "text": "Correct subject", "image_urls": [], "structured": {"subject": "correct"}}

    registry = ProviderRegistry()
    for name, paid, outcome in definitions:
        registry.register(Plugin(name, paid, outcome))
    adapter_registry = AdapterRegistry()
    adapter_registry.register(ExactAdapter())
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    monkeypatch.setattr(adapters, "DEFAULT_ADAPTERS", adapter_registry)
    return WebPolicy(provider_candidates=tuple(row[0] for row in definitions), allow_paid_fallbacks=True)


async def observe(tmp_path, policy):
    async with Runtime(tmp_path, identity_registry=tmp_path / "unused-identities.json") as web:
        return await web.extract("https://cost.test/items/one", "exact", policy)


async def test_failed_paid_call_and_rejected_projection_are_charged_once_before_free_success(tmp_path, monkeypatch):
    policy = install(monkeypatch, [
        ("paid_timeout", True, WebFailure("TIMEOUT", "Model timed out", cost_usd=1.25)),
        ("paid_wrong_subject", True, {"subject": "foreign", "cost_usd": 0.75}),
        ("free", False, {}),
    ])
    result = await observe(tmp_path, policy)
    receipt = result["receipt"]
    assert receipt["status"] == "observed" and receipt["method"] == "free"
    assert receipt["cost_usd"] == 2.0
    assert [row["status"] for row in receipt["attempts"]] == ["failed", "failed", "observed"]
    assert [row.get("cost_usd") for row in receipt["attempts"][:2]] == [1.25, 0.75]
    assert receipt["attempts"][1]["failure"] == "SCHEMA_CHANGED"


@pytest.mark.parametrize("failure", [WebFailure("TIMEOUT", "No report"), WebFailure("TIMEOUT", "Unknown charge", cost_usd=None)])
async def test_unknown_paid_failure_is_not_erased_by_later_known_success(tmp_path, monkeypatch, failure):
    policy = install(monkeypatch, [("paid", True, failure), ("free", False, {"cost_usd": 0.0})])
    receipt = (await observe(tmp_path, policy))["receipt"]
    assert receipt["status"] == "observed"
    assert receipt["cost_usd"] is None
    assert receipt["attempts"][0]["cost_usd"] is None
    assert receipt["cost_basis"] == "provider reported; excludes hardware"


async def test_paid_exhaustion_retains_measured_cost_in_failed_receipt(tmp_path, monkeypatch):
    policy = install(monkeypatch, [
        ("first", True, WebFailure("BLOCKED", "Blocked after call", cost_usd=0.2)),
        ("second", True, WebFailure("PROVIDER_DOWN", "Provider failed", cost_usd=0.3)),
    ])
    receipt = (await observe(tmp_path, policy))["receipt"]
    assert receipt["status"] == "failed" and receipt["cost_usd"] == 0.5
    # The second provider being down says nothing about the page: the wall stands.
    assert receipt["failure"]["code"] == "BLOCKED"


async def test_finite_reports_that_overflow_aggregate_remain_unknown(tmp_path, monkeypatch):
    policy = install(monkeypatch, [
        ("first", True, WebFailure("BLOCKED", "First call", cost_usd=1e308)),
        ("second", True, {"cost_usd": 1e308}),
    ])
    receipt = (await observe(tmp_path, policy))["receipt"]
    assert receipt["status"] == "observed" and receipt["cost_usd"] is None
    assert all(math.isfinite(row["cost_usd"]) for row in receipt["attempts"])


async def test_model_cost_unknown_is_preserved_even_when_plugin_is_unmetered(tmp_path, monkeypatch):
    policy = install(monkeypatch, [("local_model", False, WebFailure("TIMEOUT", "Unknown measurement", cost_usd=None))])
    receipt = (await observe(tmp_path, policy))["receipt"]
    assert receipt["status"] == "failed" and receipt["cost_usd"] is None
    assert receipt["attempts"][0]["cost_usd"] is None


async def test_paid_denial_before_acquisition_has_no_charge(tmp_path, monkeypatch):
    policy = install(monkeypatch, [("paid", True, WebFailure("TIMEOUT", "Would cost", cost_usd=3.0))])
    from dataclasses import replace
    receipt = (await observe(tmp_path, replace(policy, allow_paid_fallbacks=False)))["receipt"]
    assert receipt["failure"]["code"] == "POLICY_DENIED"
    assert receipt["cost_usd"] == 0


async def test_paid_plugin_without_budget_control_never_runs_under_explicit_cap(tmp_path, monkeypatch):
    calls = []
    class LegacyPaid:
        manifest = ProviderManifest("legacy_paid", "1", paid=True)
        async def acquire(self, request, services):
            calls.append(request)
            raise AssertionError("Unbounded paid acquisition must not execute")
    registry = ProviderRegistry()
    registry.register(LegacyPaid())
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    policy = WebPolicy(provider="legacy_paid", allow_paid_fallbacks=True, max_cost_usd=0.5)
    async with Runtime(tmp_path, identity_registry=tmp_path / "unused-identities.json") as web:
        receipt = (await web.read("https://cost.test/items/one", policy))["receipt"]
    assert not calls and receipt["cost_usd"] == 0
    assert receipt["failure"]["code"] == "BUDGET_EXHAUSTED"


async def test_bounded_paid_fallback_receives_residual_cap(tmp_path, monkeypatch):
    from dataclasses import replace
    calls = []
    policy = install(monkeypatch, [
        ("first", True, WebFailure("TIMEOUT", "Charged failed call", cost_usd=0.2)),
        ("second", True, {"cost_usd": 0.3}),
    ], bounded=True, calls=calls)
    receipt = (await observe(tmp_path, replace(policy, max_cost_usd=0.5)))["receipt"]
    assert calls == [("first", 0.5), ("second", 0.3)]
    assert receipt["status"] == "observed" and receipt["cost_usd"] == 0.5
    assert receipt["cost_budget_satisfied"] is True


@pytest.mark.parametrize("cost", [0.5, None])
async def test_unknown_or_exhausted_spend_skips_further_paid_calls_and_allows_free_fallback(tmp_path, monkeypatch, cost):
    from dataclasses import replace
    calls = []
    policy = install(monkeypatch, [
        ("first", True, WebFailure("TIMEOUT", "Failed call", cost_usd=cost)),
        ("second", True, {"cost_usd": 0.1}),
        ("free", False, {"cost_usd": 0.0}),
    ], bounded=True, calls=calls)
    receipt = (await observe(tmp_path, replace(policy, max_cost_usd=0.5)))["receipt"]
    assert calls == [("first", 0.5), ("free", 0)]
    assert receipt["status"] == "observed" and receipt["method"] == "free"
    assert receipt["attempts"][1]["failure"] == "BUDGET_EXHAUSTED"
    assert receipt["cost_usd"] == cost
    assert receipt["cost_budget_satisfied"] is (cost is not None)


@pytest.mark.parametrize("cost,strict", [(None, False), (None, True), (0.6, True)])
async def test_strict_cap_rejects_unverified_or_excess_charge_success(tmp_path, monkeypatch, cost, strict):
    from dataclasses import replace
    policy = install(monkeypatch, [("paid", True, {"cost_usd": cost})], bounded=True)
    receipt = (await observe(tmp_path, replace(policy, max_cost_usd=0.5, strict_cost_cap=strict)))["receipt"]
    assert receipt["status"] == "failed" and receipt["failure"]["code"] == "BUDGET_EXHAUSTED"
    assert receipt["cost_usd"] == cost and receipt["cost_budget_satisfied"] is False


async def test_known_excess_charge_keeps_the_page_and_stops_paid_calls(tmp_path, monkeypatch):
    from dataclasses import replace
    calls = []
    policy = install(monkeypatch, [("paid", True, {"cost_usd": 0.6}), ("second", True, {"cost_usd": 0.1})],
                     bounded=True, calls=calls)
    result = await observe(tmp_path, replace(policy, max_cost_usd=0.5))
    receipt = result["receipt"]
    assert receipt["status"] == "observed" and "Correct subject" in result["text"]
    assert receipt["cost_usd"] == 0.6 and receipt["cost_budget_satisfied"] is False
    assert receipt["budget"] == {"cap_usd": 0.5, "spent_usd": 0.6, "exceeded": True}
    assert calls == [("paid", 0.5)]


@pytest.mark.parametrize("second_recipe", [False, True])
async def test_recipe_spend_reduces_next_recipe_and_ordinary_fallback_cap(tmp_path, monkeypatch, second_recipe):
    from frankensurf.routes import PublicRouteRecipe
    calls = []
    install(monkeypatch, [
        ("baseline", True, {"cost_usd": 0.3}),
        ("recipe_first", True, WebFailure("TIMEOUT", "Recipe call charged", cost_usd=0.2)),
        ("recipe_second", True, {"cost_usd": 0.3}),
    ], bounded=True, calls=calls)
    monkeypatch.setattr(providers.DEFAULT_PROVIDERS, "candidates", lambda *args, **kwargs: ["baseline"])
    async with Runtime(tmp_path, identity_registry=tmp_path / "unused-identities.json") as web:
        for identifier in ["recipe_first"] + (["recipe_second"] if second_recipe else []):
            web.routes.register(PublicRouteRecipe(id=identifier, version="1", origin="https://cost.test",
                path_pattern=r"/items/one", operation="extract", provider=identifier, provider_version="1",
                adapter="exact", adapter_version="1",
                provenance={"source_commit": "a" * 40, "report_sha256": "b" * 64, "case_ids": ("cost-fixture",)}))
        result = await web.extract("https://cost.test/items/one", "exact",
            policy_overrides={"allow_paid_fallbacks": True, "max_cost_usd": 0.5})
    assert calls == [("recipe_first", 0.5), ("recipe_second" if second_recipe else "baseline", 0.3)]
    receipt = result["receipt"]
    assert receipt["status"] == "observed" and receipt["cost_usd"] == 0.5
    assert receipt["cost_budget_satisfied"] is True


async def test_catalog_execution_cannot_mutate_source_provider_binding(
        tmp_path, monkeypatch):
    class RotatingProvider:
        manifest = ProviderManifest("rotating", "binding_before", paid=True, cost_bounded=True)
        async def acquire(self, request, services):
            self.manifest = ProviderManifest("rotating", "binding_after", paid=True, cost_bounded=True)
            return {"url": request.url, "content_type": "text/html", "http_status": 200,
                    "content": "<p>Correct subject from changed model configuration</p>", "cost_usd": 0.2}
    source = RotatingProvider()
    registry = ProviderRegistry()
    registry.register(source)
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    policy = WebPolicy(provider="rotating", allow_paid_fallbacks=True,
                       max_cost_usd=0.5)
    async with Runtime(tmp_path, identity_registry=tmp_path / "unused-identities.json") as web:
        result = await web.read("https://cost.test/items/one", policy)
    async with Runtime(tmp_path / "next",
                       identity_registry=tmp_path / "unused-identities.json") as web:
        later = await web.read("https://cost.test/items/one", policy)
    assert source.manifest.version == "binding_before"
    source.manifest = ProviderManifest(
        "rotating", "binding_after", paid=True, cost_bounded=True)
    async with Runtime(tmp_path / "updated",
                       identity_registry=tmp_path / "unused-identities.json") as web:
        updated = await web.read("https://cost.test/items/one", policy)
    assert result["receipt"]["status"] == "observed"
    assert result["receipt"]["provider_version"] == "binding_before"
    assert result["receipt"]["cost_usd"] == 0.2
    assert later["receipt"]["status"] == "observed"
    assert later["receipt"]["provider_version"] == "binding_before"
    assert updated["receipt"]["status"] == "observed"
    assert updated["receipt"]["provider_version"] == "binding_after"


@pytest.mark.parametrize("change,code", [({"http_status": 403}, "BLOCKED"),
    ({"http_status": "200"}, "PROVIDER_DOWN"), ({"content": None}, "PROVIDER_DOWN"),
    ({"raw": b"oversized" * 1000}, "LIMIT_EXCEEDED")])
async def test_registry_rejection_preserves_provider_charge(tmp_path, monkeypatch, change, code):
    class ChargedProvider:
        manifest = ProviderManifest("charged", "1", paid=True, cost_bounded=True)
        async def acquire(self, request, services):
            return {"url": request.url, "content_type": "text/html", "http_status": 200,
                    "content": "<p>Paid observation</p>", "cost_usd": 0.2, **change}
    registry = ProviderRegistry()
    registry.register(ChargedProvider())
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    async with Runtime(tmp_path, identity_registry=tmp_path / "unused-identities.json") as web:
        result = await web.read("https://cost.test/items/one",
            WebPolicy(provider="charged", allow_paid_fallbacks=True, max_cost_usd=0.5, max_bytes=1024))
    assert result["receipt"]["failure"]["code"] == code
    assert result["receipt"]["cost_usd"] == 0.2


@pytest.mark.parametrize("value", [True, -1, math.inf, math.nan, "0", {}, 10 ** 1000])
def test_failure_cost_metadata_rejects_invalid_measurements(value):
    with pytest.raises(ValueError, match="Reported cost"):
        WebFailure("TIMEOUT", "Invalid report", cost_usd=value)
