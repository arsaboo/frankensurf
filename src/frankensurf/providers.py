"""Explicit trusted provider registration and compatibility acquisition plugins."""
import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
import json
import math
import re
import time
from typing import Protocol,Callable,Awaitable

from .actions import ACTION_CLASSES


# Each registry invocation owns its progress cell. Child tasks inherit the cell,
# while concurrent and nested acquisitions get distinct cells.
_PROVIDER_PROGRESS = ContextVar("frankensurf_provider_progress", default=None)
_PROVIDER_EXECUTION = ContextVar("frankensurf_provider_execution", default=None)

_ATTEMPT_SCHEMA = "frankensurf.provider-attempt-tree/v1"
_ATTEMPT_BILLING = "included_in_parent_cost"
_MAX_VERSION_LENGTH = 192
_PLUGIN_ID = re.compile(r"[a-z][a-z0-9_.-]{0,127}")
_PLUGIN_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+:-]{0,191}")
_FAILURE_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,127}")


@dataclass
class _ExecutionScope:
    ancestry: tuple
    children: list
    counter: list
    max_depth: int
    max_attempts: int
    max_bytes: int
    policy: object
    state: str = "active"
    child_tasks: set = field(default_factory=set)
    owner_task: object | None = None


def report_provider_stage(stage):
    from .provider_worker import FAILURE_STAGES
    progress = _PROVIDER_PROGRESS.get()
    if progress is not None and isinstance(stage, str) and stage in FAILURE_STAGES:
        progress[0] = stage


@dataclass(frozen=True)
class ProviderManifest:
    id: str
    version: str
    rendering: bool = False
    requires_local_browser: bool = False
    paid: bool = False
    authentication: bool = False
    navigation: bool = False
    route_scope_required: bool = False
    cost_bounded: bool = False
    operations: tuple[str, ...] = ("read", "extract")
    action_classes: tuple[str, ...] = ()
    action_contracts: tuple[str, ...] = ()
    diagnosis: bool = False
    # Serves stored copies, not the live page: only with policy.allow_archive.
    archive: bool = False
    # Fetches from the provider's own network, so the site sees another address
    # than this machine's: what gets past walls that block by address.
    remote: bool = False


@dataclass(frozen=True)
class ProviderRequest:
    url: str
    policy: object
    operation: str = "read"
    # Size of PDF bytes an earlier attempt already saw, so a paid tool can
    # estimate per-page charges before it is called.
    pdf_bytes: int | None = None


@dataclass(frozen=True)
class ProviderActionRequest:
    intent: object
    policy: object


@dataclass(frozen=True)
class ProviderServices:
    http: Callable[...,Awaitable[dict]]
    browser: Callable[...,Awaitable[dict]]
    isolated: Callable[...,Awaitable[dict]]
    # Runtime supplies these callbacks from its frozen startup catalog.  The
    # optional form preserves the small direct-plugin surface that predates
    # runtime-owned catalogs; production composition always binds them.
    acquire_provider: Callable[...,Awaitable[dict]] | None = None
    provider_manifest: Callable[...,object] | None = None
    # Core binds this only for the exact named-identity operation. Plugins never
    # receive the browser context, profile material, cookies, or registry.
    authenticated: Callable[...,Awaitable[dict]] | None = None
    # The callback remains inside Core and grants one attested local action run.
    # It passes only a loopback executor reference to the selected trusted plugin;
    # browser state, cookies, credentials and registry records are never copied.
    authenticated_action: Callable[...,Awaitable[dict]] | None = None
    # Plugins may stage bounded public failure bytes through this callback. Core
    # owns persistence, paths, descriptors and receipt linkage; the callback is
    # scoped to one active provider request and grants no evidence-store access.
    retain_failure_evidence: Callable[[bytes, str], bool] | None = None


async def acquire_child_provider(services, identifier, request):
    """Acquire a composite child through the caller's provider registry."""
    callback = getattr(services, "acquire_provider", None)
    if callback is not None:
        return await callback(identifier, request)
    # Compatibility for direct plugin callers. Runtime never takes this path.
    return await DEFAULT_PROVIDERS.acquire(identifier, request, services)


def child_provider_manifest(services, identifier, policy):
    callback = getattr(services, "provider_manifest", None)
    if callback is not None:
        return callback(identifier, policy)
    # Compatibility for direct plugin callers. Runtime never takes this path.
    return DEFAULT_PROVIDERS.require_enabled(identifier, policy)


def provider_attempt_tree(attempts):
    """Mark child attempts as included in the parent acquisition bill."""
    return {"schema": _ATTEMPT_SCHEMA,
            "billing": _ATTEMPT_BILLING, "attempts": attempts}


def _bounded_text(value, maximum=None):
    if (not isinstance(value, str) or not value
            or any(ord(character) < 32 or ord(character) == 127
                   for character in value)):
        return False
    if maximum is None:
        return True
    try:
        return len(value.encode()) <= maximum
    except UnicodeError:
        return False


def _integer_within_budget(value, maximum):
    if type(value) is not int or value < 0:
        return False
    if maximum is None:
        return True
    # Avoid converting an adversarial huge integer to decimal before proving
    # that its serialized representation can fit the request byte budget.
    decimal_digits = max(1, int(value.bit_length() * 0.30103) + 1)
    return decimal_digits <= maximum


