"""The owner's side of third-party plugins: what loaded, what was turned away
and why, and editing the private trust file (plugins.json) that decides it.

Installing a package never makes it run. A plugin runs only after its owner
trusts that plugin ID from that exact distribution here."""
import json
import os
import tempfile
from pathlib import Path

from .plugin_catalog import (PLUGIN_KINDS, PLUGIN_POLICY_SCHEMA, PluginConfigurationError,
                             _canonical_distribution, _ID, build_plugin_catalog,
                             default_plugin_policy_path, load_plugin_policy)

HINTS = {
    "PLUGIN_NOT_TRUSTED": "installed but not trusted yet: frankensurf plugins trust {kind} {id} {distribution}",
    "PLUGIN_DISABLED": "turned off in the trust file: frankensurf plugins enable {kind} {id}",
    "DUPLICATE_PLUGIN_ID": "another plugin (or a bundled one) already uses this ID; rename it in the package",
    "PLUGIN_LOAD_REJECTED": "trusted, but importing it or reading its manifest failed; check its manifest ID matches",
    "PLUGIN_BINDING_UNSAFE": ("trusted, but its methods read mutable module-level values (a dict, list or "
                              "object); keep state inside the method or the class"),
    "ENTRY_POINT_METADATA_INVALID": "its entry point name or package version is malformed",
    "DISCOVERY_UNAVAILABLE": "Python could not list installed entry points",
}


def _path(path=None):
    return Path(path).expanduser() if path is not None else default_plugin_policy_path()


def _require_posix():
    if os.name != "posix":
        raise PluginConfigurationError(
            "Third-party plugins need Linux, macOS or WSL: the trust file's owner and permissions "
            "cannot be checked on Windows")


def status(path=None, *, entry_points=None):
    """External plugins that loaded, and every one that was turned away with a hint."""
    path = _path(path)
    catalog = build_plugin_catalog(config_path=path, entry_points=entry_points)
    view = catalog.inspect()
    loaded = [{key: item[key] for key in ("kind", "id", "version", "enabled", "distribution",
                                          "distribution_version")}
              for item in view["plugins"] if item["source"] == "entry_point"]
    rejected = [{**item, "hint": HINTS.get(item["code"], "").format(
        kind=item["kind"], id=item["id"], distribution=item.get("distribution") or "DISTRIBUTION")}
        for item in view["rejected"]]
    return {"trust_file": str(path), "trust_file_exists": path.exists(), "loaded": loaded,
            "rejected": rejected,
            "bundled": sorted(item["id"] for item in view["plugins"]
                              if item["source"] != "entry_point" and item["kind"] == "provider")}


def _load_raw(path):
    if not path.exists():
        return {"schema": PLUGIN_POLICY_SCHEMA, "trusted": {kind: {} for kind in PLUGIN_KINDS},
                "disabled": {kind: [] for kind in PLUGIN_KINDS}}
    load_plugin_policy(path)  # refuses a file with loose permissions or a bad schema
    return json.loads(path.read_text(encoding="utf-8"))


def _save(path, value):
    """Write the trust file privately (directory 0700, file 0600), atomically,
    then read it back through the same checks Frankensurf applies at startup."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".plugins-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=1, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise
    load_plugin_policy(path)


def _check(kind, plugin_id):
    if kind not in PLUGIN_KINDS:
        raise ValueError(f"kind must be one of {', '.join(PLUGIN_KINDS)}")
    if not isinstance(plugin_id, str) or not _ID.fullmatch(plugin_id):
        raise ValueError("plugin ID must be lowercase letters, digits and underscores")


def change(action, kind, plugin_id, distribution=None, path=None):
    """trust / untrust / disable / enable one plugin; returns the new trust file."""
    _require_posix()
    _check(kind, plugin_id)
    path = _path(path)
    value = _load_raw(path)
    trusted, disabled = value["trusted"][kind], value["disabled"][kind]
    if action == "trust":
        canonical = _canonical_distribution(distribution)
        if canonical is None:
            raise ValueError("trust needs the package's distribution name, as pip shows it")
        trusted[plugin_id] = canonical
        if plugin_id in disabled:
            disabled.remove(plugin_id)
    elif action == "untrust":
        trusted.pop(plugin_id, None)
    elif action == "disable":
        if plugin_id not in disabled:
            disabled.append(plugin_id)
    elif action == "enable":
        if plugin_id in disabled:
            disabled.remove(plugin_id)
    else:
        raise ValueError("action must be trust, untrust, disable or enable")
    disabled.sort()
    _save(path, value)
    return value
