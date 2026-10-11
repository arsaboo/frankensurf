"""Discover already-installed plugins without granting implicit execution trust.

Discovery is deliberately separate from Runtime wiring.  Building a catalog
clones the bundled registries, reads one private owner policy, and loads only
entry points that policy binds to an exact distribution.  No package is
downloaded, installed, imported from a path in configuration, or allowed to
replace a bundled plugin.
"""
from __future__ import annotations

import asyncio
import base64
import builtins
from dataclasses import dataclass, field, fields, is_dataclass
import dis
from importlib import metadata
import hashlib
import inspect
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
from types import (BuiltinFunctionType, CodeType, FunctionType,
                   MappingProxyType, ModuleType)
import weakref

from .adapters import AdapterManifest, AdapterRegistry
from .providers import ProviderManifest, ProviderRegistry
from .search_plugins import SearchManifest, SearchRegistry


PLUGIN_KINDS = ("provider", "search", "adapter")
ENTRY_POINT_GROUPS = MappingProxyType({
    "provider": "frankensurf.plugins.provider",
    "search": "frankensurf.plugins.search",
    "adapter": "frankensurf.plugins.adapter",
})
PLUGIN_POLICY_SCHEMA = "frankensurf.plugin-policy/v1"
PLUGIN_SNAPSHOT_SCHEMA = "frankensurf.plugin-catalog/v1"

_ID = re.compile(r"[a-z][a-z0-9_.-]{0,127}")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+:-]{0,191}")
_DISTRIBUTION = re.compile(r"[a-z0-9][a-z0-9-]{0,127}")
_REGEX_TYPE = type(re.compile(""))
_METHOD = {"provider": "acquire", "search": "search", "adapter": "extract"}


class PluginConfigurationError(ValueError):
    """A fixed-message failure for unsafe or malformed owner configuration."""


class PluginCatalogError(ValueError):
    """A fixed-message failure while cloning or inspecting bundled registries."""


@dataclass(frozen=True)
class PluginTrust:
    kind: str
    plugin_id: str
    distribution: str


@dataclass(frozen=True)
class PluginPolicy:
    trusted: tuple[PluginTrust, ...] = ()
    disabled: frozenset[tuple[str, str]] = frozenset()

    def trusted_distribution(self, kind, plugin_id):
        return next((item.distribution for item in self.trusted
                     if item.kind == kind and item.plugin_id == plugin_id), None)

    def is_disabled(self, kind, plugin_id):
        return (kind, plugin_id) in self.disabled


@dataclass(frozen=True)
class PluginFactory:
    """Explicit external factory contract for independent Runtime executions."""

    manifest: object
    configuration: tuple[tuple[str, object], ...]
    constructor: object = field(repr=False, compare=False)
    semantic_capabilities: tuple = ()


@dataclass(frozen=True)
class PluginSecretReference:
    """A non-secret vault/config reference included in binding provenance."""

    reference: str
    version: str


@dataclass(frozen=True)
class PluginRecord:
    kind: str
    plugin_id: str
    version: str
    binding_id: str
    registration_index: int
    enabled: bool
    source: str
    trust_basis: str
    manifest: tuple[tuple[str, object], ...]
    distribution: str | None = None
    distribution_version: str | None = None


@dataclass(frozen=True)
class PluginRejection:
    kind: str
    plugin_id: str
    code: str
    distribution: str | None = None


@dataclass(frozen=True)
class PluginCatalogSnapshot:
    schema: str
    plugins: tuple[PluginRecord, ...]
    rejected: tuple[PluginRejection, ...]

    def inspect(self):
        """Return a secret-free copy suitable for an eventual operator surface."""
        return {
            "schema": self.schema,
            "plugins": [{
                "kind": item.kind,
                "id": item.plugin_id,
                "version": item.version,
                "binding_id": item.binding_id,
                "registration_index": item.registration_index,
                "enabled": item.enabled,
                "source": item.source,
                "trust_basis": item.trust_basis,
                "manifest": dict(item.manifest),
                "distribution": item.distribution,
                "distribution_version": item.distribution_version,
            } for item in self.plugins],
            "rejected": [{
                "kind": item.kind,
                "id": item.plugin_id,
                "code": item.code,
                "distribution": item.distribution,
            } for item in self.rejected],
        }


class _UnsafePluginBinding(ValueError):
    pass


def _code_constant(value):
    kind = type(value)
    if value is None or value is Ellipsis or kind in (bool, int, float,
                                                       complex, str):
        return (kind.__name__, repr(value))
    if kind is bytes:
        return ("bytes", base64.b64encode(value).decode("ascii"))
    if kind is slice:
        return ("slice", _code_constant(value.start),
                _code_constant(value.stop), _code_constant(value.step))
    if kind in (tuple, frozenset):
        values = tuple(_code_constant(item) for item in value)
        if kind is frozenset:
            values = tuple(sorted(values, key=repr))
        return (kind.__name__, values)
    if isinstance(value, CodeType):
        return ("code", _code_description(value))
    raise _UnsafePluginBinding()


def _code_description(code):
    return {
        "bytecode": base64.b64encode(code.co_code).decode("ascii"),
        "exception_table": base64.b64encode(
            getattr(code, "co_exceptiontable", b"")).decode("ascii"),
        "constants": tuple(_code_constant(item) for item in code.co_consts),
        "names": code.co_names,
        "variables": code.co_varnames,
        "freevars": code.co_freevars,
        "cellvars": code.co_cellvars,
        "argcount": code.co_argcount,
        "positional_only": code.co_posonlyargcount,
        "keyword_only": code.co_kwonlyargcount,
        "flags": code.co_flags,
        "stacksize": code.co_stacksize,
    }