def _evidence_references(value, *, seen=None, charge=None, max_bytes=None):
    if type(value) is not list:
        raise ValueError()
    if max_bytes is not None and len(value) > max_bytes:
        raise ValueError()
    seen = set() if seen is None else seen
    if id(value) in seen:
        raise ValueError()
    seen.add(id(value))
    rows = []
    for item in value:
        if id(item) in seen:
            raise ValueError()
        seen.add(id(item))
        if (type(item) is not dict
                or len(item) != 3
                or set(item) != {"sha256", "path", "bytes"}
                or not isinstance(item.get("sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is None
                or not _bounded_text(item.get("path"), max_bytes)
                or not _integer_within_budget(item.get("bytes"), max_bytes)):
            raise ValueError()
        if charge is not None:
            charge(128 + len(item["path"].encode())
                   + max(1, int(item["bytes"].bit_length() * 0.30103) + 1))
        rows.append(dict(item))
    if max_bytes is not None and charge is None:
        try:
            encoded = json.dumps(
                rows, sort_keys=True, separators=(",", ":"),
                allow_nan=False).encode()
        except (TypeError, ValueError, OverflowError):
            raise ValueError() from None
        if len(encoded) > max_bytes:
            raise ValueError()
    return rows


def _validated_provider_attempt_tree(tree, *, max_depth=None,
                                     max_nodes=None, max_bytes=None):
    """Return a policy-bounded closed-schema copy, or reject the subtree."""
    from .provider_worker import FAILURE_STAGES
    for value in (max_depth, max_nodes, max_bytes):
        if value is not None and (type(value) is not int or value < 1):
            raise ValueError()
    count = [0]
    charged = [0]
    seen = set()

    def charge(amount):
        charged[0] += amount
        if max_bytes is not None and charged[0] > max_bytes:
            raise ValueError()

    def visit(node, depth):
        if ((max_depth is not None and depth > max_depth)
                or type(node) is not dict or id(node) in seen
                or len(node) != 3
                or set(node) != {"schema", "billing", "attempts"}
                or node.get("schema") != _ATTEMPT_SCHEMA
                or node.get("billing") != _ATTEMPT_BILLING
                or type(node.get("attempts")) is not list
                or (max_nodes is not None
                    and len(node["attempts"]) > max_nodes)
                or id(node["attempts"]) in seen):
            raise ValueError()
        seen.add(id(node)); seen.add(id(node["attempts"]))
        charge(96)
        output = []
        for row in node["attempts"]:
            count[0] += 1
            allowed = {"provider", "provider_version", "provider_binding_id",
                       "status", "cost_usd",
                       "latency_ms", "failure", "failure_stage", "message", "evidence",
                       "children"}
            if ((max_nodes is not None and count[0] > max_nodes)
                    or type(row) is not dict or id(row) in seen
                    or len(row) > len(allowed) or not set(row) <= allowed
                    or not {"provider", "provider_version", "status", "cost_usd",
                            "latency_ms"} <= set(row)
                    or not isinstance(row.get("provider"), str)
                    or _PLUGIN_ID.fullmatch(row["provider"]) is None
                    or (row.get("provider_version") is not None
                        and (not _bounded_text(row["provider_version"],
                                              _MAX_VERSION_LENGTH)
                             or _PLUGIN_VERSION.fullmatch(
                                 row["provider_version"]) is None))
                    or (row.get("provider_binding_id") is not None
                        and (type(row["provider_binding_id"]) is not str
                             or re.fullmatch(r"[0-9a-f]{64}",
                                             row["provider_binding_id"])
                             is None))
                    or row.get("status") not in {"observed", "failed"}
                    or (row.get("cost_usd") is not None
                        and not ((type(row["cost_usd"]) is int
                                  and _integer_within_budget(
                                      row["cost_usd"], max_bytes))
                                 or (type(row["cost_usd"]) is float
                                     and math.isfinite(row["cost_usd"])
                                     and row["cost_usd"] >= 0)))
                    or not _integer_within_budget(row.get("latency_ms"),
                                                  max_bytes)
                    or (row["status"] == "failed") != ("failure" in row)
                    or ("failure" in row
                        and (not isinstance(row["failure"], str)
                             or _FAILURE_CODE.fullmatch(
                                 row["failure"]) is None))
                    or ("failure_stage" in row
                        and (row["status"] != "failed"
                             or row["failure_stage"] not in FAILURE_STAGES))
                    or ("message" in row
                        and (row["status"] != "failed"
                             or not _bounded_text(row["message"], 200)))):
                raise ValueError()
            seen.add(id(row))
            charge(160 + len(row["provider"].encode())
                   + (len(row["provider_version"].encode())
                      if row["provider_version"] is not None else 0)
                   + (len(row["provider_binding_id"])
                      if row.get("provider_binding_id") is not None else 0))
            saved = {key: value for key, value in row.items()
                     if key not in {"evidence", "children"}}
            if "evidence" in row:
                saved["evidence"] = _evidence_references(
                    row["evidence"], seen=seen, charge=charge,
                    max_bytes=max_bytes)
            if "children" in row:
                saved["children"] = visit(row["children"], depth + 1)
            output.append(saved)
        return {"schema": _ATTEMPT_SCHEMA,
                "billing": _ATTEMPT_BILLING, "attempts": output}

    result = visit(tree, 0)
    if max_bytes is not None:
        try:
            encoded = json.dumps(result, sort_keys=True, separators=(",", ":"),
                                 allow_nan=False).encode()
        except (TypeError, ValueError, OverflowError):
            raise ValueError() from None
        if len(encoded) > max_bytes:
            raise ValueError()
    return result

def child_provider_attempt(identifier, version, status, started, cost, *,
                           result=None, error=None):
    """Build one sanitized child node shared by every composite provider."""
    row = {"provider": identifier, "provider_version": version,
           "status": status, "cost_usd": cost,
           "latency_ms": round((time.monotonic() - started) * 1000)}
    if error is not None:
        row["failure"] = error.code
        if error.failure_stage:
            row["failure_stage"] = error.failure_stage
        if error._public_failure_evidence:
            row["evidence"] = _evidence_references(error._public_failure_evidence)
        if getattr(error, "provider_attempts", None):
            row["children"] = _validated_provider_attempt_tree(error.provider_attempts)
    elif result is not None:
        if result.get("acquisition_evidence"):
            row["evidence"] = _evidence_references(result["acquisition_evidence"])
        if result.get("provider_attempts"):
            row["children"] = _validated_provider_attempt_tree(result["provider_attempts"])
    return row


async def acquire_composite_child(services, identifier, url, base_policy,
                                  costs, attempts, *, render=None):
    """Acquire and account one child under a composite provider deadline."""
    from dataclasses import replace
    from .runtime import (WebFailure, _measured_cost, _remaining_cost_policy)
    started = time.monotonic()
    version = None
    attempt_cost = 0
    try:
        changes = {"provider": identifier, "provider_candidates": None}
        if render is not None:
            changes["render"] = render
            if render is False:
                # A non-rendered supporting source cannot satisfy a DOM
                # readiness preference; the composite's rendered child remains
                # responsible for returning the parent's readiness metadata.
                changes["content_ready_selector"] = None
        child_policy = replace(base_policy, **changes)
        manifest = child_provider_manifest(services, identifier, child_policy)
        version = getattr(manifest, "version", None)
        child_policy = _remaining_cost_policy(child_policy, costs,
                                              paid=manifest.paid)
        if manifest.paid:
            # Once a paid plugin begins, absent reporting is unknown.
            attempt_cost = None
        result = await acquire_child_provider(
            services, identifier, ProviderRequest(url, child_policy))
        if manifest.paid or "cost_usd" in result:
            attempt_cost = _measured_cost(result.get("cost_usd"))
        costs.append(attempt_cost)
        attempts.append(child_provider_attempt(identifier, version, "observed",
            started, attempt_cost, result=result))
        return result
    except WebFailure as error:
        if hasattr(error, "cost_usd"):
            attempt_cost = _measured_cost(error.cost_usd)
        try:
            row = child_provider_attempt(identifier, version, "failed",
                started, attempt_cost, error=error)
        except (ValueError, TypeError, OverflowError, RecursionError):
            error = WebFailure("PROVIDER_DOWN",
                "Invalid nested provider failure envelope")
            row = child_provider_attempt(identifier, version, "failed",
                started, attempt_cost, error=error)
        costs.append(attempt_cost)
        attempts.append(row)
        raise error
    except Exception:
        error = WebFailure("PROVIDER_DOWN",
            "Provider plugin failed; raw exception data withheld")
        costs.append(attempt_cost)
        attempts.append(child_provider_attempt(identifier, version, "failed",
            started, attempt_cost, error=error))
        raise error from None


def attach_provider_attempt_tree(error, attempts, cost):
    """Retain a sanitized child tree when a composite provider fails."""
    error.provider_attempts = _validated_provider_attempt_tree(
        provider_attempt_tree(attempts))
    # The outer provider attempt is billed once at this aggregate cost. A
    # nested leaf's individual cost remains descriptive inside the tree.
    error.cost_usd = cost
    return error


class ProviderPlugin(Protocol):
    manifest: ProviderManifest
    async def acquire(self, request: ProviderRequest, services: ProviderServices) -> dict: ...
    async def diagnose(self, request, services: ProviderServices) -> dict: ...


async def _close_execution_scope(scope, cleanup_seconds):
    """Close composition before receipt finalization and stop spawned children."""
    if scope.state != "active":
        return
    scope.state = "closing"
    tasks = [task for task in tuple(scope.child_tasks)
             if task is not scope.owner_task and not task.done()]
    for task in tasks:
        task.cancel()
        # Keep late cleanup failures observed even if the caller is cancelled
        # while waiting for the grace period. The scope's task set continues
        # tracking pending work until its normal callback removes it.
        task.add_done_callback(_consume_task_exception)
    try:
        if tasks:
            await asyncio.wait(tasks, timeout=cleanup_seconds)
    finally:
        # No inherited ContextVar may enter or mutate this receipt after the
        # enclosing acquisition has begun finalizing it.
        scope.state = "closed"


def _consume_task_exception(task):
    if task.cancelled():
        return
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        pass


def _attempt_tree_cost(tree):
    from .runtime import _total_cost
    return _total_cost([row["cost_usd"] for row in tree["attempts"]])


def _ledger_cost(nodes):
    from .runtime import _total_cost
    return _total_cost([_ledger_node_cost(node) for node in nodes])


def _ledger_node_cost(node):
    if node["status"] == "pending":
        if node.get("paid"):
            return None
        if node["children"]:
            return _ledger_cost(node["children"])
        if node.get("reported_children") is not None:
            return _attempt_tree_cost(node["reported_children"])
    return node["cost_usd"]


def _finish_ledger_node(node, status, *, cost, failure=None,
                        failure_stage=None, evidence=None):
    if (node is None or node["status"] != "pending"
            or node.get("owner") is not None
            and node["owner"].state == "closed"):
        return
    node["status"] = status
    node["cost_usd"] = cost
    node["latency_ms"] = round((time.monotonic() - node["started"]) * 1000)
    if failure is not None:
        node["failure"] = failure
    if failure_stage is not None:
        node["failure_stage"] = failure_stage
    if evidence:
        node["evidence"] = evidence


def _ledger_row(node, scope):
    status = node["status"]
    failure = node.get("failure")
    if status == "pending":
        status = "failed"
        failure = "TIMEOUT"
    row = {
        "provider": node["provider"],
        "provider_version": node["provider_version"],
        "status": status,
        "cost_usd": _ledger_node_cost(node),
        "latency_ms": node.get("latency_ms",
            round((time.monotonic() - node["started"]) * 1000)),
    }
    if node.get("provider_binding_id") is not None:
        row["provider_binding_id"] = node["provider_binding_id"]
    if failure is not None:
        row["failure"] = failure
    if node.get("failure_stage") is not None:
        row["failure_stage"] = node["failure_stage"]
    if node.get("evidence"):
        row["evidence"] = node["evidence"]
    if node["children"]:
        row["children"] = _ledger_tree(node["children"], scope)
    elif node.get("reported_children") is not None:
        row["children"] = node["reported_children"]
    return row


def _ledger_tree(nodes, scope):
    return _validated_provider_attempt_tree(
        provider_attempt_tree([_ledger_row(node, scope) for node in nodes]),
        max_depth=scope.max_depth, max_nodes=scope.max_attempts,
        max_bytes=scope.max_bytes)


def _reported_cost(value, max_bytes=None):
    if value is None:
        return None
    if type(value) is int:
        if not _integer_within_budget(value, max_bytes):
            raise ValueError()
        return value
    if type(value) is float and math.isfinite(value) and value >= 0:
        return value
    raise ValueError()


def _authoritative_total(reported, reported_present, child_total, *,
                         paid=False):
    if reported_present:
        if (reported is not None and child_total is not None
                and reported < child_total):
            raise ValueError()
        return reported
    # Child costs are a lower bound and provenance. A paid parent can add its
    # own fee, so absent an authoritative parent report its total is unknown.
    return None if paid else child_total


def _nested_policy(scope, requested, identifier):
    """Derive a child policy from its parent, accepting narrowing only."""
    from .runtime import WebFailure
    parent = scope.policy
    if type(requested) is not type(parent):
        raise WebFailure("POLICY_DENIED",
                         "Nested provider policy is incompatible")
    spent = _ledger_cost(scope.children) if scope.children else 0
    parent_cap = parent.max_cost_usd
    if parent_cap is not None:
        if spent is None:
            raise WebFailure("BUDGET_EXHAUSTED",
                             "Prior nested cost is unknown")
        remaining = max(0, parent_cap - spent)
    else:
        remaining = None
    requested_cap = requested.max_cost_usd
    if remaining is None:
        effective_cap = requested_cap
    elif requested_cap is None:
        effective_cap = remaining
    else:
        effective_cap = min(remaining, requested_cap)
    timeout_seconds = min(parent.timeout_seconds, requested.timeout_seconds)
    requested_selector = requested.content_ready_selector
    if requested_selector != parent.content_ready_selector:
        if not (requested_selector is None and requested.render is False):
            raise WebFailure("POLICY_DENIED",
                "Nested provider cannot replace the parent readiness selector")
    content_ready_timeout = min(parent.content_ready_timeout_seconds,
        requested.content_ready_timeout_seconds, timeout_seconds)
    return replace(parent,
        provider=identifier,
        provider_candidates=None,
        render=requested.render,
        content_ready_selector=requested_selector,
        content_ready_timeout_seconds=content_ready_timeout,
        max_cost_usd=effective_cap,
        max_bytes=min(parent.max_bytes, requested.max_bytes),
        timeout_seconds=timeout_seconds,
        provider_deadline_grace_seconds=min(
            parent.provider_deadline_grace_seconds,
            requested.provider_deadline_grace_seconds),
        provider_cleanup_grace_seconds=min(
            parent.provider_cleanup_grace_seconds,
            requested.provider_cleanup_grace_seconds),
        provider_composition_max_depth=min(
            parent.provider_composition_max_depth,
            requested.provider_composition_max_depth),
        provider_composition_max_attempts=min(
            parent.provider_composition_max_attempts,
            requested.provider_composition_max_attempts),
        provider_max_attempts_per_candidate=min(
            parent.provider_max_attempts_per_candidate,
            requested.provider_max_attempts_per_candidate),
        # A nested provider cannot erase or extend the operator's retry
        # cadence. The root Runtime owns outer retries; children inherit it.
        provider_retry_delay_seconds=parent.provider_retry_delay_seconds,
        provider_retry_failures=tuple(
            code for code in parent.provider_retry_failures
            if code in requested.provider_retry_failures))


def _bounded_json_object(value, maximum):
    if type(value) is not dict:
        raise ValueError()
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode()
        if len(encoded) > maximum:
            raise ValueError()
        decoded = json.loads(encoded)
    except (TypeError, ValueError, OverflowError, UnicodeError):
        raise ValueError() from None
    if type(decoded) is not dict:
        raise ValueError()
    return decoded


class ProviderRegistry:
    def __init__(self):self._plugins={};self._disabled=set();self._pending_cleanup=set();self._frozen=False
    def register(self,plugin):
        if self._frozen: raise RuntimeError("Provider registry is frozen")
        manifest=plugin.manifest
        if (not isinstance(manifest,ProviderManifest)
                or not isinstance(manifest.id, str)
                or _PLUGIN_ID.fullmatch(manifest.id) is None
                or not _bounded_text(manifest.version, _MAX_VERSION_LENGTH)
                or _PLUGIN_VERSION.fullmatch(manifest.version) is None
                or type(manifest.operations) is not tuple
                or not manifest.operations
                or any(operation not in {"read", "extract", "do"}
                       for operation in manifest.operations)
                or len(set(manifest.operations)) != len(manifest.operations)
                or type(manifest.action_classes) is not tuple
                or len(set(manifest.action_classes)) != len(manifest.action_classes)
                or any(action not in ACTION_CLASSES
                       for action in manifest.action_classes)
                or type(manifest.action_contracts) is not tuple
                or len(set(manifest.action_contracts))
                    != len(manifest.action_contracts)
                or bool(manifest.action_classes)
                    != bool(manifest.action_contracts)
                or (bool(manifest.action_classes)
                    and "do" not in manifest.operations)
                or any(not isinstance(contract, str)
                       or _PLUGIN_ID.fullmatch(contract) is None
                       for contract in manifest.action_contracts)
                or type(manifest.diagnosis) is not bool
                or type(manifest.route_scope_required) is not bool
                or not callable(getattr(plugin,"acquire",None))
                or (manifest.action_classes
                    and not callable(getattr(plugin, "perform", None)))
                or manifest.diagnosis
                and not callable(getattr(plugin,"diagnose",None))):
            raise ValueError("Invalid provider plugin contract")
        if manifest.action_contracts:
            from .web_do import require_action_contract
            try:
                contracts = tuple(require_action_contract(identifier)
                    for identifier in manifest.action_contracts)
            except PermissionError:
                raise ValueError("Invalid provider plugin contract") from None
            if any(any(required not in manifest.action_classes
                       for required in contract.required_action_classes)
                   for contract in contracts):
                raise ValueError("Invalid provider plugin contract")
        if manifest.id in self._plugins:raise ValueError("Provider already registered")
        self._plugins[manifest.id]=plugin
        return manifest
    def clone(self):
        registry = type(self)()
        registry._plugins = self._plugins.copy()
        registry._disabled = self._disabled.copy()
        return registry
    def freeze(self):
        self._frozen = True
        return self
    def contains(self,identifier):return identifier in self._plugins
    def enable(self,identifier,enabled=True):
        if self._frozen: raise RuntimeError("Provider registry is frozen")
        if identifier not in self._plugins:raise ValueError("Unknown provider")
        if enabled:self._disabled.discard(identifier)
        else:self._disabled.add(identifier)
    def inspect(self):
        from dataclasses import asdict
        return [{**asdict(plugin.manifest),
                 **({"binding_id": plugin.binding_id}
                    if (isinstance(getattr(plugin, "binding_id", None), str)
                        and re.fullmatch(r"[0-9a-f]{64}", plugin.binding_id))
                    else {}),
                 "enabled": name not in self._disabled}
                for name, plugin in sorted(self._plugins.items())]
    def binding_id(self, identifier):
        plugin = self._plugins.get(identifier)
        value = getattr(plugin, "binding_id", None)
        return (value if isinstance(value, str)
                and re.fullmatch(r"[0-9a-f]{64}", value) else None)
    def candidates(self, policy, *, configured=(), operation="read"):
        """Return generic capability-compatible public providers in catalog order."""
        if operation not in {"read", "extract", "do"}:
            return []
        if policy.identity:
            return []
        selected = []
        for identifier, plugin in self._plugins.items():
            manifest = plugin.manifest
            if (identifier in self._disabled or manifest.authentication
                    or manifest.route_scope_required):
                continue
            if operation not in manifest.operations:
                continue
            if ((policy.render or policy.content_ready_selector is not None)
                    and not manifest.rendering):
                continue
            if manifest.paid and not policy.allow_paid_fallbacks: continue
            if manifest.archive and not getattr(policy, "allow_archive", False): continue
            if manifest.requires_local_browser and not policy.allow_local_browser: continue
            try:
                available = getattr(plugin, "available", None)
                availability = (True if available is None
                                else available(configured))
            except Exception:
                # Availability is a plugin planning hook. A broken hook must
                # neither disclose its exception nor make the plugin eligible
                # for execution.
                continue
            if availability is not True: continue
            selected.append(identifier)
        return selected
    def is_available(self, identifier, *, configured=()):
        """Report an installed plugin's dependency availability, fail closed."""
        plugin = self._plugins.get(identifier)
        if plugin is None or identifier in self._disabled:
            return False
        try:
            available = getattr(plugin, "available", None)
            return available is None or available(configured) is True
        except Exception:
            return False
    def require_enabled(self,identifier,policy,*,operation=None):
        from .runtime import WebFailure
        if identifier not in self._plugins or identifier in self._disabled:raise WebFailure("PLUGIN_DISABLED","Provider is unavailable or disabled")
        manifest=self._plugins[identifier].manifest
        if (operation is not None
                and operation not in {"read", "extract", "do"}):
            raise WebFailure("POLICY_DENIED", "Unsupported provider operation")
        if operation is not None and operation not in manifest.operations:
            raise WebFailure("POLICY_DENIED",
                "Provider does not declare the requested operation")
        if ((policy.render or policy.content_ready_selector is not None)
                and not manifest.rendering):
            raise WebFailure("VISUAL_REQUIRED",
                "Provider cannot satisfy required browser rendering")
        if manifest.paid and not policy.allow_paid_fallbacks:raise WebFailure("POLICY_DENIED","Paid provider requires an explicit policy grant")
        if manifest.paid and getattr(policy, "max_cost_usd", None) is not None and not manifest.cost_bounded:
            raise WebFailure("BUDGET_EXHAUSTED", "Provider cannot enforce the requested cost cap; select a budget-aware provider or change the cost policy")
        if manifest.requires_local_browser and not policy.allow_local_browser:raise WebFailure("POLICY_DENIED","Provider requires local browser permission")
        if policy.identity and not manifest.authentication:
            raise WebFailure("IDENTITY_POLICY_DENIED",
                "Named identity requires an authenticated provider")
        if not policy.identity and manifest.authentication:
            raise WebFailure("IDENTITY_REQUIRED",
                "Authenticated provider requires a named identity")
        return manifest
    def action_candidates(self, policy, action_class, contract):
        """Return installed authenticated do providers allowed by WebPolicy."""
        from .web_do import require_action_contract
        try:
            operation_contract = require_action_contract(contract)
        except PermissionError:
            return []
        if (operation_contract.explicit_provider_required
                or action_class != operation_contract.action_class
                or contract not in policy.browser_do_allowed_contracts
                or any(required not in policy.action_classes
                       for required in
                       operation_contract.required_action_classes)):
            return []
        selected = []
        for identifier, plugin in self._plugins.items():
            manifest = plugin.manifest
            if (identifier in self._disabled or "do" not in manifest.operations
                    or any(required not in manifest.action_classes
                           for required in
                           operation_contract.required_action_classes)
                    or contract not in manifest.action_contracts
                    or not manifest.authentication):
                continue
            if manifest.paid and not policy.allow_paid_fallbacks:
                continue
            if manifest.requires_local_browser and not policy.allow_local_browser:
                continue
            try:
                available = getattr(plugin, "available", None)
                if available is not None and available(()) is not True:
                    continue
            except Exception:
                continue
            selected.append(identifier)
        return selected

    def require_action_enabled(self, identifier, policy, action_class,
                               contract, *, intent=None):
        from .runtime import WebFailure
        from .actions import require_action
        from .web_do import require_action_contract
        # The registry is an execution boundary in its own right. Callers that
        # compose providers directly must not bypass Runtime's policy check.
        try:
            operation_contract = require_action_contract(contract)
        except PermissionError as error:
            raise WebFailure("POLICY_DENIED", str(error)) from None
        if action_class != operation_contract.action_class:
            raise WebFailure("POLICY_DENIED",
                "Intent action class does not match its operation contract")
        for required_action_class in operation_contract.required_action_classes:
            require_action(policy, required_action_class)
        if contract not in policy.browser_do_allowed_contracts:
            raise WebFailure("POLICY_DENIED",
                "Action operation contract is outside WebPolicy")
        if (operation_contract.explicit_provider_required
                and policy.provider != identifier):
            raise WebFailure("POLICY_DENIED",
                "Action operation contract requires an explicit provider")
        if (operation_contract.requires_explicit_origins
                and policy.browser_do_allowed_origins is None):
            raise WebFailure("POLICY_DENIED",
                "Action operation contract requires explicit origins")
        if intent is not None:
            if (getattr(intent, "contract", None) != contract
                    or getattr(intent, "action_class", None) != action_class):
                raise WebFailure("POLICY_DENIED",
                    "Action request does not match its operation contract")
            try:
                operation_contract.validate(intent)
                intent.validate_policy(policy)
            except (PermissionError, ValueError):
                raise WebFailure("POLICY_DENIED",
                    "Action request exceeds its operation policy") from None
            if operation_contract.validation == "raw_control":
                used_tools = tuple(dict.fromkeys(
                    action.tool for action in intent.actions))
                if (len(policy.browser_do_allowed_tools) != len(used_tools)
                        or set(policy.browser_do_allowed_tools)
                            != set(used_tools)):
                    raise WebFailure("POLICY_DENIED",
                        "Raw browser control requires the exact action tools")
                from .browser_use_config import origin
                if origin(intent.url) not in policy.browser_do_allowed_origins:
                    raise WebFailure("POLICY_DENIED",
                        "Action target is outside the explicit origin scope")
                try:
                    for allowed_origin in policy.browser_do_allowed_origins:
                        operation_contract.validate_origin(allowed_origin)
                except PermissionError as error:
                    raise WebFailure("POLICY_DENIED", str(error)) from None
        elif operation_contract.validation == "raw_control":
            raise WebFailure("POLICY_DENIED",
                "Raw browser control requires an exact action request")
        if identifier not in self._plugins or identifier in self._disabled:
            raise WebFailure("PLUGIN_DISABLED",
                "Action provider is unavailable or disabled")
        manifest = self._plugins[identifier].manifest
        if ("do" not in manifest.operations
                or any(required not in manifest.action_classes
                       for required in
                       operation_contract.required_action_classes)
                or contract not in manifest.action_contracts):
            raise WebFailure("POLICY_DENIED",
                "Action provider does not support the requested action class")
        if not manifest.authentication or not policy.identity:
            raise WebFailure("IDENTITY_REQUIRED",
                "Action provider requires a named identity")
        if manifest.paid and not policy.allow_paid_fallbacks:
            raise WebFailure("POLICY_DENIED",
                "Paid provider requires an explicit policy grant")
        if (manifest.requires_local_browser
                and not policy.allow_local_browser):
            raise WebFailure("POLICY_DENIED",
                "Action provider requires local browser permission")
        try:
            available = getattr(self._plugins[identifier], "available", None)
            if available is not None and available(()) is not True:
                raise WebFailure("PROVIDER_UNAVAILABLE",
                    "Action provider dependency is unavailable")
        except WebFailure:
            raise
        except Exception:
            raise WebFailure("PROVIDER_UNAVAILABLE",
                "Action provider dependency check failed") from None
        return manifest

    async def perform(self, identifier, request, services):
        """Execute one typed action through a catalogued provider boundary."""
        from .runtime import WebFailure
        action_class = getattr(request.intent, "action_class", None)
        manifest = self.require_action_enabled(
            identifier, request.policy, action_class,
            getattr(request.intent, "contract", None), intent=request.intent)
        if services is None or services.authenticated_action is None:
            raise WebFailure("PROVIDER_UNAVAILABLE",
                "Authenticated action service is unavailable")
        active = True

        async def public_denied(*args, **kwargs):
            raise WebFailure("IDENTITY_POLICY_DENIED",
                "Authenticated action providers cannot use public transports")

        async def scoped_action(action_request, runner):
            nonlocal active
            if not active:
                raise WebFailure("POLICY_DENIED",
                    "Action provider execution scope is closed")
            return await services.authenticated_action(action_request, runner)

        plugin_services = ProviderServices(
            public_denied, public_denied, public_denied,
            authenticated_action=scoped_action)
        try:
            result = await asyncio.wait_for(
                self._plugins[identifier].perform(request, plugin_services),
                timeout=request.policy.timeout_seconds
                    + request.policy.provider_deadline_grace_seconds)
        except TimeoutError:
            raise WebFailure("TIMEOUT",
                "Authenticated action provider deadline exceeded") from None
        except WebFailure as error:
            if (not isinstance(error.code, str)
                    or _FAILURE_CODE.fullmatch(error.code) is None):
                raise WebFailure("PROVIDER_DOWN",
                    "Action provider returned an invalid failure") from None
            raise WebFailure(error.code, error.message) from None
        except asyncio.CancelledError:
            raise
        except Exception:
            raise WebFailure("PROVIDER_DOWN",
                "Action provider failed; raw exception data withheld") from None
        finally:
            active = False
        try:
            return _bounded_json_object(
                result, request.policy.browser_do_packet_max_bytes)
        except (ValueError, TypeError, OverflowError, UnicodeError):
            raise WebFailure("PROVIDER_DOWN",
                "Action provider returned an invalid result envelope") from None

    async def diagnose(self, identifier, request, services):
        """Run one exact diagnosis edge without broader acquisition authority."""
        from .repair import RepairProviderRequest
        from .runtime import WebFailure, _validate_url
        from .actions import require_action
        if (not isinstance(request, RepairProviderRequest)
                or not isinstance(identifier, str)
                or _PLUGIN_ID.fullmatch(identifier) is None):
            raise WebFailure("PLUGIN_DISABLED",
                             "Diagnosis provider is unavailable or disabled")
        _validate_url(request.url)
        require_action(request.policy, "READ_PUBLIC")
        if request.policy.identity:
            raise WebFailure(
                "IDENTITY_POLICY_DENIED",
                "Public repair diagnosis cannot execute a named identity")
        manifest = self.require_enabled(identifier, request.policy)
        plugin = self._plugins.get(identifier)
        if (not manifest.diagnosis
                or not callable(getattr(plugin, "diagnose", None))):
            raise WebFailure(
                "PLUGIN_DISABLED",
                "Provider does not declare repair diagnosis capability")

        if services is None:
            async def unavailable(*args, **kwargs):
                raise WebFailure(
                    "PROVIDER_UNAVAILABLE",
                    "Diagnosis transport service is unavailable")
            base_services = ProviderServices(
                unavailable, unavailable, unavailable)
        else:
            base_services = services
        active = True
        diagnosis_task = None
        child_tasks = set()

        async def scoped_transport(callback, url, policy, provider=None):
            if not active:
                raise WebFailure(
                    "POLICY_DENIED", "Diagnosis execution scope is closed")
            if (url != request.url or policy is not request.policy
                    or provider is not None and provider != identifier):
                raise WebFailure(
                    "POLICY_DENIED",
                    "Diagnosis transport exceeds its exact request scope")
            invocation_task = asyncio.current_task()
            is_child = (invocation_task is not None
                        and invocation_task is not diagnosis_task)
            if is_child:
                child_tasks.add(invocation_task)
                invocation_task.add_done_callback(consume)
            try:
                if provider is None:
                    return await callback(url, policy)
                return await callback(url, policy, provider)
            finally:
                if is_child:
                    child_tasks.discard(invocation_task)

        async def scoped_http(url, policy):
            return await scoped_transport(
                base_services.http, url, policy)

        async def scoped_browser(url, policy, provider):
            return await scoped_transport(
                base_services.browser, url, policy, provider)

        async def scoped_isolated(url, policy, provider):
            return await scoped_transport(
                base_services.isolated, url, policy, provider)

        async def composition_denied(*args, **kwargs):
            raise WebFailure(
                "POLICY_DENIED",
                "Diagnosis providers cannot compose acquisition providers")

        def manifest_denied(*args, **kwargs):
            raise WebFailure(
                "POLICY_DENIED",
                "Diagnosis providers cannot inspect acquisition providers")

        async def authenticated_denied(*args, **kwargs):
            raise WebFailure(
                "IDENTITY_POLICY_DENIED",
                "Public diagnosis cannot use authenticated acquisition")

        plugin_services = ProviderServices(
            scoped_http, scoped_browser, scoped_isolated,
            composition_denied, manifest_denied, authenticated_denied)

        def consume(completed):
            self._pending_cleanup.discard(completed)
            if not completed.cancelled():
                completed.exception()

        async def stop_task(task):
            task.cancel()
            done, _ = await asyncio.wait(
                {task},
                timeout=request.policy.provider_cleanup_grace_seconds)
            if done:
                consume(task)
            else:
                self._pending_cleanup.add(task)
                task.add_done_callback(consume)

        try:
            diagnosis_task = asyncio.create_task(
                plugin.diagnose(request, plugin_services))
            done, _ = await asyncio.wait(
                {diagnosis_task},
                timeout=request.policy.timeout_seconds
                    + request.policy.provider_deadline_grace_seconds)
            if not done:
                await stop_task(diagnosis_task)
                raise WebFailure(
                    "TIMEOUT", "Diagnosis provider deadline exceeded")
            result = diagnosis_task.result()
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if (diagnosis_task is not None and diagnosis_task.cancelled()
                    and not (current is not None and current.cancelling())):
                raise WebFailure(
                    "PROVIDER_DOWN",
                    "Diagnosis plugin cancelled its execution") from None
            if diagnosis_task is not None and not diagnosis_task.done():
                await stop_task(diagnosis_task)
            raise
        except WebFailure:
            raise
        except Exception:
            raise WebFailure(
                "PROVIDER_DOWN",
                "Diagnosis plugin failed; raw exception data withheld") from None
        finally:
            active = False
            pending_children = {
                task for task in child_tasks if not task.done()}
            for task in pending_children:
                task.cancel()
            if pending_children:
                done, pending = await asyncio.wait(
                    pending_children,
                    timeout=request.policy.provider_cleanup_grace_seconds)
                for task in done:
                    consume(task)
                for task in pending:
                    self._pending_cleanup.add(task)
                    task.add_done_callback(consume)

        current = self.require_enabled(identifier, request.policy)
        if current.version != manifest.version or not current.diagnosis:
            raise WebFailure(
                "PROVIDER_UNAVAILABLE",
                "Diagnosis provider changed during execution")
        return result

    async def acquire(self,identifier,request,services,*,_parent_scope=None,
                      _evidence_preparer=None):
        from .runtime import WebFailure,_validate_url,_status_failure
        from .actions import require_action, required_read_action

        if not isinstance(identifier, str) or _PLUGIN_ID.fullmatch(identifier) is None:
            raise WebFailure("PLUGIN_DISABLED",
                "Provider is unavailable or disabled")
        _validate_url(request.url)
        # A scoped service callback passes authority explicitly. ContextVar is
        # retained for backwards-compatible direct registry composition, but a
        # plugin cannot gain root authority by spawning with a clean context.
        parent_scope = (_parent_scope if _parent_scope is not None
                        else _PROVIDER_EXECUTION.get())
        root_scope = (parent_scope if parent_scope is not None
                      else _ExecutionScope((), [], [0],
                          request.policy.provider_composition_max_depth,
                          request.policy.provider_composition_max_attempts,
                          request.policy.max_bytes, request.policy))
        plugin = self._plugins.get(identifier)
        hint = getattr(plugin, "manifest", None)
        version = (hint.version if isinstance(hint, ProviderManifest)
                   and _bounded_text(hint.version, _MAX_VERSION_LENGTH)
                   and _PLUGIN_VERSION.fullmatch(hint.version) else None)
        node = None
        if parent_scope is not None:
            if parent_scope.state != "active":
                raise WebFailure("POLICY_DENIED",
                    "Provider composition scope is closed")
            invocation_task = asyncio.current_task()
            if (invocation_task is not None
                    and invocation_task is not parent_scope.owner_task):
                parent_scope.child_tasks.add(invocation_task)
                def finish_child(completed, scope=parent_scope):
                    scope.child_tasks.discard(completed)
                    _consume_task_exception(completed)
                invocation_task.add_done_callback(finish_child)
            request = ProviderRequest(request.url,
                _nested_policy(parent_scope, request.policy, identifier),
                request.operation, request.pdf_bytes)
            if parent_scope.counter[0] >= root_scope.max_attempts:
                raise WebFailure("PROVIDER_DOWN",
                    "Nested provider attempt limit exceeded")
            parent_scope.counter[0] += 1
            node = {"provider": identifier, "provider_version": version,
                    "status": "pending", "cost_usd": 0,
                    "paid": bool(getattr(hint, "paid", False)),
                    "started": time.monotonic(), "children": [],
                    "reported_children": None, "owner": parent_scope}
            binding_id = self.binding_id(identifier)
            if binding_id is not None:
                node["provider_binding_id"] = binding_id
            parent_scope.children.append(node)
            if (identifier in parent_scope.ancestry
                    or len(parent_scope.ancestry) >= root_scope.max_depth):
                _finish_ledger_node(node, "failed", cost=0,
                                    failure="POLICY_DENIED")
                raise WebFailure("POLICY_DENIED",
                    "Recursive provider composition is not permitted")

        try:
            require_action(
                request.policy, required_read_action(request.policy.identity))
            manifest = self.require_enabled(identifier, request.policy,
                                            operation=request.operation)
        except WebFailure as error:
            _finish_ledger_node(node, "failed", cost=0,
                                failure=error.code,
                                failure_stage=error.failure_stage)
            raise

        if node is not None:
            node["provider_version"] = manifest.version
            node["paid"] = manifest.paid
            node["cost_usd"] = None if manifest.paid else 0
        local_children = node["children"] if node is not None else root_scope.children
        ancestry = ((parent_scope.ancestry if parent_scope is not None else ())
                    + (identifier,))
        execution_scope = _ExecutionScope(
            ancestry, local_children, root_scope.counter,
            root_scope.max_depth, root_scope.max_attempts,
            root_scope.max_bytes, request.policy)

        if services is None:
            async def unavailable_transport(*args, **kwargs):
                raise WebFailure("PROVIDER_UNAVAILABLE",
                    "Provider transport service is unavailable")
            base_services = ProviderServices(
                unavailable_transport, unavailable_transport,
                unavailable_transport)
        else:
            base_services = services

        async def scoped_acquire(child_identifier, child_request):
            if execution_scope.state != "active":
                raise WebFailure("POLICY_DENIED",
                    "Provider composition scope is closed")
            if manifest.authentication:
                raise WebFailure("IDENTITY_POLICY_DENIED",
                    "Authenticated providers cannot compose child providers")
            return await self.acquire(
                child_identifier, child_request, base_services,
                _parent_scope=execution_scope,
                _evidence_preparer=_evidence_preparer)

        def scoped_manifest(child_identifier, child_policy):
            if execution_scope.state != "active":
                raise WebFailure("POLICY_DENIED",
                    "Provider composition scope is closed")
            if manifest.authentication:
                raise WebFailure("IDENTITY_POLICY_DENIED",
                    "Authenticated providers cannot inspect child providers")
            effective = _nested_policy(
                execution_scope, child_policy, child_identifier)
            return self.require_enabled(child_identifier, effective)

        async def scoped_authenticated(url, policy):
            if execution_scope.state != "active":
                raise WebFailure("POLICY_DENIED",
                    "Provider execution scope is closed")
            if not manifest.authentication:
                raise WebFailure("IDENTITY_POLICY_DENIED",
                    "Public providers cannot use authenticated acquisition")
            callback = base_services.authenticated
            if callback is None:
                raise WebFailure("PROVIDER_UNAVAILABLE",
                    "Authenticated provider service is unavailable")

            # A plugin may create the capability call in a clean Context.  Tie
            # it to this explicit execution scope so receipt finalization can
            # cancel an in-flight or fire-and-forget authenticated read.
            invocation_task = asyncio.current_task()
            if (invocation_task is not None
                    and invocation_task is not execution_scope.owner_task):
                if execution_scope.state != "active":
                    raise WebFailure("POLICY_DENIED",
                        "Provider execution scope is closed")
                execution_scope.child_tasks.add(invocation_task)
                def finish_authenticated(completed, scope=execution_scope):
                    scope.child_tasks.discard(completed)
                    _consume_task_exception(completed)
                invocation_task.add_done_callback(finish_authenticated)
            return await callback(url, policy)

        async def identity_public_denied(*args, **kwargs):
            raise WebFailure("IDENTITY_POLICY_DENIED",
                "Authenticated providers cannot use public transport services")

        staged_failure_evidence = []
        staged_failure_bytes = 0

        def scoped_retain_failure_evidence(raw, content_type):
            nonlocal staged_failure_bytes
            # Staging is an optional diagnostic capability. A closed scope,
            # unavailable sink or malformed/over-budget proposal must never
            # replace the provider's typed acquisition outcome.
            try:
                if (execution_scope.state != "active"
                        or _evidence_preparer is None
                        or manifest.authentication
                        or request.policy.identity
                        or not request.policy.retain_public_failure_evidence
                        or type(raw) is not bytes
                        or not raw
                        or not _bounded_text(
                            content_type, request.policy.max_bytes)):
                    return False
                next_total = staged_failure_bytes + len(raw)
                if next_total > request.policy.max_bytes:
                    return False
                staged_failure_evidence.append((raw, content_type))
                staged_failure_bytes = next_total
                return True
            except Exception:
                return False

        plugin_transports = ((identity_public_denied,) * 3
            if manifest.authentication else
            (base_services.http, base_services.browser,
             base_services.isolated))
        plugin_services = ProviderServices(
            *plugin_transports,
            scoped_acquire, scoped_manifest, scoped_authenticated,
            retain_failure_evidence=scoped_retain_failure_evidence)
        progress = [None]
        progress_token = _PROVIDER_PROGRESS.set(progress)
        execution_token = _PROVIDER_EXECUTION.set(execution_scope)
        try:
            if node is not None:
                node["executed"] = True
            task = asyncio.create_task(
                self._plugins[identifier].acquire(
                    request, plugin_services))
            execution_scope.owner_task = task
        except asyncio.CancelledError:
            await _close_execution_scope(
                execution_scope,
                request.policy.provider_cleanup_grace_seconds)
            _finish_ledger_node(node, "failed",
                cost=None if manifest.paid else 0, failure="UNKNOWN")
            raise
        except Exception:
            await _close_execution_scope(
                execution_scope,
                request.policy.provider_cleanup_grace_seconds)
            _finish_ledger_node(node, "failed",
                cost=None if manifest.paid else 0, failure="PROVIDER_DOWN")
            raise WebFailure("PROVIDER_DOWN",
                "Provider plugin failed; raw exception data withheld",
                **({"cost_usd": None} if manifest.paid else {})) from None
        finally:
            _PROVIDER_EXECUTION.reset(execution_token)
            _PROVIDER_PROGRESS.reset(progress_token)

        def consume(completed):
            self._pending_cleanup.discard(completed)
            if not completed.cancelled():
                completed.exception()

        def abandon():
            task.cancel()
            if task.done():
                consume(task)
            else:
                self._pending_cleanup.add(task)
                task.add_done_callback(consume)

        def safe_failure(error):
            raw_tree = error.__dict__.get("provider_attempts")
            supplied_tree = None
            malformed_tree = False
            if raw_tree is not None:
                try:
                    supplied_tree = _validated_provider_attempt_tree(
                        raw_tree, max_depth=root_scope.max_depth,
                        max_nodes=root_scope.max_attempts,
                        max_bytes=root_scope.max_bytes)
                except (ValueError, TypeError, OverflowError, RecursionError):
                    malformed_tree = True

            reported_present = "cost_usd" in error.__dict__
            reported_cost = None
            valid_reported_cost = False
            if reported_present:
                try:
                    reported_cost = _reported_cost(error.__dict__["cost_usd"], root_scope.max_bytes)
                    valid_reported_cost = True
                except (ValueError, TypeError, OverflowError):
                    pass

            invalid_reported_cost = reported_present and not valid_reported_cost
            code = error.__dict__.get("code")
            valid_code = (isinstance(code, str)
                          and _FAILURE_CODE.fullmatch(code) is not None)
            if malformed_tree or invalid_reported_cost or not valid_code:
                code = "PROVIDER_DOWN"
                message = "Invalid provider failure envelope"
            else:
                message = error.__dict__.get("message")
                if not isinstance(message, str) or len(message) > 4096:
                    message = "Provider acquisition failed"

            http_status = error.__dict__.get("http_status")
            if (http_status is not None
                    and (type(http_status) is not int
                         or not 100 <= http_status <= 599)):
                http_status = None
            response_url = error.__dict__.get("response_url")
            if response_url is not None:
                try:
                    _validate_url(response_url)
                except WebFailure:
                    response_url = None
            failure_stage = error.__dict__.get("failure_stage")
            if failure_stage is not None:
                from .provider_worker import FAILURE_STAGES
                if failure_stage not in FAILURE_STAGES:
                    failure_stage = None

            authoritative_tree = None
            if local_children:
                child_cost = _ledger_cost(local_children)
                try:
                    authoritative_tree = _ledger_tree(
                        local_children, root_scope)
                except (ValueError, TypeError, OverflowError,
                        RecursionError):
                    code = "PROVIDER_DOWN"
                    message = (
                        "Nested provider attempt envelope exceeds policy")
                try:
                    aggregate_cost = _authoritative_total(
                        reported_cost, valid_reported_cost, child_cost,
                        paid=manifest.paid)
                except ValueError:
                    aggregate_cost = None if manifest.paid else child_cost
                    code = "PROVIDER_DOWN"
                    message = "Provider total is below known child cost"
                expose_cost = True
            elif supplied_tree is not None:
                authoritative_tree = supplied_tree
                child_cost = _attempt_tree_cost(supplied_tree)
                try:
                    aggregate_cost = _authoritative_total(
                        reported_cost, valid_reported_cost, child_cost,
                        paid=manifest.paid)
                except ValueError:
                    aggregate_cost = None if manifest.paid else child_cost
                    code = "PROVIDER_DOWN"
                    message = "Provider total is below known child cost"
                expose_cost = True
                if node is not None:
                    node["reported_children"] = supplied_tree
            elif valid_reported_cost:
                aggregate_cost = reported_cost
                expose_cost = True
            elif manifest.paid:
                aggregate_cost = None
                expose_cost = True
            else:
                aggregate_cost = 0
                expose_cost = False

            kwargs = {"response_url": response_url,
                      "failure_stage": failure_stage}
            if expose_cost:
                kwargs["cost_usd"] = aggregate_cost
            safe = WebFailure(code, message, http_status, **kwargs)
            from .runtime import WALL_VENDORS
            if error.__dict__.get("wall_vendor") in WALL_VENDORS:
                safe.wall_vendor = error.__dict__["wall_vendor"]
            evidence = error.__dict__.get("_public_failure_evidence")
            try:
                safe_evidence = (_evidence_references(
                    evidence, max_bytes=root_scope.max_bytes)
                    if evidence else [])
            except (ValueError, TypeError, OverflowError, RecursionError):
                safe_evidence = []
            if staged_failure_evidence and _evidence_preparer is not None:
                for raw, content_type in staged_failure_evidence:
                    try:
                        prepared = _evidence_preparer(raw, content_type)
                        if (type(prepared) is not tuple
                                or len(prepared) != 2
                                or not callable(prepared[1])):
                            raise ValueError()
                        retained, commit = prepared
                        candidate = _evidence_references(
                            [retained], max_bytes=root_scope.max_bytes)
                        combined = _evidence_references(
                            [*safe_evidence, *candidate],
                            max_bytes=root_scope.max_bytes)
                        # Descriptor and complete receipt linkage are validated
                        # before the content-addressed atomic writer runs. A
                        # rejected descriptor therefore creates no orphan and
                        # never requires deleting a preexisting artifact.
                        commit()
                    except Exception:
                        # Evidence is optional diagnostic material. Persistence
                        # failure must not replace the typed acquisition failure.
                        continue
                    safe_evidence = combined
            safe._public_failure_evidence = safe_evidence
            if authoritative_tree is not None:
                safe.provider_attempts = authoritative_tree

            node_cost = aggregate_cost if expose_cost else (
                None if manifest.paid else 0)
            _finish_ledger_node(node, "failed", cost=node_cost,
                                failure=safe.code,
                                failure_stage=safe.failure_stage,
                                evidence=safe_evidence)
            return safe

        try:
            done, _ = await asyncio.wait(
                {task}, timeout=request.policy.timeout_seconds
                + request.policy.provider_deadline_grace_seconds)
            if not done:
                last_stage = progress[0]
                task.cancel()
                done, _ = await asyncio.wait(
                    {task}, timeout=request.policy.provider_cleanup_grace_seconds)
                if not done:
                    self._pending_cleanup.add(task)
                    task.add_done_callback(consume)
                else:
                    consume(task)
                timeout_subject = ("Authenticated provider" if
                    manifest.authentication else "Public provider")
                error = WebFailure("TIMEOUT",
                    timeout_subject + " acquisition deadline exceeded"
                    + (" at " + last_stage if last_stage else ""),
                    failure_stage=last_stage)
                raise error
            result = task.result()
            await _close_execution_scope(
                execution_scope,
                request.policy.provider_cleanup_grace_seconds)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            # A provider task may cancel itself. That is an untrusted provider
            # failure, not cancellation of the caller running this registry.
            if task.cancelled() and not (
                    current is not None and current.cancelling()):
                await _close_execution_scope(
                    execution_scope,
                    request.policy.provider_cleanup_grace_seconds)
                raise safe_failure(WebFailure(
                    "PROVIDER_DOWN",
                    "Provider plugin cancelled its acquisition")) from None
            task.cancel()
            await _close_execution_scope(
                execution_scope,
                request.policy.provider_cleanup_grace_seconds)
            cancellation_cost = (None if manifest.paid else
                (_ledger_cost(local_children) if local_children else 0))
            _finish_ledger_node(node, "failed", cost=cancellation_cost,
                                failure="TIMEOUT",
                                failure_stage=progress[0])
            abandon()
            raise
        except WebFailure as error:
            await _close_execution_scope(
                execution_scope,
                request.policy.provider_cleanup_grace_seconds)
            raise safe_failure(error) from None
        except Exception:
            await _close_execution_scope(
                execution_scope,
                request.policy.provider_cleanup_grace_seconds)
            raise safe_failure(WebFailure("PROVIDER_DOWN",
                "Provider plugin failed; raw exception data withheld")) from None

        supplied_tree = None
        result_cost = None
        cost_present = False
        safe_evidence = None
        try:
            if (type(result) is dict
                    and ({"visible_snapshot",
                          "identity_private_image_sources"} & set(result))):
                raise WebFailure("PROVIDER_DOWN",
                    "Provider returned reserved identity capture metadata")
            if type(result) is dict and "cost_usd" in result:
                cost_present = True
                try:
                    result_cost = _reported_cost(result["cost_usd"], root_scope.max_bytes)
                except (ValueError, TypeError, OverflowError):
                    raise WebFailure("PROVIDER_DOWN",
                        "Invalid provider cost envelope") from None
            if type(result) is dict and "provider_attempts" in result:
                try:
                    supplied_tree = _validated_provider_attempt_tree(
                        result["provider_attempts"],
                        max_depth=root_scope.max_depth,
                        max_nodes=root_scope.max_attempts,
                        max_bytes=root_scope.max_bytes)
                except (ValueError, TypeError, OverflowError, RecursionError):
                    raise WebFailure("PROVIDER_DOWN",
                        "Invalid composite provider attempt envelope") from None
            if type(result) is dict and result.get("acquisition_evidence"):
                try:
                    safe_evidence = _evidence_references(
                        result["acquisition_evidence"],
                        max_bytes=root_scope.max_bytes)
                except (ValueError, TypeError, OverflowError, RecursionError):
                    raise WebFailure("PROVIDER_DOWN",
                        "Invalid provider evidence envelope") from None
            if (type(result) is not dict
                    or not all(isinstance(result.get(key),str)
                               for key in ("url","content","content_type"))):
                raise WebFailure("PROVIDER_DOWN",
                    "Provider returned an invalid acquisition envelope")
            _validate_url(result["url"])
            if not _bounded_text(result["content_type"],
                                 request.policy.max_bytes):
                raise WebFailure("PROVIDER_DOWN",
                    "Provider returned an invalid content type")
            if len(result["content"].encode()) > request.policy.max_bytes:
                raise WebFailure("LIMIT_EXCEEDED",
                    "Provider content exceeds policy byte budget")
            status=result.get("http_status")
            if type(status) is int:
                if failure := _status_failure(status):
                    raise WebFailure(failure,"Provider response failed",status,
                                     response_url=result["url"])
            elif status is not None:
                raise WebFailure("PROVIDER_DOWN",
                    "Invalid provider HTTP status")
            raw=result.get("raw",result["content"].encode())
            headers = result.get("headers", {})
            if (not isinstance(raw, bytes) or type(headers) is not dict
                    or any(not isinstance(key, str)
                           or not isinstance(value, str)
                           for key, value in headers.items())):
                raise WebFailure("PROVIDER_DOWN",
                    "Invalid provider byte/header envelope")
            try:
                headers = _bounded_json_object(
                    headers, request.policy.max_bytes)
            except (ValueError, TypeError, OverflowError, RecursionError):
                raise WebFailure("PROVIDER_DOWN",
                    "Invalid provider byte/header envelope") from None
            if len(raw) > request.policy.max_bytes:
                raise WebFailure("LIMIT_EXCEEDED",
                    "Provider raw response exceeds policy byte budget")
            screenshot = result.get("screenshot")
            if screenshot is not None:
                try:
                    screenshot = _evidence_references(
                        [screenshot], max_bytes=request.policy.max_bytes)[0]
                except (ValueError, TypeError, OverflowError,
                        RecursionError):
                    raise WebFailure("PROVIDER_DOWN",
                        "Invalid provider screenshot envelope") from None
            navigation = result.get("navigation")
            if navigation is not None:
                try:
                    navigation = _bounded_json_object(
                        navigation, request.policy.max_bytes)
                except (ValueError, TypeError, OverflowError,
                        RecursionError):
                    raise WebFailure("PROVIDER_DOWN",
                        "Invalid provider navigation envelope") from None
            navigation_data = result.get("navigation_data")
            if navigation_data is not None:
                try:
                    if (type(navigation_data) is not dict
                            or not isinstance(navigation_data.get("url"), str)
                            or not isinstance(navigation_data.get("content"), str)
                            or type(navigation_data.get("http_status")) is not int
                            or not 100 <= navigation_data["http_status"] <= 599
                            or ("method" in navigation_data
                                and not _bounded_text(
                                    navigation_data["method"], 32))
                            or len(navigation_data["content"].encode())
                                > request.policy.max_bytes):
                        raise ValueError()
                    _validate_url(navigation_data["url"])
                    navigation_data = {
                        key: navigation_data[key]
                        for key in ("url", "http_status", "method",
                                    "content")
                        if key in navigation_data}
                except (WebFailure, ValueError, TypeError, OverflowError,
                        UnicodeError):
                    raise WebFailure("PROVIDER_DOWN",
                        "Invalid provider navigation data envelope") from None
            from .runtime import _SAFE_HEADERS, _validated_content_readiness
            content_readiness = _validated_content_readiness(
                result.get("content_readiness"), request.policy)
            allowed_result_fields = {
                "url", "content", "content_type", "raw", "http_status",
                "headers", "screenshot", "navigation", "navigation_data",
                "cost_usd", "acquisition_evidence", "provider_attempts"}
            pdf = result.get("pdf")
            if pdf is not None:
                if (type(pdf) is not dict
                        or set(pdf) != {"pages", "pages_total", "truncated", "note"} - (
                            set() if pdf.get("truncated") else {"note"})
                        or type(pdf["pages"]) is not int or pdf["pages"] < 0
                        or (pdf["pages_total"] is not None
                            and (type(pdf["pages_total"]) is not int or pdf["pages_total"] < 0))
                        or type(pdf["truncated"]) is not bool
                        or ("note" in pdf and not _bounded_text(pdf["note"], 200))):
                    raise WebFailure("PROVIDER_DOWN", "Invalid provider PDF envelope")
            filtered = {key: result[key] for key in allowed_result_fields
                        if key in result}
            if pdf is not None:
                filtered["pdf"] = dict(pdf)
            filtered.update(raw=raw,http_status=status,
                headers={key:value for key,value in headers.items()
                         if key in _SAFE_HEADERS})
            if screenshot is not None:
                filtered["screenshot"] = screenshot
            if navigation is not None:
                filtered["navigation"] = navigation
            if navigation_data is not None:
                filtered["navigation_data"] = navigation_data
            if safe_evidence is not None:
                filtered["acquisition_evidence"] = safe_evidence
            if content_readiness is not None:
                filtered["content_readiness"] = content_readiness
            archived = result.get("archived")
            if getattr(getattr(plugin, "manifest", None), "archive", False):
                # An archive must say when its copy was taken; nothing else may claim it.
                if (type(archived) is not dict or set(archived) != {"source", "archived_at", "snapshot_url"}
                        or not all(type(value) is str and len(value) <= 2048 for value in archived.values())
                        or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", archived["archived_at"])
                        or not archived["snapshot_url"].startswith("https://")):
                    raise WebFailure("PROVIDER_DOWN", "Archive provider did not date its copy")
                filtered["archived"] = dict(archived)
            performed = result.get("interactions")
            if performed is not None:
                # What the safe actions did: short, bounded, plain.
                if (type(performed) is not list or len(performed) > 20
                        or any(type(item) is not dict or set(item) - {"action", "label", "ok", "grew"}
                               or not isinstance(item.get("action"), str)
                               or len(str(item.get("label") or "")) > 120 for item in performed)):
                    raise WebFailure("PROVIDER_DOWN", "Invalid interaction record")
                filtered["interactions"] = [dict(item) for item in performed]
            captured = result.get("captured_json")
            if captured is not None and request.policy.capture_json_responses:
                # Raw page data is passed through only when the caller opted in,
                # within the policy's item and size bounds.
                from .runtime import _validate_url as _check_url
                try:
                    items = captured["items"]
                    if (type(captured) is not dict or type(items) is not list
                            or len(items) > request.policy.capture_json_max_items):
                        raise ValueError()
                    for item in items:
                        _check_url(item["url"])
                        if len(json.dumps(item["data"]).encode()) > request.policy.capture_json_max_bytes:
                            raise ValueError()
                    filtered["captured_json"] = {"items": items,
                                                 "skipped": int(captured.get("skipped", 0))}
                except (KeyError, TypeError, ValueError, WebFailure):
                    raise WebFailure("PROVIDER_DOWN",
                        "Invalid captured page data envelope") from None

            authoritative_tree = None
            if local_children:
                try:
                    authoritative_tree = _ledger_tree(
                        local_children, root_scope)
                except (ValueError, TypeError, OverflowError,
                        RecursionError):
                    raise WebFailure("PROVIDER_DOWN",
                        "Nested provider attempt envelope exceeds policy") from None
                child_cost = _ledger_cost(local_children)
                try:
                    result_cost = _authoritative_total(
                        result_cost, cost_present, child_cost,
                        paid=manifest.paid)
                except ValueError:
                    raise WebFailure("PROVIDER_DOWN",
                        "Provider total is below known child cost") from None
                cost_present = True
            elif supplied_tree is not None:
                authoritative_tree = supplied_tree
                child_cost = _attempt_tree_cost(supplied_tree)
                try:
                    result_cost = _authoritative_total(
                        result_cost, cost_present, child_cost,
                        paid=manifest.paid)
                except ValueError:
                    raise WebFailure("PROVIDER_DOWN",
                        "Provider total is below known child cost") from None
                cost_present = True
                if node is not None:
                    node["reported_children"] = supplied_tree
            if authoritative_tree is not None:
                filtered["provider_attempts"] = authoritative_tree
            else:
                filtered.pop("provider_attempts", None)
            if cost_present:
                filtered["cost_usd"] = result_cost

            node_cost = (result_cost if cost_present
                         else (None if manifest.paid else 0))
            _finish_ledger_node(node, "observed", cost=node_cost,
                                evidence=safe_evidence)
            return filtered
        except WebFailure as error:
            if supplied_tree is not None:
                error.provider_attempts = supplied_tree
            if cost_present:
                error.cost_usd = result_cost
            raise safe_failure(error) from None
        except Exception:
            raise safe_failure(WebFailure(
                "PROVIDER_DOWN",
                "Provider returned an invalid acquisition envelope")) from None


class LegacyProvider:
    def __init__(self,manifest,kind):self.manifest=manifest;self.kind=kind
    def available(self, configured):
        if self.kind == "identity": return True
        if self.kind == "isolated":
            from .experimental import installed
            return installed(self.manifest.id)
        if self.manifest.id == "steel": return "steel" in configured
        return True
    async def acquire(self,request,services):
        if self.kind == "http":return await services.http(request.url,request.policy)
        if self.kind == "isolated":return await services.isolated(request.url,request.policy,self.manifest.id)
        if self.kind == "identity":
            from .runtime import WebFailure
            if services.authenticated is None:
                raise WebFailure("PROVIDER_UNAVAILABLE",
                    "Authenticated provider service is unavailable")
            return await services.authenticated(request.url, request.policy)
        return await services.browser(request.url,request.policy,self.manifest.id)


DEFAULT_PROVIDERS=ProviderRegistry()
DEFAULT_PROVIDERS.register(LegacyProvider(ProviderManifest("http","legacy"),"http"))
DEFAULT_PROVIDERS.register(LegacyProvider(ProviderManifest("local","legacy",rendering=True,requires_local_browser=True),"browser"))
DEFAULT_PROVIDERS.register(LegacyProvider(ProviderManifest("steel","legacy",rendering=True),"browser"))
DEFAULT_PROVIDERS.register(LegacyProvider(ProviderManifest("local_cdp","legacy",rendering=True,requires_local_browser=True,authentication=True),"identity"))
DEFAULT_PROVIDERS.register(LegacyProvider(ProviderManifest("camoufox","legacy",rendering=True,requires_local_browser=True,navigation=True),"isolated"))
DEFAULT_PROVIDERS.register(LegacyProvider(ProviderManifest("scrapling","legacy",rendering=True,requires_local_browser=True),"isolated"))
DEFAULT_PROVIDERS.register(LegacyProvider(ProviderManifest("scrapling_http","legacy"),"isolated"))
DEFAULT_PROVIDERS.register(LegacyProvider(ProviderManifest("patchright","1",rendering=True,requires_local_browser=True),"isolated"))
DEFAULT_PROVIDERS.register(LegacyProvider(ProviderManifest("nodriver","1",rendering=True,requires_local_browser=True),"isolated"))

from .scrapling_ready import ScraplingReadyProvider
DEFAULT_PROVIDERS.register(ScraplingReadyProvider())

from .public_entry import CamoufoxEntryProvider
DEFAULT_PROVIDERS.register(CamoufoxEntryProvider())

from .browser_use_provider import BrowserUseProvider
DEFAULT_PROVIDERS.register(BrowserUseProvider())

from .browser_use_action_provider import BrowserUseActionProvider
DEFAULT_PROVIDERS.register(BrowserUseActionProvider())

from .crawl4ai_provider import Crawl4AIProvider
DEFAULT_PROVIDERS.register(Crawl4AIProvider())

from .handoff import HandoffProvider  # noqa: E402
DEFAULT_PROVIDERS.register(HandoffProvider())
from .real_browser import RealBrowserProvider  # noqa: E402
DEFAULT_PROVIDERS.register(RealBrowserProvider())
from .hosted_providers import register as _register_hosted  # noqa: E402
_register_hosted(DEFAULT_PROVIDERS)
