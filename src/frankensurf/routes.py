"""Bound public route recipes; no learned policy grants.

Recipes come from read-only benchmark-earned package seeds or private operator
configuration. They are never inferred during an operation. This registry is
deliberately separate from route memory.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, fields, replace
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from types import MappingProxyType
from urllib.parse import parse_qsl, urlparse


class RouteRecipeError(ValueError):
    code = "ROUTE_CONFIG_INVALID"


def _invalid():
    raise RouteRecipeError("Invalid public route recipe configuration")


# These are authority/routing values, not operational defaults a recipe can grant.
# New identity or write recipe types need their own authority contract.
_AUTHORITY_FIELDS = frozenset({"identity", "provider", "provider_candidates",
    "action_classes",
    "allow_local_browser", "allow_paid_fallbacks", "freshness", "terminal_failures",
    "context_stop_failures", "public_entry_continue_failures", "use_route_memory",
    "route_memory_ttl_seconds", "route_memory_min_samples",
    "scrapling_solve_cloudflare",
    "browser_agent_allowed_actions", "browser_agent_allowed_origins",
    "browser_agent_entry_url", "browser_agent_task", "browser_agent_use_vision"})
_OPERATOR_BASIS = "explicit operator configuration"
_BUNDLED_BASIS = "bundled benchmark-verified route seed"
_COMPATIBILITY_BASIS = "bundled provisional compatibility seed"
# Downloaded by `frankensurf share sync` (share.py): provisional, like compatibility seeds.
_COMMUNITY_BASIS = "community provisional seed"
_SEED_BASES = frozenset({_COMPATIBILITY_BASIS, _COMMUNITY_BASIS})


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", value):
        _invalid()
    return value


def _version(value):
    if not isinstance(value, str) or not value or not value.isprintable():
        _invalid()
    return value


def _origin(value):
    try:
        parsed = urlparse(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or
                parsed.password or parsed.path or parsed.params or parsed.query or parsed.fragment):
            _invalid()
        return value
    except (ValueError, TypeError):
        _invalid()


def _defaults(values, origins):
    from .runtime import WebPolicy
    if not isinstance(values, dict) or set(values) & _AUTHORITY_FIELDS:
        _invalid()
    known = {field.name for field in fields(WebPolicy)}
    if set(values) - known:
        _invalid()
    tuple_fields = {field.name for field in fields(WebPolicy) if isinstance(field.default, tuple)}
    converted = {key: tuple(value) if key in tuple_fields and isinstance(value, list) else value
                 for key, value in values.items()}
    try:
        json.dumps(converted, allow_nan=False)
        policy = WebPolicy(**converted)
    except (ValueError, TypeError):
        _invalid()
    if policy.navigation_page != 1:
        _invalid()
    # Store public configuration, not arbitrary query values, selector queries or
    # copied operation URLs. Entry navigation must stay inside the declared origin.
    for key in ("public_entry_url",):
        value = converted.get(key)
        if value is not None:
            parsed = urlparse(value)
            if (parsed.query or parsed.fragment or parsed.username or parsed.password or
                    parsed.scheme + "://" + parsed.netloc not in origins):
                _invalid()
    return MappingProxyType(copy.deepcopy(converted))


def _provenance(value):
    if not isinstance(value, dict) or set(value) - {"source_commit", "manifest_sha256", "report_sha256", "case_ids"}:
        _invalid()
    checked = {}
    for key, width in (("source_commit", 40), ("manifest_sha256", 64), ("report_sha256", 64)):
        if key in value:
            item = value[key]
            if not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{" + str(width) + "}", item):
                _invalid()
            checked[key] = item
    if "case_ids" in value:
        ids = value["case_ids"]
        if not isinstance(ids, (list, tuple)):
            _invalid()
        checked["case_ids"] = tuple(_identifier(item) for item in ids)
    return MappingProxyType(checked)


@dataclass(frozen=True)
class PublicRouteRecipe:
    id: str
    version: str
    origin: str
    path_pattern: str
    operation: str
    provider: str
    provider_version: str
    adapter: str | None = None
    adapter_version: str | None = None
    adapter_scope: str = "exact"
    policy_defaults: object = None
    provenance: object = None
    enabled: bool = True
    query_keys: tuple[str, ...] | None = None
    representation: str = "single_page"
    continuation_adapter: str | None = None
    continuation_adapter_version: str | None = None
    acquisition_adapter: str | None = None
    acquisition_adapter_version: str | None = None
    basis: str = _OPERATOR_BASIS

    def __post_init__(self):
        for value in (self.id, self.provider):
            _identifier(value)
        for value in (self.version, self.provider_version):
            _version(value)
        _origin(self.origin)
        if not isinstance(self.path_pattern, str):
            _invalid()
        try:
            re.compile(self.path_pattern)
        except re.error:
            _invalid()
        if (self.operation not in {"read", "extract", "paginate"} or type(self.enabled) is not bool
                or self.basis not in {_OPERATOR_BASIS, _BUNDLED_BASIS,
                                      _COMPATIBILITY_BASIS, _COMMUNITY_BASIS}
                or self.adapter_scope not in {"exact", "any"}
                or (self.adapter_scope == "any"
                    and self.operation != "extract")):
            _invalid()
        sequence = self.representation == "native_sequence"
        if self.representation not in {"single_page", "native_sequence"} or sequence != (self.operation == "paginate"):
            _invalid()
        if sequence:
            for value in (self.continuation_adapter, self.acquisition_adapter):
                _identifier(value)
            for value in (self.continuation_adapter_version, self.acquisition_adapter_version):
                _version(value)
        elif any(value is not None for value in (self.continuation_adapter, self.continuation_adapter_version,
                                                self.acquisition_adapter, self.acquisition_adapter_version)):
            _invalid()
        if self.operation == "read":
            if (self.adapter is not None or self.adapter_version is not None
                    or self.adapter_scope != "exact"):
                _invalid()
        elif self.operation == "extract" and self.adapter_scope == "any":
            if self.adapter is not None or self.adapter_version is not None:
                _invalid()
        else:
            _identifier(self.adapter)
            _version(self.adapter_version)
        if self.query_keys is not None:
            if not isinstance(self.query_keys, (list, tuple)):
                _invalid()
            keys = tuple(_identifier(key) for key in self.query_keys)
            if len(set(keys)) != len(keys):
                _invalid()
            object.__setattr__(self, "query_keys", keys)
        if self.policy_defaults is not None and not isinstance(self.policy_defaults, (dict, MappingProxyType)):
            _invalid()
        if self.provenance is not None and not isinstance(self.provenance, (dict, MappingProxyType)):
            _invalid()
        object.__setattr__(self, "policy_defaults", _defaults(dict(self.policy_defaults or {}), (self.origin,)))
        object.__setattr__(self, "provenance", _provenance(dict(self.provenance or {})))

    def record(self):
        return {"id": self.id, "version": self.version, "origin": self.origin,
                "path_pattern": self.path_pattern, "operation": self.operation,
                "provider": self.provider, "provider_version": self.provider_version,
                "adapter": self.adapter, "adapter_version": self.adapter_version,
                "adapter_scope": self.adapter_scope,
                "policy_defaults": dict(self.policy_defaults), "provenance": dict(self.provenance),
                "enabled": self.enabled, "query_keys": self.query_keys,
                "representation": self.representation,
                "continuation_adapter": self.continuation_adapter,
                "continuation_adapter_version": self.continuation_adapter_version,
                "acquisition_adapter": self.acquisition_adapter,
                "acquisition_adapter_version": self.acquisition_adapter_version}

    @property
    def fingerprint(self):
        raw = json.dumps({**self.record(), "basis": self.basis}, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode()
        return hashlib.sha256(raw).hexdigest()

    def metadata(self):
        return {"id": self.id, "version": self.version, "sha256": self.fingerprint,
                "provider": self.provider, "provider_version": self.provider_version,
                "adapter": self.adapter, "adapter_version": self.adapter_version,
                "adapter_scope": self.adapter_scope,
                "enabled": self.enabled, "policy_fields": sorted(self.policy_defaults),
                "provenance": dict(self.provenance), "basis": self.basis,
                "reliability": None, "representation": self.representation,
                "operation": self.operation, "continuation_adapter": self.continuation_adapter,
                "continuation_adapter_version": self.continuation_adapter_version,
                "acquisition_adapter": self.acquisition_adapter,
                "acquisition_adapter_version": self.acquisition_adapter_version}

    def matches(self, url, operation, adapter):
        try:
            parsed = urlparse(url)
            pairs = parse_qsl(parsed.query, keep_blank_values=True)
            keys = [key for key, _ in pairs]
            return (parsed.scheme + "://" + parsed.netloc == self.origin
                    and not parsed.username and not parsed.password and not parsed.fragment
                    and re.fullmatch(self.path_pattern, parsed.path) is not None
                    and len(keys) == len(set(keys))
                    and (self.query_keys is None or set(keys) <= set(self.query_keys))
                    and operation == self.operation
                    and (self.adapter_scope == "any" or adapter == self.adapter))
        except (ValueError, TypeError):
            return False


@dataclass(frozen=True)
class AcquisitionPlan:
    provider: str
    policy: object
    adapter: str | None
    recipe: PublicRouteRecipe
    acquisition_adapter: str | None = None


@dataclass(frozen=True)
class RecipePlanning:
    plans: tuple[AcquisitionPlan, ...] = ()
    skipped: tuple[dict, ...] = ()
    seeds: tuple[AcquisitionPlan, ...] = ()


def request_policy(policy=None, overrides=None):
    """Preserve omission; a legacy whole WebPolicy protects every field."""
    from .runtime import WebPolicy
    if policy is not None and overrides is not None:
        raise ValueError("Use either WebPolicy or policy_overrides")
    if policy is not None:
        if not isinstance(policy, WebPolicy):
            raise ValueError("Expected WebPolicy")
        return policy, frozenset(field.name for field in fields(WebPolicy))
    if overrides is not None and not isinstance(overrides, dict):
        raise ValueError("policy_overrides must be an object")
    supplied = dict(overrides or {})
    tuple_fields = {field.name for field in fields(WebPolicy) if isinstance(field.default, tuple)} | {"provider_candidates"}
    values = {key: tuple(value) if key in tuple_fields and isinstance(value, list) else value
              for key, value in supplied.items()}
    return WebPolicy(**values), frozenset(supplied)


class RouteRecipeRegistry:
    """Read-only package seeds plus private operator overrides.

    Operations only read the effective view. A same-ID operator record replaces
    its bundled seed, including a disabled override.
    """
    # Names this store in errors; the site module store shares this machinery.
    label = "Public route"

    def __init__(self, path):
        self.path = Path(path).expanduser()

    @staticmethod
    def _decode(data, basis):
        if (not isinstance(data, dict) or set(data) != {"schema", "recipes"}
                or data["schema"] != "frankensurf.public-route-recipes/v1"
                or not isinstance(data["recipes"], list)):
            _invalid()
        recipes = [replace(PublicRouteRecipe(**row), basis=basis)
                   for row in data["recipes"] if isinstance(row, dict)]
        if len(recipes) != len(data["recipes"]) or len({recipe.id for recipe in recipes}) != len(recipes):
            _invalid()
        return recipes

    @staticmethod
    def _bundled_path():
        return Path(__file__).with_name("bundled_routes.json")

    @staticmethod
    def _compatibility_path():
        return Path(__file__).with_name("bundled_route_seeds.json")

    def _load_bundled(self):
        try:
            with self._bundled_path().open("r", encoding="utf-8") as stream:
                return self._decode(json.load(stream), _BUNDLED_BASIS)
        except RouteRecipeError:
            raise
        except (OSError, ValueError, TypeError):
            raise RouteRecipeError("Bundled public route configuration is invalid") from None

    def _load_compatibility(self):
        try:
            with self._compatibility_path().open("r", encoding="utf-8") as stream:
                recipes = self._decode(
                    json.load(stream), _COMPATIBILITY_BASIS)
            # Compatibility recipes are cold-order hints for the generic
            # planner. Native sequences bypass that planner and therefore
            # cannot honestly be represented as provisional evidence.
            if any(recipe.representation != "single_page"
                   for recipe in recipes):
                raise RouteRecipeError(
                    "Bundled compatibility route configuration is invalid")
            return recipes
        except RouteRecipeError:
            raise
        except (OSError, ValueError, TypeError):
            raise RouteRecipeError("Bundled compatibility route configuration is invalid") from None

    def _load_operator(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return []
        except OSError:
            raise RouteRecipeError(f"{self.label} configuration is unavailable") from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
                raise RouteRecipeError(f"{self.label} configuration must be a private owned file")
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                fd = None
                data = json.load(stream)
            return self._decode(data, _OPERATOR_BASIS)
        except RouteRecipeError:
            raise
        except (OSError, ValueError, TypeError):
            raise RouteRecipeError(f"Invalid {self.label.lower()} configuration") from None
        finally:
            if fd is not None:
                os.close(fd)

    def _load_community(self):
        """Community seeds saved by `frankensurf share sync`, if any. Unreadable or invalid
        community data is ignored rather than breaking reads; `share sync` checks it on save."""
        path = self.path.with_name("community_routes.json")
        try:
            with path.open("r", encoding="utf-8") as stream:
                recipes = self._decode(json.load(stream), _COMMUNITY_BASIS)
        except (OSError, ValueError, TypeError):
            return []
        return [recipe for recipe in recipes
                if recipe.id.startswith("community_") and recipe.operation == "read"
                and recipe.representation == "single_page"]

    def _load(self):
        operator = self._load_operator()
        overridden = {recipe.id for recipe in operator}
        packaged = self._load_bundled() + self._load_compatibility()
        # A site a packaged or operator recipe covers keeps that recipe: community seeds only
        # fill sites nothing else knows.
        covered = {(recipe.origin, recipe.operation) for recipe in operator + packaged}
        community = [recipe for recipe in self._load_community()
                     if (recipe.origin, recipe.operation) not in covered]
        return operator + [recipe for recipe in packaged + community
                           if recipe.id not in overridden]

    @contextmanager
    def _mutation(self):
        import fcntl
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.path.parent.is_symlink():
                raise RouteRecipeError(f"{self.label} directory must be owned local storage")
            lock_path = self.path.with_name(self.path.name + ".lock")
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
                os.close(fd)
                raise RouteRecipeError(f"{self.label} lock must be a private owned file")
            with os.fdopen(fd, "a") as stream:
                fcntl.flock(stream, fcntl.LOCK_EX)
                yield
        except OSError:
            raise RouteRecipeError(f"{self.label} configuration could not be saved") from None

    def _save(self, recipes):
        self._save_document({"schema": "frankensurf.public-route-recipes/v1",
                             "recipes": [recipe.record() for recipe in recipes]})

    def _save_document(self, document, prefix=".route-recipes-"):
        """Write the store atomically as a private file (mkstemp creates it 0600)."""
        destination = None
        try:
            fd, destination = tempfile.mkstemp(prefix=prefix, dir=self.path.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(document, stream, sort_keys=True, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(destination, self.path)
            destination = None
        except OSError:
            raise RouteRecipeError(f"{self.label} configuration could not be saved") from None
        finally:
            if destination is not None:
                Path(destination).unlink(missing_ok=True)

    def register(self, recipe):
        if not isinstance(recipe, PublicRouteRecipe):
            raise RouteRecipeError("Register a validated PublicRouteRecipe")
        recipe = replace(recipe, basis=_OPERATOR_BASIS)
        with self._mutation():
            recipes = self._load_operator()
            recipes = [item for item in recipes if item.id != recipe.id] + [recipe]
            self._save(recipes)
        return recipe.metadata()

    def enable(self, identifier, enabled=True):
        _identifier(identifier)
        if type(enabled) is not bool:
            _invalid()
        with self._mutation():
            recipes = self._load_operator()
            if identifier not in {recipe.id for recipe in recipes}:
                packaged = self._load_bundled() + self._load_compatibility()
                seed = next((recipe for recipe in packaged
                             if recipe.id == identifier), None)
                if seed is not None:
                    recipes.append(replace(seed, basis=_OPERATOR_BASIS))
            if identifier not in {recipe.id for recipe in recipes}:
                raise RouteRecipeError("Public route recipe is not registered")
            self._save([replace(recipe, enabled=enabled) if recipe.id == identifier else recipe for recipe in recipes])

    def inspect(self):
        return [recipe.metadata() for recipe in self._load()]

    def plan(self, url, operation, adapter, adapter_version, policy, protected_fields,
             *, providers=None, configured=()):
        if providers is None:
            from .providers import DEFAULT_PROVIDERS
            providers = DEFAULT_PROVIDERS
        from .runtime import WebFailure
        if policy.identity or policy.provider is not None or policy.provider_candidates is not None:
            return RecipePlanning()
        plans, skipped, seeds = [], [], []
        for recipe in self._load():
            if not recipe.matches(url, operation, adapter):
                continue
            reason = None
            if not recipe.enabled:
                reason = "PLUGIN_DISABLED"
            elif (recipe.adapter_scope == "exact"
                  and adapter_version != recipe.adapter_version):
                reason = "ADAPTER_VERSION_MISMATCH"
            elif any(key in protected_fields and getattr(policy, key) != value for key, value in recipe.policy_defaults.items()):
                reason = "EXPLICIT_POLICY_CONFLICT"
            elif policy.navigation_page != 1:
                reason = "INCOMPATIBLE_OPERATION"
            else:
                try:
                    effective = replace(policy, **dict(recipe.policy_defaults))
                    manifest = providers.require_enabled(
                        recipe.provider, effective, operation=operation)
                    if manifest.authentication:
                        reason = "IDENTITY_POLICY_DENIED"
                    elif manifest.version != recipe.provider_version:
                        reason = "PROVIDER_VERSION_MISMATCH"
                    elif (recipe.basis in _SEED_BASES
                          and manifest.route_scope_required
                          and recipe.adapter_scope != "exact"):
                        reason = "ROUTE_SCOPE_REQUIRED"
                    elif (recipe.basis in _SEED_BASES
                          and not providers.is_available(
                              recipe.provider, configured=configured)):
                        reason = "PROVIDER_UNAVAILABLE"
                    else:
                        target = (seeds if recipe.basis in _SEED_BASES
                                  else plans)
                        target.append(AcquisitionPlan(
                            recipe.provider, effective, adapter, recipe))
                except WebFailure as exc:
                    reason = exc.code
                except (ValueError, TypeError):
                    reason = "POLICY_DENIED"
            if reason:
                skipped.append({"id": recipe.id, "version": recipe.version, "reason": reason})
        return RecipePlanning(tuple(plans), tuple(skipped), tuple(seeds))

    def plan_paginate(self, url, adapter, continuation_adapter, policy, protected_fields,
                      *, providers=None, adapters=None):
        """Only explicit native-sequence recipes can replace page acquisitions."""
        if adapters is None:
            from .adapters import DEFAULT_ADAPTERS
            adapters = DEFAULT_ADAPTERS
        if providers is None:
            from .providers import DEFAULT_PROVIDERS
            providers = DEFAULT_PROVIDERS
        from .runtime import WebFailure
        if policy.identity or policy.provider is not None or policy.provider_candidates is not None:
            return RecipePlanning()
        plans, skipped = [], []
        # Omission lets a version-bound recipe supply the continuation leaf.
        # An explicit continuation remains caller authority and must match.
        continuation = continuation_adapter
        for recipe in self._load():
            if (not recipe.matches(url, "paginate", adapter)
                    or continuation is not None and recipe.continuation_adapter != continuation):
                continue
            reason = None
            if not recipe.enabled:
                reason = "PLUGIN_DISABLED"
            elif any(key in protected_fields and getattr(policy, key) != value for key, value in recipe.policy_defaults.items()):
                reason = "EXPLICIT_POLICY_CONFLICT"
            elif policy.navigation_page != 1:
                reason = "INCOMPATIBLE_OPERATION"
            else:
                try:
                    effective = replace(policy, **dict(recipe.policy_defaults))
                    resolved_continuation = continuation or recipe.continuation_adapter
                    for identifier, version in ((adapter, recipe.adapter_version),
                            (resolved_continuation, recipe.continuation_adapter_version),
                            (recipe.acquisition_adapter, recipe.acquisition_adapter_version)):
                        if adapters.require_enabled(identifier).version != version:
                            reason = "ADAPTER_VERSION_MISMATCH"
                            break
                    if reason is None:
                        adapters.require_sequence_enabled(recipe.acquisition_adapter)
                        manifest = providers.require_enabled(
                            recipe.provider, effective, operation="extract")
                        if manifest.authentication:
                            reason = "IDENTITY_POLICY_DENIED"
                        elif not manifest.navigation:
                            reason = "SEQUENCE_UNSUPPORTED"
                        elif manifest.version != recipe.provider_version:
                            reason = "PROVIDER_VERSION_MISMATCH"
                        else:
                            plans.append(AcquisitionPlan(recipe.provider, effective, adapter, recipe, recipe.acquisition_adapter))
                except WebFailure as exc:
                    reason = exc.code
                except (ValueError, TypeError):
                    reason = "POLICY_DENIED"
            if reason:
                skipped.append({"id": recipe.id, "version": recipe.version, "reason": reason})
        return RecipePlanning(tuple(plans), tuple(skipped))