def _code_fingerprint(function):
    encoded = json.dumps(_code_description(function.__code__), sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _implementation_identity(value, *, strict=True):
    if type(value) is FunctionType:
        return (value.__module__, value.__qualname__,
                _code_fingerprint(value))
    plugin_type = value if isinstance(value, type) else type(value)
    rows = []
    for base in reversed(plugin_type.__mro__[:-1]):
        for method_name, descriptor in sorted(vars(base).items()):
            functions = []
            if type(descriptor) is FunctionType:
                functions = [descriptor]
            elif isinstance(descriptor, (staticmethod, classmethod)):
                functions = [descriptor.__func__]
            elif isinstance(descriptor, property):
                functions = [item for item in (
                    descriptor.fget, descriptor.fset, descriptor.fdel)
                    if item is not None]
            for function in functions:
                if not strict:
                    rows.append((base.__module__, base.__qualname__,
                                 method_name, _code_fingerprint(function)))
                    continue
                if function.__dict__:
                    raise _UnsafePluginBinding()
                defaults = _safe_function_defaults(function)
                closure = []
                for capture_name, cell in zip(function.__code__.co_freevars,
                                      function.__closure__ or ()):
                    captured = cell.cell_contents
                    if capture_name == "__class__" and captured is plugin_type:
                        closure.append((capture_name, ("plugin_class",)))
                        continue
                    if captured is plugin_type:
                        raise _UnsafePluginBinding()
                    closure.append((capture_name, _named_function_capture(
                        capture_name, captured)))
                globals_used = []
                for global_name in sorted({instruction.argval
                                    for instruction in dis.get_instructions(function)
                                    if instruction.opname in {"LOAD_GLOBAL", "LOAD_NAME"}}):
                    if global_name not in function.__globals__:
                        continue
                    dependency = function.__globals__[global_name]
                    if dependency is plugin_type:
                        # A direct `PluginType.state` reference bypasses the
                        # per-session subclass and cannot be isolated safely.
                        raise _UnsafePluginBinding()
                    globals_used.append((global_name, _global_dependency(
                        global_name, dependency)))
                rows.append((base.__module__, base.__qualname__, method_name,
                             _code_fingerprint(function), defaults,
                             tuple(closure), tuple(globals_used)))
    encoded = json.dumps(rows, sort_keys=True, separators=(",", ":"),
                         allow_nan=False).encode()
    return (plugin_type.__module__, plugin_type.__qualname__,
            hashlib.sha256(encoded).hexdigest())


def _new_cell(value):
    return (lambda captured: lambda: captured)(value).__closure__[0]


class _FunctionTemplate:
    """Core-owned immutable description used to clone one plugin method."""

    def __init__(self, function, plugin_type, *, clone_globals=True):
        self.code = function.__code__
        self.globals = dict(function.__globals__)
        self.name = function.__name__
        self.qualname = function.__qualname__
        self.module = function.__module__
        self.doc = function.__doc__
        self.global_functions = (tuple(
            (name, _FunctionTemplate(function.__globals__[name], plugin_type,
                                     clone_globals=False))
            for name in sorted({instruction.argval
                for instruction in dis.get_instructions(function)
                if instruction.opname in {"LOAD_GLOBAL", "LOAD_NAME"}})
            if name in function.__globals__
            and type(function.__globals__[name]) is FunctionType
            and function.__globals__[name].__module__.split(
                ".", 1)[0] not in sys.stdlib_module_names
            and not function.__globals__[name].__closure__)
            if clone_globals else ())
        self.defaults = tuple(_function_capture(value)
                              for value in (function.__defaults__ or ()))
        self.keyword_defaults = tuple(
            (name, _function_capture(value))
            for name, value in sorted((function.__kwdefaults__ or {}).items()))
        self.closure = []
        for name, cell in zip(function.__code__.co_freevars,
                              function.__closure__ or ()):
            value = cell.cell_contents
            if name == "__class__" and value is plugin_type:
                self.closure.append(("plugin_class",))
            else:
                self.closure.append(_named_function_capture(name, value))

    def create(self, plugin_type):
        defaults = tuple(_thaw_value(value) for value in self.defaults)
        closure = tuple(_new_cell(
            plugin_type if value == ("plugin_class",) else _thaw_value(value))
            for value in self.closure)
        globals_copy = self.globals.copy()
        for name, template in self.global_functions:
            globals_copy[name] = template.create(plugin_type)
        function = FunctionType(
            self.code, globals_copy, self.name,
            defaults if defaults else None, closure if closure else None)
        function.__kwdefaults__ = {
            name: _thaw_value(value)
            for name, value in self.keyword_defaults} or None
        function.__qualname__ = self.qualname
        function.__module__ = self.module
        function.__doc__ = self.doc
        return function


def _method_templates(plugin_type):
    templates = {}
    for base in reversed(plugin_type.__mro__[:-1]):
        for name, descriptor in vars(base).items():
            if type(descriptor) is FunctionType:
                templates[name] = ("function",
                                   _FunctionTemplate(descriptor, plugin_type))
            elif isinstance(descriptor, staticmethod):
                templates[name] = ("staticmethod",
                    _FunctionTemplate(descriptor.__func__, plugin_type))
            elif isinstance(descriptor, classmethod):
                templates[name] = ("classmethod",
                    _FunctionTemplate(descriptor.__func__, plugin_type))
            elif isinstance(descriptor, property):
                templates[name] = ("property", tuple(
                    _FunctionTemplate(function, plugin_type)
                    if function is not None else None
                    for function in (descriptor.fget, descriptor.fset,
                                     descriptor.fdel)), descriptor.__doc__)
    return tuple(sorted(templates.items()))


def _materialize_methods(templates, plugin_type):
    namespace = {}
    for name, template in templates:
        kind = template[0]
        if kind == "function":
            namespace[name] = template[1].create(plugin_type)
        elif kind == "staticmethod":
            namespace[name] = staticmethod(template[1].create(plugin_type))
        elif kind == "classmethod":
            namespace[name] = classmethod(template[1].create(plugin_type))
        else:
            functions, doc = template[1], template[2]
            namespace[name] = property(*(
                function.create(plugin_type) if function is not None else None
                for function in functions), doc=doc)
    return namespace


def _function_capture(value):
    kind = type(value)
    if value is None:
        return ("none",)
    if kind in (bool, int, str, bytes):
        return (kind.__name__, (base64.b64encode(value).decode("ascii")
                                if kind is bytes else value))
    if kind is float and math.isfinite(value):
        return ("float", value)
    if isinstance(value, Path):
        return ("path", str(value))
    for manifest_type, label in (
            (ProviderManifest, "provider_manifest"),
            (SearchManifest, "search_manifest"),
            (AdapterManifest, "adapter_manifest")):
        if type(value) is manifest_type:
            return (label, tuple(
                (item.name, _function_capture(getattr(value, item.name)))
                for item in fields(value)))
    if type(value) is PluginSecretReference:
        return _secret_reference_snapshot(value)
    if kind in (tuple, frozenset):
        values = tuple(_function_capture(item) for item in value)
        if kind is frozenset:
            values = tuple(sorted(values, key=lambda item: json.dumps(
                item, sort_keys=True, separators=(",", ":"))))
        return (kind.__name__, values)
    raise _UnsafePluginBinding()


def _secret_reference_snapshot(value):
    if (type(value.reference) is not str or not value.reference
            or len(value.reference) > 512
            or type(value.version) is not str
            or _VERSION.fullmatch(value.version) is None):
        raise _UnsafePluginBinding()
    return ("secret_reference", value.reference, value.version)


def _named_function_capture(name, value):
    if _SENSITIVE_CONFIGURATION.search(name):
        if type(value) is not PluginSecretReference:
            raise _UnsafePluginBinding()
        return _secret_reference_snapshot(value)
    return _function_capture(value)


def _safe_function_defaults(function):
    positional = function.__defaults__ or ()
    names = function.__code__.co_varnames[:function.__code__.co_argcount]
    positional_names = names[len(names) - len(positional):]
    defaults = tuple((name, _named_function_capture(name, value))
                     for name, value in zip(positional_names, positional))
    keywords = tuple((name, _named_function_capture(name, value))
                     for name, value in sorted(
                         (function.__kwdefaults__ or {}).items()))
    return (defaults, keywords)


def _global_dependency(name, value):
    if _SENSITIVE_CONFIGURATION.search(name):
        if type(value) is not PluginSecretReference:
            raise _UnsafePluginBinding()
        return _secret_reference_snapshot(value)
    if inspect.ismodule(value):
        return ("module", value.__name__)
    if type(value) is FunctionType:
        # Rebinding the module global is pinned by each cloned method. Its code
        # identity is included so ordinary helper changes alter the binding.
        if value.__closure__ or value.__dict__:
            raise _UnsafePluginBinding()
        _validate_helper_function(value)
        try:
            defaults = _safe_function_defaults(value)
        except _UnsafePluginBinding:
            if value.__module__.split(".", 1)[0] not in sys.stdlib_module_names:
                raise
            defaults = ("stdlib_defaults",)
        return ("function", value.__module__, value.__qualname__,
                _code_fingerprint(value), defaults)
    if isinstance(value, type):
        return ("type", value.__module__, value.__qualname__)
    if inspect.isbuiltin(value):
        return ("builtin", getattr(value, "__module__", "builtins"),
                getattr(value, "__qualname__", getattr(value, "__name__", "")))
    return _function_capture(value)


def _validate_helper_function(function, seen=None):
    """Reject imported helper graphs that retain mutable execution state."""
    if (function.__module__.split(".", 1)[0] in sys.stdlib_module_names
            or function.__module__.startswith("frankensurf.")):
        return
    seen = set() if seen is None else seen
    if id(function) in seen:
        return
    seen.add(id(function))
    if function.__closure__ or function.__dict__:
        raise _UnsafePluginBinding()
    _safe_function_defaults(function)
    for dependency_name in {instruction.argval
                            for instruction in dis.get_instructions(function)
                            if instruction.opname in {"LOAD_GLOBAL", "LOAD_NAME"}}:
        if dependency_name not in function.__globals__:
            continue
        dependency = function.__globals__[dependency_name]
        if inspect.ismodule(dependency) or inspect.isbuiltin(dependency):
            continue
        if type(dependency) is FunctionType:
            _validate_helper_function(dependency, seen)
            continue
        if isinstance(dependency, type):
            if dependency.__module__ == function.__module__:
                raise _UnsafePluginBinding()
            continue
        _named_function_capture(dependency_name, dependency)


def _frozen_value(value, seen=None, function_references=None,
                  observed_references=None):
    """Core-owned state snapshot; never invokes plugin copy/pickle hooks."""
    seen = set() if seen is None else seen
    kind = type(value)
    if value is None:
        return ("none",)
    if kind is bool:
        return ("bool", value)
    if kind is int:
        return ("int", value)
    if kind is float:
        if not math.isfinite(value):
            raise _UnsafePluginBinding()
        return ("float", value)
    if kind is str:
        return ("str", value)
    if kind is bytes:
        return ("bytes", base64.b64encode(value).decode("ascii"))
    if isinstance(value, Path):
        return ("path", str(value))
    for manifest_type, label in (
            (ProviderManifest, "provider_manifest"),
            (SearchManifest, "search_manifest"),
            (AdapterManifest, "adapter_manifest")):
        if type(value) is manifest_type:
            return (label, tuple(
                (item.name, _frozen_value(
                    getattr(value, item.name), seen, function_references,
                    observed_references))
                for item in fields(value)))
    if type(value) is PluginSecretReference:
        return _secret_reference_snapshot(value)
    # WebFailure is a core-owned typed compatibility value. Its exception
    # traceback and deepcopy hook are deliberately not retained or invoked.
    from .runtime import WebFailure
    if type(value) is WebFailure:
        if (observed_references is not None
                and id(value) in observed_references):
            raise _UnsafePluginBinding()
        if observed_references is not None:
            observed_references.add(id(value))
        return ("web_failure", _frozen_value(
            dict(value.__dict__), seen, function_references,
            observed_references))
    if kind is FunctionType:
        # Trusted bundled/test compatibility may retain callbacks. External
        # object entry points pass no reference table and are rejected because
        # their hidden closure/default state cannot be isolated safely.
        if function_references is None:
            raise _UnsafePluginBinding()
        reference = next((index for index, item in
                          enumerate(function_references) if item is value), None)
        if reference is None:
            reference = len(function_references)
            function_references.append(value)
        return ("function_ref", reference, value.__module__,
                value.__qualname__, _code_fingerprint(value))
    if kind in (tuple, list, set, frozenset, dict):
        # CPython interns the empty tuple. Repeated immutable empty manifest
        # fields therefore share identity without sharing mutable plugin state.
        if kind is tuple and not value:
            return ("tuple", ())
        if id(value) in seen:
            raise _UnsafePluginBinding()
        if (observed_references is not None
                and id(value) in observed_references):
            # The compatibility snapshot intentionally rejects aliased object
            # graphs rather than silently changing plugin `is` semantics.
            raise _UnsafePluginBinding()
        if observed_references is not None:
            observed_references.add(id(value))
        seen.add(id(value))
        try:
            if kind is dict:
                if any(type(key) is not str for key in value):
                    raise _UnsafePluginBinding()
                return ("dict", tuple(
                    (key, _frozen_value(
                        item, seen, function_references,
                        observed_references))
                    for key, item in sorted(value.items())))
            values = tuple(_frozen_value(
                item, seen, function_references, observed_references)
                           for item in value)
            if kind in (set, frozenset):
                values = tuple(sorted(values, key=lambda item: json.dumps(
                    item, sort_keys=True, separators=(",", ":"))))
            return (kind.__name__, values)
        finally:
            seen.remove(id(value))
    raise _UnsafePluginBinding()


def _thaw_value(snapshot, function_references=None):
    kind = snapshot[0]
    if kind == "none":
        return None
    if kind in {"bool", "int", "float", "str"}:
        return snapshot[1]
    if kind == "bytes":
        return base64.b64decode(snapshot[1])
    if kind == "path":
        return Path(snapshot[1])
    if kind == "secret_reference":
        return PluginSecretReference(snapshot[1], snapshot[2])
    if kind == "function_ref":
        if (function_references is None or type(snapshot[1]) is not int
                or not 0 <= snapshot[1] < len(function_references)):
            raise _UnsafePluginBinding()
        return function_references[snapshot[1]]
    manifest_types = {
        "provider_manifest": ProviderManifest,
        "search_manifest": SearchManifest,
        "adapter_manifest": AdapterManifest,
    }
    if kind in manifest_types:
        return manifest_types[kind](**{
            name: _thaw_value(value, function_references)
            for name, value in snapshot[1]})
    if kind == "web_failure":
        from .runtime import WebFailure
        state = _thaw_value(snapshot[1], function_references)
        failure = WebFailure(state["code"], state["message"],
                             state.get("http_status"))
        failure.__dict__.clear()
        failure.__dict__.update(state)
        return failure
    if kind == "dict":
        return {key: _thaw_value(value, function_references)
                for key, value in snapshot[1]}
    values = [_thaw_value(value, function_references)
              for value in snapshot[1]]
    if kind == "tuple":
        return tuple(values)
    if kind == "list":
        return values
    if kind == "set":
        return set(values)
    if kind == "frozenset":
        return frozenset(values)
    raise _UnsafePluginBinding()


_SENSITIVE_CONFIGURATION = re.compile(
    r"(?:secret|token|password|credential|cookie|api[_-]?key)", re.I)


def _configuration_provenance(entries):
    result = []
    for name, value in entries:
        if _SENSITIVE_CONFIGURATION.search(name):
            if not value or value[0] != "secret_reference":
                raise _UnsafePluginBinding()
        result.append((name, value))
    return tuple(result)


def _manifest_value(value):
    kind = type(value)
    if value is None or kind in (bool, int, str):
        return value
    if kind is float and math.isfinite(value):
        return value
    if kind is tuple:
        return tuple(_manifest_value(item) for item in value)
    if kind is frozenset:
        return tuple(sorted((_manifest_value(item) for item in value),
                            key=repr))
    raise ValueError()


def _copy_manifest_value(value):
    kind = type(value)
    if value is None or kind in (bool, int, str):
        return value
    if kind is float and math.isfinite(value):
        return value
    if kind is tuple:
        return tuple(_copy_manifest_value(item) for item in value)
    if kind is frozenset:
        return frozenset(_copy_manifest_value(item) for item in value)
    raise _UnsafePluginBinding()


_MANIFEST_FIELDS = MappingProxyType({
    "provider": ("id", "version", "rendering", "requires_local_browser",
                 "paid", "authentication", "navigation", "cost_bounded",
                 "operations", "diagnosis"),
    "search": ("id", "version", "paid", "cost_bounded",
               "transport_provider"),
    "adapter": ("id", "version"),
})

_MANIFEST_TYPES = MappingProxyType({
    "provider": ProviderManifest,
    "search": SearchManifest,
    "adapter": AdapterManifest,
})


def _canonical_manifest(kind, manifest):
    """Return every typed immutable manifest field used for provenance."""
    expected = _MANIFEST_TYPES.get(kind)
    if expected is None or not isinstance(manifest, expected) or not is_dataclass(manifest):
        raise ValueError()
    names = tuple(item.name for item in fields(manifest))
    if not set(_MANIFEST_FIELDS[kind]) <= set(names):
        raise ValueError()
    values = []
    for name in names:
        if _SENSITIVE_CONFIGURATION.search(name):
            raise ValueError()
        value = _manifest_value(getattr(manifest, name))
        values.append((name, value))
    values = tuple(values)
    described = dict(values)
    if (type(described["id"]) is not str
            or _ID.fullmatch(described["id"]) is None
            or type(described["version"]) is not str
            or _VERSION.fullmatch(described["version"]) is None):
        raise ValueError()
    for name in _MANIFEST_FIELDS[kind]:
        value = described[name]
        if name in {"id", "version"}:
            continue
        if name == "operations":
            if (type(value) is not tuple or not value
                    or any(type(operation) is not str
                           or operation not in {"read", "extract", "do"}
                           for operation in value)
                    or len(set(value)) != len(value)):
                raise ValueError()
            continue
        if name == "transport_provider":
            if (value is not None and (type(value) is not str
                    or _ID.fullmatch(value) is None)):
                raise ValueError()
        elif type(value) is not bool:
            raise ValueError()
    return values


def _copy_manifest(kind, manifest):
    values = _canonical_manifest(kind, manifest)
    manifest_type = type(manifest)
    try:
        copied = object.__new__(manifest_type)
        for item in fields(manifest):
            object.__setattr__(copied, item.name,
                               _copy_manifest_value(
                                   getattr(manifest, item.name)))
    except Exception:
        raise _UnsafePluginBinding() from None
    if _canonical_manifest(kind, copied) != values:
        raise _UnsafePluginBinding()
    return copied


def _instance_state(plugin, function_references, observed_references):
    state = {}
    try:
        raw = object.__getattribute__(plugin, "__dict__")
    except AttributeError:
        raw = None
    except BaseException:
        raise _UnsafePluginBinding() from None
    if raw is not None:
        if type(raw) is not dict:
            raise _UnsafePluginBinding()
        state.update(raw)
    for base in type(plugin).__mro__:
        slots = vars(base).get("__slots__", ())
        if isinstance(slots, str):
            slots = (slots,)
        for name in slots:
            if name in {"__dict__", "__weakref__"} or name in state:
                continue
            try:
                state[name] = object.__getattribute__(plugin, name)
            except AttributeError:
                pass
            except Exception:
                raise _UnsafePluginBinding() from None
    state.pop("manifest", None)
    state.pop("binding_id", None)
    return tuple((name, _frozen_value(
                    value, function_references=function_references,
                    observed_references=observed_references))
                 for name, value in sorted(state.items()))


def _class_configuration(plugin_or_type, observed_references):
    values = {}
    plugin_type = (plugin_or_type if isinstance(plugin_or_type, type)
                   else type(plugin_or_type))
    for base in reversed(plugin_type.__mro__[:-1]):
        for name, value in vars(base).items():
            if ((name.startswith("__") and name.endswith("__"))
                    or name in {"manifest", "binding_id"}
                    or callable(value)
                    or isinstance(value, (property, staticmethod, classmethod))
                    or inspect.ismemberdescriptor(value)
                    or inspect.isgetsetdescriptor(value)):
                continue
            try:
                trial = set(observed_references)
                values[name] = _frozen_value(
                    value, observed_references=trial)
                observed_references.clear()
                observed_references.update(trial)
            except _UnsafePluginBinding:
                # An opaque class-owned value would otherwise be shared by all
                # Runtime sessions and bypass execution isolation.
                raise
    return tuple(sorted(values.items()))


class _CompatibilityFactory:
    """Explicit core-owned snapshot factory for legacy plugin objects."""

    def __init__(self, kind, plugin, *, strict=True):
        self.kind = kind
        self.plugin_type = type(plugin)
        self.manifest = _copy_manifest(kind, plugin.manifest)
        self.implementation = _implementation_identity(
            self.plugin_type, strict=strict)
        self.methods = (_method_templates(self.plugin_type) if strict else ())
        self.function_references = None if strict else []
        observed_references = set()
        self.state = _instance_state(
            plugin, self.function_references, observed_references)
        self.class_configuration = _class_configuration(
            plugin, observed_references)
        self.provenance = (
            ("factory", self.implementation),
            ("state", _configuration_provenance(self.state)),
            ("class_configuration",
             _configuration_provenance(self.class_configuration)),
        )

    def create(self, manifest, binding_id, semantic_capabilities=()):
        pinned_manifest = manifest
        pinned_capabilities = semantic_capabilities
        namespace = {**(_materialize_methods(self.methods, self.plugin_type)
                        if self.methods else {}),
            "__module__": self.plugin_type.__module__,
            "__slots__": (),
            "manifest": property(lambda instance: pinned_manifest,
                                 lambda instance, value: None),
            "binding_id": property(lambda instance: binding_id,
                                   lambda instance, value: None),
        }
        for name, value in self.class_configuration:
            namespace[name] = _thaw_value(value, self.function_references)
        if self.kind == "adapter":
            namespace["semantic_capabilities"] = property(
                lambda instance: pinned_capabilities,
                lambda instance, value: None)
        try:
            execution_type = type(
                "_FrankenSurfSession" + self.plugin_type.__name__,
                (self.plugin_type,), namespace)
            execution = object.__new__(execution_type)
            try:
                raw = object.__getattribute__(execution, "__dict__")
            except AttributeError:
                raw = None
            if raw is not None:
                raw.update({name: _thaw_value(value, self.function_references)
                            for name, value in self.state})
            else:
                for name, value in self.state:
                    object.__setattr__(execution, name, _thaw_value(
                        value, self.function_references))
        except BaseException:
            raise _UnsafePluginBinding() from None
        return execution


class _ExplicitFactory:
    def __init__(self, kind, factory):
        if (not isinstance(factory.constructor, type)
                or type(factory.configuration) is not tuple
                or any(type(item) is not tuple or len(item) != 2
                       or type(item[0]) is not str
                       for item in factory.configuration)):
            raise _UnsafePluginBinding()
        self.kind = kind
        self.manifest = _copy_manifest(kind, factory.manifest)
        frozen = tuple((name, _frozen_value(value))
                       for name, value in factory.configuration)
        if len({name for name, _ in frozen}) != len(frozen):
            raise _UnsafePluginBinding()
        configuration_provenance = _configuration_provenance(frozen)
        self.constructor = factory.constructor
        self.implementation = _implementation_identity(self.constructor)
        self.methods = _method_templates(self.constructor)
        self.class_configuration = _class_configuration(
            self.constructor, set())
        self._issued = []
        self._nonweak_provenance = None
        try:
            implementation = self.implementation
        except BaseException:
            raise _UnsafePluginBinding() from None
        if (any(type(item) is not str or not item or len(item) > 512
                for item in implementation)):
            raise _UnsafePluginBinding()
        self.provenance = (
            ("factory", ("explicit", *implementation)),
            ("configuration", configuration_provenance),
            ("class_configuration",
             _configuration_provenance(self.class_configuration)),
        )

    def create(self, manifest, binding_id, semantic_capabilities=()):
        try:
            if (_implementation_identity(self.constructor)
                    != self.implementation
                    or _class_configuration(self.constructor, set())
                    != self.class_configuration):
                raise _UnsafePluginBinding()
            plugin = self.constructor()
            if type(plugin) is not self.constructor:
                raise _UnsafePluginBinding()
            try:
                reference = weakref.ref(plugin)
            except TypeError:
                compatibility = _CompatibilityFactory(self.kind, plugin)
                if (_canonical_manifest(self.kind, compatibility.manifest)
                        != _canonical_manifest(self.kind, manifest)):
                    raise _UnsafePluginBinding()
                if (self._nonweak_provenance is not None
                        and compatibility.provenance
                        != self._nonweak_provenance):
                    raise _UnsafePluginBinding()
                self._nonweak_provenance = compatibility.provenance
                return compatibility.create(
                    manifest, binding_id, semantic_capabilities)
            issued = [reference for reference in self._issued
                      if reference() is not None]
            if any(reference() is plugin for reference in issued):
                raise _UnsafePluginBinding()
            issued.append(reference)
            self._issued = issued
        except _UnsafePluginBinding:
            raise
        except BaseException:
            raise _UnsafePluginBinding() from None
        return _pin_created_plugin(
            self.kind, plugin, manifest, binding_id,
            class_configuration=self.class_configuration,
            methods=self.methods,
            semantic_capabilities=semantic_capabilities)

    def discard(self, plugin):
        self._issued = [reference for reference in self._issued
                        if reference() is not None and reference() is not plugin]


def _pin_created_plugin(kind, plugin, manifest, binding_id, *,
                        class_configuration=None, methods=None,
                        semantic_capabilities=()):
    if _canonical_manifest(kind, plugin.manifest) != _canonical_manifest(
            kind, manifest):
        raise _UnsafePluginBinding()
    base = type(plugin)
    pinned_manifest = manifest
    if class_configuration is None:
        class_configuration = _class_configuration(base, set())
    if methods is None:
        methods = _method_templates(base)
    try:
        namespace = {**_materialize_methods(methods, base),
            "__module__": base.__module__, "__slots__": (),
            "manifest": property(lambda instance: pinned_manifest,
                                 lambda instance, value: None),
            "binding_id": property(lambda instance: binding_id,
                                   lambda instance, value: None),
        }
        for name, value in class_configuration:
            namespace[name] = _thaw_value(value)
        if kind == "adapter":
            pinned_capabilities = semantic_capabilities
            namespace["semantic_capabilities"] = property(
                lambda instance: pinned_capabilities,
                lambda instance, value: None)
        pinned_type = type(
            "_FrankenSurfSession" + base.__name__, (base,),
            namespace)
        plugin.__class__ = pinned_type
    except (AttributeError, TypeError):
        raise _UnsafePluginBinding() from None
    return plugin


@dataclass(frozen=True)
class PluginBinding:
    kind: str
    plugin_id: str
    manifest: object = field(repr=False, compare=False)
    binding_id: str
    enabled: bool
    source: str
    trust_basis: str
    distribution: str | None
    distribution_version: str | None
    _factory: object = field(repr=False, compare=False)
    semantic_capabilities: tuple = ()

    def create(self):
        return self._factory.create(
            self.manifest, self.binding_id, self.semantic_capabilities)

    def discard(self, plugin):
        method = getattr(self._factory, "discard", None)
        if method is not None:
            method(plugin)


def _semantic_code_global_names(code):
    names = {instruction.argval for instruction in dis.get_instructions(code)
             if instruction.opname in {"LOAD_GLOBAL", "LOAD_NAME"}}
    for constant in code.co_consts:
        if isinstance(constant, CodeType):
            names.update(_semantic_code_global_names(constant))
    return names


def _semantic_source_identity(value):
    kind = type(value)
    if kind is FunctionType:
        module_name = value.__module__
    elif type(value) is type:
        module_name = type.__getattribute__(value, "__module__")
    else:
        raise _UnsafePluginBinding()
    try:
        source = inspect.getsourcefile(value)
        source_path = Path(source).resolve() if type(source) is str else None
    except (OSError, TypeError):
        source_path = None
    if source_path is not None and source_path.is_file():
        return (module_name, "source_sha256",
                hashlib.sha256(source_path.read_bytes()).hexdigest())
    return (module_name, "builtin")


def _semantic_canonical_reference(value, module_name, qualname):
    module = sys.modules.get(module_name)
    if type(module) is not ModuleType or "<locals>" in qualname:
        return False
    current = vars(module).get(qualname.split(".", 1)[0])
    for name in qualname.split(".")[1:]:
        if type(current) is not type:
            return False
        current = vars(current).get(name)
        if type(current) in (staticmethod, classmethod):
            current = current.__func__
    return current is value


def _semantic_function_defaults(function, local_modules, active):
    positional = function.__defaults__ or ()
    names = function.__code__.co_varnames[:function.__code__.co_argcount]
    positional_names = names[len(names) - len(positional):]
    defaults = tuple((name, _semantic_named_identity(
        name, value, local_modules, active))
        for name, value in zip(positional_names, positional))
    keywords = tuple((name, _semantic_named_identity(
        name, value, local_modules, active))
        for name, value in sorted((function.__kwdefaults__ or {}).items()))
    return defaults, keywords


def _semantic_function_identity(function, local_modules, active):
    if type(function) is not FunctionType or function.__closure__:
        raise _UnsafePluginBinding()
    module_name = function.__module__
    qualname = function.__qualname__
    if module_name in local_modules and function.__dict__:
        raise _UnsafePluginBinding()
    if (function.__builtins__ is not vars(builtins)
            or _semantic_code_global_names(function.__code__) & {
                "__import__", "eval", "exec", "globals", "locals"}):
        raise _UnsafePluginBinding()
    reference = (module_name, qualname, _code_fingerprint(function))
    if id(function) in active:
        return ("function_reference", *reference)
    if (module_name not in local_modules
            and not _semantic_canonical_reference(
                function, module_name, qualname)):
        raise _UnsafePluginBinding()
    active.add(id(function))
    try:
        defaults = _semantic_function_defaults(
            function, local_modules, active)
        globals_identity = ()
        if module_name in local_modules:
            globals_identity = tuple((name, _semantic_named_identity(
                name, function.__globals__[name], local_modules, active))
                for name in sorted(_semantic_code_global_names(
                    function.__code__)) if name in function.__globals__)
        return ("function", *reference, defaults,
                _semantic_source_identity(function), globals_identity)
    finally:
        active.remove(id(function))


def _semantic_type_identity(value, local_modules, active):
    if type(value) is not type:
        raise _UnsafePluginBinding()
    module_name = type.__getattribute__(value, "__module__")
    qualname = type.__getattribute__(value, "__qualname__")
    if module_name not in local_modules:
        if not _semantic_canonical_reference(value, module_name, qualname):
            raise _UnsafePluginBinding()
        return ("imported_type", module_name, qualname,
                _implementation_identity(value, strict=False),
                _semantic_source_identity(value))
    reference = (module_name, qualname)
    if id(value) in active:
        return ("type_reference", *reference)
    active.add(id(value))
    try:
        methods = []
        for base in reversed(value.__mro__[:-1]):
            for name, descriptor in sorted(vars(base).items()):
                functions = []
                if type(descriptor) is FunctionType:
                    functions = [descriptor]
                elif type(descriptor) in (staticmethod, classmethod):
                    functions = [descriptor.__func__]
                elif type(descriptor) is property:
                    functions = [item for item in (
                        descriptor.fget, descriptor.fset, descriptor.fdel)
                        if item is not None]
                for function in functions:
                    methods.append((name, _semantic_function_identity(
                        function, local_modules, active)))
        bases = tuple(_semantic_type_identity(
            base, local_modules, active) for base in value.__bases__
            if base is not object)
        return ("type", *reference, tuple(methods), bases,
                _configuration_provenance(
                    _class_configuration(value, set())))
    finally:
        active.remove(id(value))


def _semantic_immutable_identity(value):
    kind = type(value)
    if value is None:
        return ("none",)
    if kind is bool:
        return ("bool", value)
    if kind is int:
        return ("int", value)
    if kind is float:
        if not math.isfinite(value):
            raise _UnsafePluginBinding()
        return ("float", value)
    if kind is str:
        return ("str", value)
    if kind is bytes:
        return ("bytes", base64.b64encode(value).decode("ascii"))
    if kind is tuple:
        return ("tuple", tuple(
            _semantic_immutable_identity(item) for item in value))
    if kind is frozenset:
        values = tuple(_semantic_immutable_identity(item) for item in value)
        return ("frozenset", tuple(sorted(
            values, key=lambda item: json.dumps(
                item, sort_keys=True, separators=(",", ":")))))
    raise _UnsafePluginBinding()


def _semantic_named_identity(name, value, local_modules, active):
    if _SENSITIVE_CONFIGURATION.search(name):
        if type(value) is not PluginSecretReference:
            raise _UnsafePluginBinding()
        return _secret_reference_snapshot(value)
    kind = type(value)
    if kind is ModuleType:
        # Module objects are mutable shared state. Semantic adapters must import
        # the exact functions/classes they execute instead.
        raise _UnsafePluginBinding()
    if kind is FunctionType:
        return _semantic_function_identity(value, local_modules, active)
    if type(value) is type:
        return _semantic_type_identity(value, local_modules, active)
    if kind is _REGEX_TYPE:
        return ("regex", value.pattern, value.flags)
    if kind is BuiltinFunctionType:
        return ("builtin", value.__module__, value.__qualname__)
    if kind in (type(None), bool, int, float, str, bytes,
                tuple, frozenset):
        return _semantic_immutable_identity(value)
    raise _UnsafePluginBinding()


def _semantic_module_identity(plugin):
    """Bind exact code, globals and sources for bundled semantic execution."""
    plugin_type = type(plugin)
    module_names = tuple(sorted({type.__getattribute__(base, "__module__")
        for base in plugin_type.__mro__[:-1]
        if type.__getattribute__(base, "__module__").startswith(
            "frankensurf.")}))
    if not module_names:
        raise _UnsafePluginBinding()
    rows = []
    for module_name in module_names:
        module = sys.modules.get(module_name)
        source = (vars(module).get("__file__")
                  if type(module) is ModuleType else None)
        source_path = (Path(source).resolve()
                       if type(source) is str else None)
        if source_path is None or not source_path.is_file():
            raise _UnsafePluginBinding()
        rows.append((module_name, hashlib.sha256(
            source_path.read_bytes()).hexdigest()))
    return (tuple(rows), _semantic_type_identity(
        plugin_type, frozenset(module_names), set()))


def _binding(kind, plugin_or_factory, *, enabled=True, source="bundled",
             trust_basis="bundled", distribution=None,
             distribution_version=None):
    semantic_capabilities = ()
    semantic_modules = ()
    if kind == "adapter":
        try:
            raw_capabilities = getattr(
                plugin_or_factory, "semantic_capabilities", ())
            semantic_capabilities = _manifest_value(raw_capabilities)
            if (semantic_capabilities and source == "bundled"
                    and not isinstance(plugin_or_factory, PluginFactory)):
                semantic_modules = _semantic_module_identity(
                    plugin_or_factory)
        except Exception:
            raise _UnsafePluginBinding() from None
        if type(semantic_capabilities) is not tuple:
            raise _UnsafePluginBinding()
    factory = (_ExplicitFactory(kind, plugin_or_factory)
               if isinstance(plugin_or_factory, PluginFactory)
               else _CompatibilityFactory(
                   kind, plugin_or_factory,
                   strict=(source != "bundled"
                           or bool(semantic_modules))))
    manifest = factory.manifest
    described = _canonical_manifest(kind, manifest)
    payload = {
        "schema": "frankensurf.plugin-binding/v1",
        "kind": kind,
        "manifest": described,
        "source": source,
        "trust_basis": trust_basis,
        "distribution": distribution,
        "distribution_version": distribution_version,
        "configuration": factory.provenance,
    }
    if semantic_capabilities:
        payload["semantic_capabilities"] = semantic_capabilities
    if semantic_modules:
        payload["semantic_modules"] = semantic_modules
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         allow_nan=False).encode()
    binding_id = hashlib.sha256(encoded).hexdigest()
    return PluginBinding(
        kind, dict(described)["id"], manifest, binding_id, enabled,
        source, trust_basis, distribution, distribution_version, factory,
        semantic_capabilities)


