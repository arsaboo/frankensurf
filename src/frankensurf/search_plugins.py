"""Typed search-source plugins and bounded source routing."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from decimal import Decimal
import json
import math
import re
from typing import Awaitable, Callable, Protocol

from .search import build_search, normalize_results


_PLUGIN_ID = re.compile(r"[a-z][a-z0-9_.-]{0,127}\Z")
_PLUGIN_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+:-]{0,191}\Z")
_FAILURE_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
_TRACE_ID = re.compile(r"[0-9a-f]{32}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _text(value, maximum, *, empty=True):
    if not isinstance(value, str) or not empty and not value:
        raise ValueError()
    try:
        if len(value.encode()) > maximum:
            raise ValueError()
    except UnicodeError:
        raise ValueError() from None
    return value


def _number(value, *, unknown=False):
    if value is None and unknown:
        return None
    if type(value) is int:
        if value < 0:
            raise ValueError()
        return value
    if type(value) is not float or not math.isfinite(value) or value < 0:
        raise ValueError()
    return value


def _url(value):
    from .runtime import WebFailure, _validate_url
    try:
        _validate_url(value)
    except WebFailure:
        raise ValueError() from None
    return value


def _copy_evidence(value, maximum, count_limit):
    if type(value) is not list or len(value) > count_limit:
        raise ValueError()
    output = []
    seen = {id(value)}
    for item in value:
        if (type(item) is not dict or id(item) in seen
                or set(item) != {"sha256", "path", "bytes"}
                or not isinstance(item.get("sha256"), str)
                or _SHA256.fullmatch(item["sha256"]) is None
                or type(item.get("bytes")) is not int
                or item["bytes"] < 0):
            raise ValueError()
        seen.add(id(item))
        path = _text(item.get("path"), maximum, empty=False)
        digits = max(1, int(item["bytes"].bit_length() * 0.30103) + 1)
        if digits > maximum:
            raise ValueError()
        output.append({"sha256": item["sha256"], "path": path,
                       "bytes": item["bytes"]})
    return output


def _copy_attempts(value, policy):
    from .provider_worker import FAILURE_STAGES
    from .providers import _validated_provider_attempt_tree
    maximum = policy.max_bytes
    limit = policy.provider_composition_max_attempts
    if type(value) is not list or len(value) > limit:
        raise ValueError()
    allowed = {"provider", "provider_version", "provider_binding_id",
               "status", "failure",
               "failure_stage", "latency_ms", "cost_usd", "final_url",
               "evidence", "children", "route_recipe"}
    output = []
    seen = {id(value)}
    for row in value:
        if (type(row) is not dict or id(row) in seen
                or not set(row) <= allowed
                or not {"provider", "status"} <= set(row)
                or not isinstance(row.get("provider"), str)
                or _PLUGIN_ID.fullmatch(row["provider"]) is None
                or row.get("status") not in {"observed", "failed"}):
            raise ValueError()
        seen.add(id(row))
        version = row.get("provider_version")
        if (version is not None and (not isinstance(version, str)
                or _PLUGIN_VERSION.fullmatch(version) is None)):
            raise ValueError()
        binding_id = row.get("provider_binding_id")
        if (binding_id is not None and (type(binding_id) is not str
                or _SHA256.fullmatch(binding_id) is None)):
            raise ValueError()
        failed = row["status"] == "failed"
        if failed != ("failure" in row):
            raise ValueError()
        saved = {"provider": row["provider"], "provider_version": version,
                 "status": row["status"]}
        if binding_id is not None:
            saved["provider_binding_id"] = binding_id
        if failed:
            failure = row["failure"]
            if (not isinstance(failure, str)
                    or _FAILURE_CODE.fullmatch(failure) is None):
                raise ValueError()
            saved["failure"] = failure
        if "failure_stage" in row:
            if not failed or row["failure_stage"] not in FAILURE_STAGES:
                raise ValueError()
            saved["failure_stage"] = row["failure_stage"]
        if "latency_ms" in row:
            saved["latency_ms"] = _number(row["latency_ms"])
        if "cost_usd" in row:
            saved["cost_usd"] = _number(row["cost_usd"], unknown=True)
        if "final_url" in row:
            saved["final_url"] = _url(row["final_url"])
        if "evidence" in row:
            saved["evidence"] = _copy_evidence(row["evidence"], maximum,
                                                maximum)
        if "children" in row:
            saved["children"] = _validated_provider_attempt_tree(
                row["children"],
                max_depth=policy.provider_composition_max_depth,
                max_nodes=limit, max_bytes=maximum)
        # Route metadata is accepted from Runtime receipts but omitted because
        # it is not needed to route or account a search-source result.
        output.append(saved)
    return output


_RECEIPT_KEYS = {
    "trace_id", "status", "operation", "action_class",
    "acquisition_context", "adapter",
    "adapter_version", "observed_at", "freshness_seconds", "cache_hit",
    "method", "identity", "http_status", "failure", "requested_url",
    "final_url", "confidence", "cost_usd", "cost_basis", "evidence",
    "attempts", "routing", "route_recipe", "authority_mode", "executor",
    "profile_version", "identity_generation", "executor_generation",
    "network_context", "geography", "authentication", "browser_execution",
    "provider_version", "visible_snapshot", "capture_freshness_seconds",
    "source_freshness", "source_refresh_performed",
    "requested_freshness_satisfied", "semantic_freshness", "navigation",
    "navigation_data_source", "failure_stage", "latency_ms", "max_cost_usd",
    "cost_budget_satisfied", "recovery", "search_source",
    "search_source_version", "provider_binding_id",
    "adapter_binding_id", "search_binding_id",
    # Fields later Core reads add: completeness checks, second opinions,
    # next-step hints, profiles and the try-harder lever.
    "completeness", "second_opinion", "next_step", "profile", "try_harder",
    "if_not_right", "quality",
}


def _copy_receipt(receipt, identifier, manifest, policy):
    maximum = policy.max_bytes
    required = {"trace_id", "status", "failure", "cost_usd", "evidence",
                "attempts"}
    if (type(receipt) is not dict or not required <= set(receipt)
            or not set(receipt) <= _RECEIPT_KEYS
            or not isinstance(receipt.get("trace_id"), str)
            or _TRACE_ID.fullmatch(receipt["trace_id"]) is None
            or receipt.get("status") not in {"observed", "failed"}
            or receipt.get("operation", "search") != "search"):
        raise ValueError()
    failed = receipt["status"] == "failed"
    failure = receipt["failure"]
    if failed:
        if (type(failure) is not dict or set(failure) != {"code", "message"}
                or not isinstance(failure.get("code"), str)
                or _FAILURE_CODE.fullmatch(failure["code"]) is None):
            raise ValueError()
        safe_failure = {"code": failure["code"],
                        "message": _text(failure.get("message"), maximum)}
    elif failure is not None:
        raise ValueError()
    else:
        safe_failure = None
    saved = {"trace_id": receipt["trace_id"], "status": receipt["status"],
             "operation": "search", "failure": safe_failure,
             "cost_usd": _number(receipt["cost_usd"], unknown=True),
             "evidence": _copy_evidence(receipt["evidence"], maximum, maximum),
             "attempts": _copy_attempts(receipt["attempts"], policy)}
    if receipt.get("action_class") not in (None, "READ_PUBLIC"):
        raise ValueError()
    saved["action_class"] = "READ_PUBLIC"
    for key in ("provider_binding_id", "adapter_binding_id",
                "search_binding_id"):
        value = receipt.get(key)
        if value is not None:
            if type(value) is not str or _SHA256.fullmatch(value) is None:
                raise ValueError()
            saved[key] = value
    if "latency_ms" in receipt:
        saved["latency_ms"] = _number(receipt["latency_ms"], unknown=True)
    from .runtime import _total_cost
    attempt_cost = _total_cost(
        [attempt.get("cost_usd", 0) for attempt in saved["attempts"]])
    if (saved["cost_usd"] is not None and attempt_cost is not None
            and saved["cost_usd"] < attempt_cost):
        raise ValueError()
    return saved


def _copy_attribution(value, request, identifier, manifest, maximum):
    if (type(value) is not dict
            or set(value) != {"query", "adapter", "source", "source_version",
                              "request_url"}
            or value.get("query") != request.query
            or value.get("adapter") != identifier
            or value.get("source") != identifier
            or value.get("source_version") != manifest.version):
        raise ValueError()
    return {"query": _text(value["query"], maximum), "adapter": identifier,
            "source": identifier, "source_version": manifest.version,
            "request_url": _url(value.get("request_url"))}


def _copy_extra(value, maximum):
    """Source-specific scalars on a result (points, publisher, stars): at most
    ten, named in snake_case, strings bounded like every other text."""
    import re as _re
    if type(value) is not dict or len(value) > 10:
        raise ValueError()
    copied = {}
    for key, item in value.items():
        if type(key) is not str or _re.fullmatch(r"[a-z][a-z0-9_]{0,31}", key) is None:
            raise ValueError()
        if item is None or type(item) in (bool, int):
            copied[key] = item
        elif type(item) is float and math.isfinite(item):
            copied[key] = item
        elif type(item) is str:
            copied[key] = _text(item, maximum) if item else item
        else:
            raise ValueError()
    return copied


def _copy_search_bundle(result, request, identifier, manifest):
    maximum = request.policy.max_bytes
    if (type(result) is not dict or set(result) != {"response", "acquisition"}
            or type(result["response"]) is not dict
            or type(result["acquisition"]) is not dict):
        raise ValueError()
    response, acquisition = result["response"], result["acquisition"]
    if set(response) != {"query", "source", "results", "receipt",
                         "query_attribution", "coverage", "upstream_failures"}:
        raise ValueError()
    raw_receipt = acquisition.get("receipt")
    if type(raw_receipt) is not dict or response.get("receipt") is not raw_receipt:
        raise ValueError()
    receipt = _copy_receipt(raw_receipt, identifier, manifest, request.policy)
    attribution = _copy_attribution(response.get("query_attribution"), request,
                                    identifier, manifest, maximum)
    if (response.get("query") != request.query
            or response.get("source") != identifier
            or response.get("coverage") not in {
                "unknown", "partial", "returned-results"}
            or type(response.get("results")) is not list
            or len(response["results"]) > request.limit):
        raise ValueError()
    results = []
    for item in response["results"]:
        if (type(item) is not dict
                or not {"url", "title", "snippet", "engines", "indexed_date", "listing_state",
                        "verification", "query_attribution"} <= set(item)
                or set(item) - {"url", "title", "snippet", "engines", "indexed_date", "published",
                                "listing_state", "verification", "query_attribution", "extra"}
                or item.get("listing_state") != "unknown"
                or item.get("verification") != "indexed_discovery"
                or type(item.get("engines")) is not list
                or len(item["engines"]) > maximum):
            raise ValueError()
        item_attribution = _copy_attribution(
            item.get("query_attribution"), request, identifier, manifest,
            maximum)
        engines = [_text(engine, maximum, empty=False)
                   for engine in item["engines"]]
        indexed = item.get("indexed_date")
        if indexed is not None:
            indexed = _text(indexed, maximum)
        copied = {"url": _url(item.get("url")),
                  "title": _text(item.get("title"), maximum),
                  "snippet": _text(item.get("snippet"), maximum),
                  "engines": engines, "indexed_date": indexed,
                  "listing_state": "unknown",
                  "verification": "indexed_discovery",
                  "query_attribution": item_attribution}
        if item.get("published") is not None:
            copied["published"] = _text(item["published"], maximum)
        if "extra" in item:
            copied["extra"] = _copy_extra(item["extra"], maximum)
        results.append(copied)
    failures = response.get("upstream_failures")
    if type(failures) is not list or len(failures) > maximum:
        raise ValueError()
    copied_failures = []
    for failure in failures:
        if (type(failure) not in (list, tuple) or len(failure) != 2
                or any(not isinstance(value, str) for value in failure)):
            raise ValueError()
        copied_failures.append([_text(value, maximum) for value in failure])
    acquisition_url = acquisition.get("url")
    if acquisition_url is not None:
        acquisition_url = _url(acquisition_url)
    safe_response = {"query": _text(request.query, maximum),
                     "source": identifier, "results": results,
                     "receipt": receipt, "query_attribution": attribution,
                     "coverage": response["coverage"],
                     "upstream_failures": copied_failures}
    safe = {"response": safe_response,
            "acquisition": {"url": acquisition_url, "receipt": receipt}}
    try:
        encoded = json.dumps(safe, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError, OverflowError, UnicodeError):
        raise ValueError() from None
    if len(encoded) > maximum:
        raise ValueError()
    return safe


def _safe_plugin_failure(error, maximum):
    from .runtime import WebFailure
    payload = error.__dict__
    code = payload.get("code")
    if not isinstance(code, str) or _FAILURE_CODE.fullmatch(code) is None:
        code = "PROVIDER_DOWN"
    try:
        message = _text(payload.get("message"), 4096)
    except ValueError:
        message = "Search plugin failed; invalid failure envelope"
    status = payload.get("http_status")
    if status is not None and (type(status) is not int or not 100 <= status <= 599):
        status = None
    cost = payload.get("cost_usd")
    try:
        cost = _number(cost, unknown=True)
        if (type(cost) is int
                and max(1, int(cost.bit_length() * 0.30103) + 1) > maximum):
            raise ValueError()
    except ValueError:
        code, message, cost = ("PROVIDER_DOWN",
                               "Search plugin failed; invalid failure envelope",
                               None)
    return WebFailure(code, message, status, cost_usd=cost)



@dataclass(frozen=True)
class SearchManifest:
    id: str
    version: str
    paid: bool = False
    cost_bounded: bool = False
    # Some sources expose a transport-level wire contract. Bundled HTML/RSS
    # search adapters require raw HTTP bytes; other plugins remain routed.
    transport_provider: str | None = None
    # web, or one kind of result (news, reference, discussions, qa, code,
    # papers, books). Automatic fallback stays inside one vertical.
    vertical: str = "web"


@dataclass(frozen=True)
class SearchRequest:
    query: str
    limit: int
    policy: object
    config: dict | None = None


@dataclass(frozen=True)
class SearchServices:
    read: Callable[..., Awaitable[dict]]


class SearchPlugin(Protocol):
    manifest: SearchManifest

    async def search(self, request: SearchRequest, services: SearchServices) -> dict: ...


class SearchRegistry:
    def __init__(self):
        self._plugins = {}
        self._disabled = set()
        self._frozen = False
        self._pending_cleanup = set()

    def register(self, plugin):
        if self._frozen:
            raise RuntimeError("Search registry is frozen")
        manifest = plugin.manifest
        if (not isinstance(manifest, SearchManifest) or not manifest.id or not manifest.version
                or not callable(getattr(plugin, "search", None))):
            raise ValueError("Invalid search plugin contract")
        if manifest.id in self._plugins:
            raise ValueError("Search plugin already registered")
        self._plugins[manifest.id] = plugin
        return manifest

    def clone(self):
        registry = type(self)()
        registry._plugins = self._plugins.copy()
        registry._disabled = self._disabled.copy()
        return registry

    def freeze(self):
        self._frozen = True
        return self

    def contains(self, identifier):
        return identifier in self._plugins

    def enable(self, identifier, enabled=True):
        if self._frozen:
            raise RuntimeError("Search registry is frozen")
        if identifier not in self._plugins:
            raise ValueError("Unknown search plugin")
        if enabled:
            self._disabled.discard(identifier)
        else:
            self._disabled.add(identifier)

    def inspect(self):
        from dataclasses import asdict
        return [{**asdict(plugin.manifest),
                 **({"binding_id": plugin.binding_id}
                    if (isinstance(getattr(plugin, "binding_id", None), str)
                        and _SHA256.fullmatch(plugin.binding_id) is not None)
                    else {}),
                 "enabled": identifier not in self._disabled}
                for identifier, plugin in sorted(self._plugins.items())]

    def binding_id(self, identifier):
        plugin = self._plugins.get(identifier)
        value = getattr(plugin, "binding_id", None)
        return (value if isinstance(value, str)
                and _SHA256.fullmatch(value) is not None else None)

    def require_enabled(self, identifier, policy):
        from .runtime import WebFailure
        if identifier not in self._plugins or identifier in self._disabled:
            raise WebFailure("PLUGIN_DISABLED", "Search source is unavailable or disabled")
        manifest = self._plugins[identifier].manifest
        if manifest.paid and not policy.allow_paid_fallbacks:
            raise WebFailure("POLICY_DENIED", "Paid search source requires an explicit policy grant")
        if manifest.paid and policy.max_cost_usd is not None and not manifest.cost_bounded:
            raise WebFailure("BUDGET_EXHAUSTED", "Search source cannot enforce the requested cost cap")
        return manifest

    def candidates(self, policy, *, explicit=None, vertical="web"):
        if explicit is not None:
            self.require_enabled(explicit, policy)
            selected = [explicit]
            if (policy.search_source_candidates is not None
                    and explicit not in policy.search_source_candidates):
                selected = []
        elif policy.search_source_candidates is not None:
            selected = list(policy.search_source_candidates)
        else:
            selected = list(self._plugins)
        if policy.search_source_allow is not None:
            allowed = set(policy.search_source_allow)
            selected = [identifier for identifier in selected if identifier in allowed]
        selected = [identifier for identifier in selected
                    if identifier in self._plugins and identifier not in self._disabled
                    and (explicit is not None or self._plugins[identifier].manifest.vertical == vertical)
                    and (not self._plugins[identifier].manifest.paid or policy.allow_paid_fallbacks)
                    and (explicit is not None or self._available(identifier))]
        if explicit is None and policy.search_source_prefer:
            preferred = [identifier for identifier in policy.search_source_prefer if identifier in selected]
            selected = preferred + [identifier for identifier in selected if identifier not in preferred]
        if policy.search_max_attempts is not None:
            selected = selected[:policy.search_max_attempts]
        return selected

    def _available(self, identifier):
        # Keyed sources without a configured key are skipped, not attempted.
        check = getattr(self._plugins[identifier], "available", None)
        try:
            return check is None or check() is True
        except Exception:
            return False

    def _consume_cleanup(self, task):
        self._pending_cleanup.discard(task)
        if not task.cancelled():
            task.exception()

    async def _stop_tasks(self, tasks, grace):
        pending = {task for task in tasks if task is not None and not task.done()}
        for task in pending:
            self._pending_cleanup.add(task)
            task.add_done_callback(self._consume_cleanup)
            task.cancel()
        if pending:
            # Callbacks are installed before this cancellation point, so a
            # repeatedly-cancelled caller cannot orphan task exceptions.
            await asyncio.wait(pending, timeout=grace)

    async def search(self, identifier, request, services):
        from .runtime import WebFailure, _measured_cost, _total_cost
        manifest = self.require_enabled(identifier, request.policy)
        timeout = request.policy.timeout_seconds
        cleanup = request.policy.provider_cleanup_grace_seconds
        if (type(timeout) not in (int, float) or not math.isfinite(timeout)
                or timeout <= 0 or type(cleanup) not in (int, float)
                or not math.isfinite(cleanup) or cleanup < 0):
            raise WebFailure("POLICY_DENIED", "Invalid search source deadline")

        completed_costs = []
        read_tasks = set()
        uncertain_read = [False]
        scope = {"state": "active"}
        read_lock = asyncio.Lock()

        async def tracked_read(url, supplied_policy=None, provider=None,
                               adapter=None, *, policy_overrides=None):
            if scope["state"] != "active":
                raise WebFailure("POLICY_DENIED",
                                 "Search source execution scope is closed")
            if ((supplied_policy is not None
                    and supplied_policy is not request.policy)
                    or provider is not None or policy_overrides is not None):
                raise WebFailure("POLICY_DENIED",
                    "Search plugins cannot widen acquisition policy")
            current = asyncio.current_task()
            if current is not None:
                read_tasks.add(current)
            service_started = False
            try:
                async with read_lock:
                    if scope["state"] != "active":
                        raise WebFailure("POLICY_DENIED",
                            "Search source execution scope is closed")
                    child_policy = request.policy
                    cap = request.policy.max_cost_usd
                    if cap is not None:
                        spent = _total_cost(completed_costs)
                        if spent is None or Decimal(str(spent)) > Decimal(str(cap)):
                            raise WebFailure("BUDGET_EXHAUSTED",
                                "No verified search acquisition budget remains",
                                cost_usd=spent)
                        remaining = float(Decimal(str(cap)) - Decimal(str(spent)))
                        child_policy = replace(request.policy,
                                               max_cost_usd=remaining)
                    service_started = True
                    acquisition = await services.read(
                        url, child_policy, adapter=adapter)
            except asyncio.CancelledError:
                if service_started:
                    uncertain_read[0] = True
                raise
            except WebFailure as error:
                if service_started:
                    completed_costs.append(_measured_cost(
                        error.__dict__.get("cost_usd")))
                raise
            except Exception:
                if service_started:
                    uncertain_read[0] = True
                raise
            else:
                receipt = (acquisition.get("receipt")
                           if type(acquisition) is dict else None)
                completed_costs.append(_measured_cost(
                    receipt.get("cost_usd") if type(receipt) is dict else None))
                aggregate = _total_cost(completed_costs)
                if (request.policy.max_cost_usd is not None
                        and (aggregate is None
                             or aggregate > request.policy.max_cost_usd)):
                    raise WebFailure("BUDGET_EXHAUSTED",
                        "Search acquisition exceeded the aggregate cost cap",
                        cost_usd=aggregate)
                return acquisition
            finally:
                if current is not None:
                    read_tasks.discard(current)

        plugin_services = SearchServices(read=tracked_read)

        def incurred_cost():
            if manifest.paid or uncertain_read[0]:
                return None
            return _total_cost(completed_costs)

        def execution_failure(code, message):
            return WebFailure(code, message, cost_usd=incurred_cost())

        async def invoke():
            return await self._plugins[identifier].search(request,
                                                          plugin_services)

        task = asyncio.create_task(invoke())
        try:
            done, _ = await asyncio.wait({task}, timeout=timeout)
        except asyncio.CancelledError:
            scope["state"] = "closing"
            uncertain_read[0] = uncertain_read[0] or bool(read_tasks)
            try:
                await self._stop_tasks({task, *read_tasks}, cleanup)
            finally:
                scope["state"] = "closed"
            raise
        if not done:
            scope["state"] = "closing"
            uncertain_read[0] = uncertain_read[0] or bool(read_tasks)
            try:
                await self._stop_tasks({task, *read_tasks}, cleanup)
            finally:
                scope["state"] = "closed"
            raise execution_failure(
                "TIMEOUT", "Search source deadline exceeded") from None

        # Close composition before inspecting or exposing any plugin result.
        # Delayed tasks inherit this mutable scope and are rejected before read.
        scope["state"] = "closing"
        lingering = {item for item in read_tasks
                     if item is not task and not item.done()}
        uncertain_read[0] = uncertain_read[0] or bool(lingering)
        try:
            await self._stop_tasks(lingering, cleanup)
        finally:
            scope["state"] = "closed"
        try:
            result = task.result()
        except asyncio.CancelledError:
            raise execution_failure(
                "PROVIDER_DOWN", "Search plugin cancelled its own operation") from None
        except WebFailure as error:
            reported = "cost_usd" in error.__dict__
            safe = _safe_plugin_failure(error, request.policy.max_bytes)
            if uncertain_read[0]:
                safe.cost_usd = None
            elif not reported:
                safe.cost_usd = incurred_cost()
            elif (safe.cost_usd is not None and completed_costs
                    and _total_cost(completed_costs) is not None
                    and safe.cost_usd < _total_cost(completed_costs)):
                raise execution_failure(
                    "PROVIDER_DOWN",
                    "Search plugin total is below known acquisition cost") from None
            cap = request.policy.max_cost_usd
            if cap is not None and (safe.cost_usd is None
                                    or safe.cost_usd > cap):
                raise WebFailure("BUDGET_EXHAUSTED",
                    "Search source exceeded the aggregate cost cap",
                    cost_usd=safe.cost_usd) from None
            raise safe from None
        except ValueError:
            raise execution_failure(
                "PROVIDER_DOWN", "Search plugin failed; invalid internal value") from None
        except Exception:
            raise execution_failure(
                "PROVIDER_DOWN",
                "Search plugin failed; raw exception data withheld") from None

        child_cost_unknown = (bool(completed_costs)
                              and _total_cost(completed_costs) is None)
        if uncertain_read[0] or child_cost_unknown:
            if request.policy.max_cost_usd is not None:
                raise WebFailure("BUDGET_EXHAUSTED",
                    "Search acquisition cost is unknown under the aggregate cap",
                    cost_usd=None) from None
            raise execution_failure(
                "PROVIDER_DOWN", "Search acquisition cost is unknown") from None
        if lingering:
            raise execution_failure(
                "PROVIDER_DOWN", "Search plugin left an acquisition running")
        try:
            safe = _copy_search_bundle(result, request, identifier, manifest)
        except (ValueError, TypeError, OverflowError, RecursionError, UnicodeError):
            raise execution_failure(
                "PROVIDER_DOWN",
                "Search plugin returned an invalid result envelope") from None
        reported_cost = safe["acquisition"]["receipt"]["cost_usd"]
        child_cost = _total_cost(completed_costs)
        if (reported_cost is not None and child_cost is not None
                and reported_cost < child_cost):
            raise execution_failure(
                "PROVIDER_DOWN",
                "Search plugin total is below known acquisition cost") from None
        cap = request.policy.max_cost_usd
        if cap is not None and (reported_cost is None or reported_cost > cap):
            raise WebFailure("BUDGET_EXHAUSTED",
                "Search source exceeded the aggregate cost cap",
                cost_usd=reported_cost) from None
        return safe, manifest

class HttpSearchPlugin:
    def __init__(self, identifier, version="1"):
        from .search import REGISTRY
        self.manifest = SearchManifest(identifier, version,
                                       transport_provider="http",
                                       vertical=REGISTRY[identifier].get("vertical", "web"))

    async def search(self, request, services):
        url, definition = build_search(request.query, self.manifest.id, request.config)
        adapter = "json" if definition["kind"] == "json" else "rss" if definition["kind"] == "rss" else "html"
        acquisition = await services.read(url, request.policy, adapter=adapter)
        receipt = acquisition["receipt"]
        receipt["operation"] = "search"
        receipt["search_source"] = self.manifest.id
        receipt["search_source_version"] = self.manifest.version
        attribution = {"query": request.query, "adapter": self.manifest.id,
                       "source": self.manifest.id, "source_version": self.manifest.version,
                       "request_url": url}
        response = {"query": request.query, "source": self.manifest.id, "results": [],
                    "receipt": receipt, "query_attribution": attribution,
                    "coverage": "unknown", "upstream_failures": []}
        if receipt.get("status") == "observed":
            try:
                found, failures = normalize_results(acquisition["content"], acquisition["structured"],
                                                    self.manifest.id, request.limit, url)
                response["upstream_failures"] = failures
                response["coverage"] = "partial" if failures else "returned-results"
                if not found and failures:
                    receipt.update(status="failed", failure={"code": "SEARCH_UNAVAILABLE",
                        "message": "No results; upstream search engines failed"})
                else:
                    response["results"] = [{**item, "listing_state": "unknown",
                        "verification": "indexed_discovery", "query_attribution": attribution}
                        for item in found]
            except ValueError:
                receipt.update(status="failed", failure={"code": "SCHEMA_CHANGED",
                    "message": "Search adapter could not validate result structure"})
        if receipt.get("failure"):
            attempts = receipt.get("attempts", [])
            if attempts:
                attempts[-1].update(status="failed", failure=receipt["failure"]["code"])
        return {"response": response, "acquisition": acquisition}


class HostedSearchPlugin:
    """A keyed search API (Exa, Brave, Tavily, Parallel, Firecrawl) read through its Core transport provider."""

    def __init__(self, identifier, transport, key):
        self.manifest = SearchManifest(identifier, "1", paid=True,
                                       transport_provider=transport)
        self._transport, self._key = transport, key

    def available(self):
        from .hosted_providers import _setting
        return bool(_setting(self._key))

    def _normalize(self, structured, limit):
        import re
        from .search import _iso
        found = []
        if self.manifest.id == "exa":
            rows = structured.get("results") if isinstance(structured, dict) else None
            if not isinstance(rows, list):
                raise ValueError("Expected Exa results array")
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("url"), str):
                    continue
                highlights = row.get("highlights")
                snippet = (" ".join(item for item in highlights if isinstance(item, str))
                           if isinstance(highlights, list) else (row.get("text") or "")[:500])
                found.append({"url": row["url"], "title": row.get("title") or "",
                              "snippet": snippet, "engines": ["exa"],
                              "indexed_date": row.get("publishedDate"),
                              "published": _iso(row.get("publishedDate"))})
        elif self.manifest.id == "firecrawl_search":
            data = structured.get("data") if isinstance(structured, dict) else None
            # v2 groups results by source ({"web": [...], "news": [...]}); v1 returned a list.
            rows = (data if isinstance(data, list) else
                    [row for group in ("web", "news") for row in (data.get(group) or [])
                     if isinstance(row, dict)] if isinstance(data, dict) else None)
            if rows is None:
                raise ValueError("Expected Firecrawl results")
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("url"), str):
                    continue
                metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
                when = (row.get("date") or metadata.get("publishedTime")
                        or metadata.get("article:published_time") or metadata.get("modifiedTime"))
                found.append({"url": row["url"], "title": row.get("title") or metadata.get("title") or "",
                              "snippet": (row.get("description") or metadata.get("description") or "")[:500],
                              "engines": ["firecrawl_search"], "indexed_date": when,
                              "published": _iso(when)})
        elif self.manifest.id in ("tavily", "parallel"):
            rows = structured.get("results") if isinstance(structured, dict) else None
            if not isinstance(rows, list):
                raise ValueError("Expected a results array")
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("url"), str):
                    continue
                excerpts = row.get("excerpts")
                snippet = (" ".join(item for item in excerpts if isinstance(item, str))[:500]
                           if isinstance(excerpts, list) else row.get("content") or "")
                found.append({"url": row["url"], "title": row.get("title") or "",
                              "snippet": snippet, "engines": [self.manifest.id],
                              "indexed_date": row.get("published_date") or row.get("publish_date"),
                              "published": _iso(row.get("published_date") or row.get("publish_date"))})
        else:
            web = structured.get("web") if isinstance(structured, dict) else None
            rows = web.get("results") if isinstance(web, dict) else None
            if not isinstance(rows, list):
                raise ValueError("Expected Brave web results array")
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("url"), str):
                    continue
                snippet = re.sub(r"<[^>]+>", "", row.get("description") or "")
                found.append({"url": row["url"], "title": row.get("title") or "",
                              "snippet": snippet, "engines": ["brave"],
                              "indexed_date": row.get("page_age") or row.get("age"),
                              "published": _iso(row.get("page_age"))})
        for item in found:
            if item.get("published") is None:
                item.pop("published", None)
        return found[:limit]

    async def search(self, request, services):
        from .hosted_providers import search_url
        url = search_url(self._transport, request.query, request.limit,
                         {key: value for key, value in (request.config or {}).items()
                          if key in ("site", "exclude_domains", "recency", "region")})
        acquisition = await services.read(url, request.policy, adapter="json")
        receipt = acquisition["receipt"]
        receipt["operation"] = "search"
        receipt["search_source"] = self.manifest.id
        receipt["search_source_version"] = self.manifest.version
        attribution = {"query": request.query, "adapter": self.manifest.id,
                       "source": self.manifest.id, "source_version": self.manifest.version,
                       "request_url": url}
        response = {"query": request.query, "source": self.manifest.id, "results": [],
                    "receipt": receipt, "query_attribution": attribution,
                    "coverage": "unknown", "upstream_failures": []}
        if receipt.get("status") == "observed":
            try:
                found = self._normalize(acquisition.get("structured"), request.limit)
                response["coverage"] = "returned-results"
                response["results"] = [{**item, "listing_state": "unknown",
                    "verification": "indexed_discovery", "query_attribution": attribution}
                    for item in found]
            except ValueError:
                receipt.update(status="failed", failure={"code": "SCHEMA_CHANGED",
                    "message": "Search API returned an unexpected result structure"})
        if receipt.get("failure"):
            attempts = receipt.get("attempts", [])
            if attempts:
                attempts[-1].update(status="failed", failure=receipt["failure"]["code"])
        return {"response": response, "acquisition": acquisition}


DEFAULT_SEARCHES = SearchRegistry()
from .search import REGISTRY as _HTTP_SOURCES
for _identifier in _HTTP_SOURCES:
    DEFAULT_SEARCHES.register(HttpSearchPlugin(_identifier))
DEFAULT_SEARCHES.register(HostedSearchPlugin("exa", "exa_api", "exa_api_key"))
DEFAULT_SEARCHES.register(HostedSearchPlugin("brave", "brave_api", "brave_api_key"))
DEFAULT_SEARCHES.register(HostedSearchPlugin("tavily", "tavily_api", "tavily_api_key"))
DEFAULT_SEARCHES.register(HostedSearchPlugin("parallel", "parallel_api", "parallel_api_key"))
DEFAULT_SEARCHES.register(HostedSearchPlugin("firecrawl_search", "firecrawl_search_api", "firecrawl_api_key"))
from .site_watch import SiteWatchPlugin
DEFAULT_SEARCHES.register(SiteWatchPlugin())