class _DefinitionPlugin:
    def __init__(self, binding):
        self.manifest = binding.manifest
        self.binding_id = binding.binding_id
        self.semantic_capabilities = binding.semantic_capabilities

    def available(self, configured):
        return True

    async def acquire(self, request, services):
        raise RuntimeError("Catalog definitions are not executable")

    async def perform(self, request, services):
        raise RuntimeError("Catalog definitions are not executable")

    async def diagnose(self, request, services):
        raise RuntimeError("Catalog definitions are not executable")

    async def search(self, request, services):
        raise RuntimeError("Catalog definitions are not executable")

    def extract(self, request):
        raise RuntimeError("Catalog definitions are not executable")


def _registry_for(kind):
    return {"provider": ProviderRegistry, "search": SearchRegistry,
            "adapter": AdapterRegistry}[kind]()


def _definition_registries(bindings):
    registries = {kind: _registry_for(kind) for kind in PLUGIN_KINDS}
    for binding in bindings:
        registry = registries[binding.kind]
        registry.register(_DefinitionPlugin(binding))
        if not binding.enabled:
            registry.enable(binding.plugin_id, False)
    return {kind: registry.freeze() for kind, registry in registries.items()}


async def _lifecycle_call(plugin, name):
    method = getattr(plugin, name, None)
    if method is None:
        return
    result = method()
    if inspect.isawaitable(result):
        await result
    elif result is not None:
        raise TypeError()


class PluginSession:
    """Independent execution instances and lifecycle for one Runtime."""

    def __init__(self, catalog):
        self.catalog = catalog
        self.state = "created"
        self._lifecycle_lock = asyncio.Lock()
        self._executions = []
        self._created_bindings = []
        self._started = []
        registries = {kind: _registry_for(kind) for kind in PLUGIN_KINDS}
        try:
            for binding in catalog.bindings:
                registry = registries[binding.kind]
                if binding.enabled:
                    plugin = binding.create()
                    manifest = registry.register(plugin)
                    if (_canonical_manifest(binding.kind, manifest)
                            != _canonical_manifest(
                                binding.kind, binding.manifest)):
                        raise _UnsafePluginBinding()
                    self._executions.append(plugin)
                    self._created_bindings.append((binding, plugin))
                else:
                    registry.register(_DefinitionPlugin(binding))
                    registry.enable(binding.plugin_id, False)
        except BaseException:
            for binding, plugin in reversed(self._created_bindings):
                binding.discard(plugin)
            self._created_bindings.clear()
            self._executions.clear()
            raise PluginCatalogError(
                "Plugin session construction failed") from None
        self.providers = registries["provider"].freeze()
        self.searches = registries["search"].freeze()
        self.adapters = registries["adapter"].freeze()

    async def start(self):
        async with self._lifecycle_lock:
            return await self._start_locked()

    async def _start_locked(self):
        if self.state == "active":
            return self
        if self.state != "created":
            raise PluginCatalogError("Plugin session lifecycle invalid")
        self.state = "starting"
        try:
            for plugin in self._executions:
                # Include a plugin before invoking its hook so a hook that
                # allocates and then fails is still closed during rollback.
                self._started.append(plugin)
                await _lifecycle_call(plugin, "start")
        except asyncio.CancelledError:
            current = asyncio.current_task()
            caller_cancelled = bool(current and current.cancelling())
            self.state = "closing"
            await self._finish_close(suppress=True)
            if caller_cancelled:
                raise
            raise PluginCatalogError("Plugin session startup failed") from None
        except BaseException:
            self.state = "closing"
            await self._finish_close(suppress=True)
            raise PluginCatalogError("Plugin session startup failed") from None
        self.state = "active"
        return self

    async def _close_all(self, *, suppress):
        failed = False
        for plugin in reversed(self._started):
            try:
                name = ("aclose" if callable(getattr(plugin, "aclose", None))
                        else "close")
                await _lifecycle_call(plugin, name)
            except BaseException:
                failed = True
        if failed and not suppress:
            raise PluginCatalogError("Plugin session shutdown failed")

    async def _finish_close(self, *, suppress):
        cleanup = asyncio.create_task(self._close_all(suppress=suppress))
        cancellation = None
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError as caught:
                cancellation = caught
            except BaseException:
                # A completed cleanup failure is collected below after the
                # session state has been finalized.
                pass
        failure = None
        try:
            await cleanup
        except BaseException as caught:
            failure = caught
        finally:
            self.state = "closed"
        if cancellation is not None:
            raise cancellation
        if failure is not None:
            raise failure

    async def close(self):
        async with self._lifecycle_lock:
            return await self._close_locked()

    async def _close_locked(self):
        if self.state == "closed":
            return
        self.state = "closing"
        await self._finish_close(suppress=False)


@dataclass(frozen=True)
class PluginCatalog:
    """Immutable startup definitions; each Runtime opens its own session."""
    providers: ProviderRegistry
    searches: SearchRegistry
    adapters: AdapterRegistry
    snapshot: PluginCatalogSnapshot
    bindings: tuple[PluginBinding, ...] = field(repr=False)

    def inspect(self):
        return self.snapshot.inspect()

    def open_session(self):
        return PluginSession(self)


@dataclass(frozen=True)
class _EntryCandidate:
    kind: str
    plugin_id: str
    distribution: str
    distribution_version: str | None
    entry_point: object


def default_plugin_policy_path():
    """The owner's trust file: FRANKENSURF_PLUGINS, else ~/.config/frankensurf/plugins.json."""
    configured = os.environ.get("FRANKENSURF_PLUGINS")
    return Path(configured).expanduser() if configured else Path.home() / ".config/frankensurf/plugins.json"


def _canonical_distribution(value):
    if not isinstance(value, str):
        return None
    canonical = re.sub(r"[-_.]+", "-", value).lower()
    return canonical if _DISTRIBUTION.fullmatch(canonical) else None


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PluginConfigurationError("Plugin configuration schema invalid")
        result[key] = value
    return result


def _decode_policy(value):
    if (not isinstance(value, dict)
            or set(value) != {"schema", "trusted", "disabled"}
            or value.get("schema") != PLUGIN_POLICY_SCHEMA
            or not isinstance(value.get("trusted"), dict)
            or not isinstance(value.get("disabled"), dict)
            or set(value["trusted"]) != set(PLUGIN_KINDS)
            or set(value["disabled"]) != set(PLUGIN_KINDS)):
        raise PluginConfigurationError("Plugin configuration schema invalid")
    trusted = []
    disabled = set()
    for kind in PLUGIN_KINDS:
        grants = value["trusted"][kind]
        blocked = value["disabled"][kind]
        if not isinstance(grants, dict) or not isinstance(blocked, list):
            raise PluginConfigurationError("Plugin configuration schema invalid")
        for plugin_id, distribution in grants.items():
            canonical = _canonical_distribution(distribution)
            if (not isinstance(plugin_id, str) or not _ID.fullmatch(plugin_id)
                    or canonical is None or canonical != distribution):
                raise PluginConfigurationError("Plugin configuration schema invalid")
            trusted.append(PluginTrust(kind, plugin_id, distribution))
        if (any(not isinstance(plugin_id, str) or not _ID.fullmatch(plugin_id)
                for plugin_id in blocked)
                or len(set(blocked)) != len(blocked)):
            raise PluginConfigurationError("Plugin configuration schema invalid")
        disabled.update((kind, plugin_id) for plugin_id in blocked)
    trusted.sort(key=lambda item: (item.kind, item.plugin_id, item.distribution))
    return PluginPolicy(tuple(trusted), frozenset(disabled))


def load_plugin_policy(path=None):
    """Load a no-secrets policy from a private, owner-controlled JSON file."""
    path = Path(path).expanduser() if path is not None else default_plugin_policy_path()
    try:
        observed = path.lstat()
    except FileNotFoundError:
        return PluginPolicy()
    except OSError:
        raise PluginConfigurationError("Plugin configuration unavailable") from None
    if os.name != "posix":
        raise PluginConfigurationError("Plugin configuration ownership cannot be verified")
    descriptor = None
    try:
        parent = path.parent.lstat()
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
                or parent.st_mode & 0o077):
            raise PluginConfigurationError("Plugin configuration permissions invalid")
        if (not stat.S_ISREG(observed.st_mode) or observed.st_uid != os.getuid()
                or observed.st_mode & 0o077):
            raise PluginConfigurationError("Plugin configuration permissions invalid")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        current = os.fstat(descriptor)
        if ((current.st_dev, current.st_ino) != (observed.st_dev, observed.st_ino)
                or not stat.S_ISREG(current.st_mode) or current.st_uid != os.getuid()
                or current.st_mode & 0o077):
            raise PluginConfigurationError("Plugin configuration changed")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = None
            value = json.load(stream, object_pairs_hook=_unique_pairs)
    except PluginConfigurationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise PluginConfigurationError("Plugin configuration unavailable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return _decode_policy(value)


def _distribution_name(entry_point):
    distribution = getattr(entry_point, "dist", None)
    name = getattr(distribution, "name", None)
    if not isinstance(name, str):
        package_metadata = getattr(distribution, "metadata", None)
        if package_metadata is not None:
            name = package_metadata.get("Name")
    canonical = _canonical_distribution(name)
    if canonical is None:
        raise ValueError()
    return canonical


def _entry_candidate(entry_point, kind):
    plugin_id = getattr(entry_point, "name", None)
    if not isinstance(plugin_id, str) or not _ID.fullmatch(plugin_id):
        raise ValueError()
    distribution = getattr(entry_point, "dist", None)
    version = getattr(distribution, "version", None)
    if (type(version) is not str
            or _VERSION.fullmatch(version) is None):
        raise ValueError()
    return _EntryCandidate(kind, plugin_id, _distribution_name(entry_point),
                           version, entry_point)


def _installed_entry_points(source):
    failures = []
    if source is None:
        try:
            source = metadata.entry_points()
        except Exception:
            return {kind: [] for kind in PLUGIN_KINDS}, [
                PluginRejection(kind, "catalog", "DISCOVERY_UNAVAILABLE")
                for kind in PLUGIN_KINDS]
    elif callable(source):
        try:
            source = source()
        except Exception:
            return {kind: [] for kind in PLUGIN_KINDS}, [
                PluginRejection(kind, "catalog", "DISCOVERY_UNAVAILABLE")
                for kind in PLUGIN_KINDS]
    selected = {kind: [] for kind in PLUGIN_KINDS}
    if hasattr(source, "select"):
        for kind, group in ENTRY_POINT_GROUPS.items():
            try:
                selected[kind] = list(source.select(group=group))
            except Exception:
                failures.append(PluginRejection(kind, "catalog", "DISCOVERY_UNAVAILABLE"))
    elif isinstance(source, dict):
        for kind, group in ENTRY_POINT_GROUPS.items():
            try:
                selected[kind] = list(source.get(group, ()))
            except Exception:
                failures.append(PluginRejection(kind, "catalog", "DISCOVERY_UNAVAILABLE"))
    else:
        reverse = {group: kind for kind, group in ENTRY_POINT_GROUPS.items()}
        try:
            entries = list(source)
        except Exception:
            return selected, [PluginRejection(kind, "catalog", "DISCOVERY_UNAVAILABLE")
                              for kind in PLUGIN_KINDS]
        for entry_point in entries:
            try:
                kind = reverse.get(entry_point.group)
            except Exception:
                failures.append(PluginRejection("catalog", "unknown",
                                                "ENTRY_POINT_METADATA_INVALID"))
                continue
            if kind is not None:
                selected[kind].append(entry_point)
    candidates = {kind: [] for kind in PLUGIN_KINDS}
    for kind in PLUGIN_KINDS:
        for entry_point in selected[kind]:
            try:
                candidates[kind].append(_entry_candidate(entry_point, kind))
            except Exception:
                try:
                    plugin_id = getattr(entry_point, "name", None)
                except Exception:
                    plugin_id = None
                failures.append(PluginRejection(
                    kind, plugin_id if isinstance(plugin_id, str) and _ID.fullmatch(plugin_id)
                    else "unknown", "ENTRY_POINT_METADATA_INVALID"))
        candidates[kind].sort(key=lambda item: (item.plugin_id, item.distribution))
    return candidates, failures


def _materialize(candidate):
    value = candidate.entry_point.load()
    if isinstance(value, PluginFactory):
        return value
    expected = _METHOD[candidate.kind]
    if isinstance(value, type):
        value = value()
    elif not (hasattr(value, "manifest")
              and callable(getattr(value, expected, None))):
        if callable(value):
            value = value()
    return value


def _validated_external(candidate):
    value = _materialize(candidate)
    if isinstance(value, PluginFactory):
        manifest = value.manifest
    else:
        staging = _registry_for(candidate.kind)
        manifest = staging.register(value)
    if (manifest.id != candidate.plugin_id
            or not isinstance(manifest.version, str)
            or _VERSION.fullmatch(manifest.version) is None):
        raise ValueError()
    _canonical_manifest(candidate.kind, manifest)
    return _binding(
        candidate.kind, value, source="entry_point",
        trust_basis="owner_config", distribution=candidate.distribution,
        distribution_version=candidate.distribution_version)


def _registry_records(bindings):
    records = []
    indices = {kind: 0 for kind in PLUGIN_KINDS}
    for binding in bindings:
        manifest = _canonical_manifest(binding.kind, binding.manifest)
        described = dict(manifest)
        records.append(PluginRecord(
            binding.kind, binding.plugin_id, described["version"],
            binding.binding_id, indices[binding.kind], binding.enabled,
            binding.source,
            binding.trust_basis, manifest, binding.distribution,
            binding.distribution_version))
        indices[binding.kind] += 1
    return tuple(sorted(records, key=lambda item: (item.kind, item.plugin_id)))


def build_plugin_catalog(*, config_path=None, entry_points=None,
                         base_providers=None, base_searches=None,
                         base_adapters=None):
    """Build immutable definitions; Runtime sessions own execution instances."""
    policy = load_plugin_policy(config_path)
    if base_providers is None:
        from .providers import DEFAULT_PROVIDERS as base_providers
    if base_searches is None:
        from .search_plugins import DEFAULT_SEARCHES as base_searches
    if base_adapters is None:
        from .adapters import DEFAULT_ADAPTERS as base_adapters
    bases = {
        "provider": base_providers,
        "search": base_searches,
        "adapter": base_adapters,
    }
    expected = {"provider": ProviderRegistry, "search": SearchRegistry,
                "adapter": AdapterRegistry}
    if any(not isinstance(bases[kind], expected[kind])
           for kind in PLUGIN_KINDS):
        raise PluginCatalogError("Bundled plugin registry type invalid")

    bindings = []
    try:
        for kind in PLUGIN_KINDS:
            registry = bases[kind]
            for identifier, plugin in registry._plugins.items():
                binding = _binding(
                    kind, plugin,
                    enabled=(identifier not in registry._disabled
                             and not policy.is_disabled(kind, identifier)))
                if binding.plugin_id != identifier:
                    raise _UnsafePluginBinding()
                bindings.append(binding)
    except BaseException:
        raise PluginCatalogError(
            "Bundled plugin registry binding failed") from None

    candidates, rejections = _installed_entry_points(entry_points)
    occupied = {(binding.kind, binding.plugin_id) for binding in bindings}
    for kind in PLUGIN_KINDS:
        counts = {}
        for candidate in candidates[kind]:
            counts[candidate.plugin_id] = counts.get(candidate.plugin_id, 0) + 1
        for candidate in candidates[kind]:
            key = (kind, candidate.plugin_id)
            if counts[candidate.plugin_id] > 1 or key in occupied:
                rejections.append(PluginRejection(
                    kind, candidate.plugin_id, "DUPLICATE_PLUGIN_ID",
                    candidate.distribution))
                continue
            if policy.is_disabled(kind, candidate.plugin_id):
                rejections.append(PluginRejection(
                    kind, candidate.plugin_id, "PLUGIN_DISABLED",
                    candidate.distribution))
                continue
            if (policy.trusted_distribution(kind, candidate.plugin_id)
                    != candidate.distribution):
                rejections.append(PluginRejection(
                    kind, candidate.plugin_id, "PLUGIN_NOT_TRUSTED",
                    candidate.distribution))
                continue
            try:
                binding = _validated_external(candidate)
            except _UnsafePluginBinding:
                rejections.append(PluginRejection(
                    kind, candidate.plugin_id, "PLUGIN_BINDING_UNSAFE",
                    candidate.distribution))
                continue
            except BaseException:
                rejections.append(PluginRejection(
                    kind, candidate.plugin_id, "PLUGIN_LOAD_REJECTED",
                    candidate.distribution))
                continue
            bindings.append(binding)
            occupied.add(key)

    # Lifecycle dependencies are kind-level: searches may consume providers,
    # and adapters consume acquired responses. Preserve registration order
    # within each kind while starting every provider before those dependants.
    bindings = [binding for kind in PLUGIN_KINDS
                for binding in bindings if binding.kind == kind]
    registries = _definition_registries(bindings)
    records = _registry_records(bindings)
    rejections = tuple(sorted(
        rejections,
        key=lambda item: (item.kind, item.plugin_id,
                          item.distribution or "", item.code)))
    snapshot = PluginCatalogSnapshot(
        PLUGIN_SNAPSHOT_SCHEMA, records, rejections)
    return PluginCatalog(
        registries["provider"], registries["search"],
        registries["adapter"], snapshot, tuple(bindings))
