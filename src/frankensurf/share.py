"""Shared site knowledge, opt in both ways.

    frankensurf share export [--out FILE]   what your reads learned, as route recipes to review and share
    frankensurf share sync                  fetch the community recipes into your state folder
    frankensurf share remove                stop using them

export turns route memory (route-observations.jsonl) into public route recipes: for each site, the
tool that read it reliably when cheaper tools did not. Only public reads count (nothing read with a
profile or identity), and only sites with enough reads (min_samples, default 3) where one tool
succeeded at least min_rate of the time (default 80%). Paths are cut to their first segment, or to
the whole site when even that looks item-specific, so an export says which sites need which tool,
not which pages you read. Nothing is sent anywhere: the file is yours to review and contribute
(a pull request adding it to community/recipes/ in the repository).

sync downloads community/routes.json from the repository, checks every recipe (known shape, a
community_ id, single pages only, no authority fields: the same rules as bundled seeds), and saves it
as a private file beside your own recipes. Community recipes are provisional seeds, exactly like the
bundled compatibility seeds: they order the first attempts for a site, never grant paid tools or
identities, and your own recipes and route memory override them. A community recipe for a site that a
bundled recipe already covers is ignored.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

COMMUNITY_URL = "https://raw.githubusercontent.com/yail259/frankensurf/main/community/routes.json"
FILE_NAME = "community_routes.json"
SCHEMA = "frankensurf.public-route-recipes/v1"
# The first rung every read tries anyway: a recipe for it teaches nothing.
_DEFAULT_FIRST = {"http"}
_SEGMENT = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,29}")
_TRANSPORT = None   # tests inject an httpx.MockTransport


def community_path(state_dir) -> Path:
    return Path(state_dir).expanduser() / "routes" / FILE_NAME


def _general_pattern(path_pattern) -> str:
    """A path pattern cut to its first segment, or the whole site."""
    first = str(path_pattern or "").lstrip("^").split("/")[1:2]
    segment = first[0] if first else ""
    if segment and _SEGMENT.fullmatch(segment) and "{" not in segment and not re.search(r"\d{3,}", segment):
        return "/" + re.escape(segment) + "(?:/.*)?"
    return ".*"


def _slug(text) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:60] or "site"


def export(state_dir, *, min_samples: int = 3, min_rate: float = 0.8) -> dict:
    """Route recipes from this machine's route memory; see the module docstring."""
    from . import route_memory
    from .providers import DEFAULT_PROVIDERS
    versions = {item["id"]: item["version"] for item in DEFAULT_PROVIDERS.inspect()}
    groups = route_memory.capability_summary(Path(state_dir).expanduser())["groups"]
    best: dict[tuple, dict] = {}
    for group in groups:
        if (group.get("identity_class") not in (None, "public") or group.get("operation") != "read"
                or group.get("adapter") is not None or not group.get("domain")
                or group.get("provider") not in versions or group.get("provider") in _DEFAULT_FIRST):
            continue
        samples, observed = group["sample_count"], group["observed_count"]
        if samples < min_samples or observed / samples < min_rate:
            continue
        key = (group["domain"].lower(), _general_pattern(group.get("path_pattern")))
        merged = best.setdefault(key, {})
        stats = merged.setdefault(group["provider"], [0, 0])
        stats[0] += samples
        stats[1] += observed
    recipes = []
    for (domain, pattern), providers in sorted(best.items()):
        provider, (samples, observed) = max(providers.items(), key=lambda kv: (kv[1][1] / kv[1][0], kv[1][0]))
        origin = "https://" + domain
        recipes.append({"id": "community_" + _slug(domain) + ("" if pattern == ".*" else "_" + _slug(pattern)),
                        "version": "1", "origin": origin, "path_pattern": pattern, "operation": "read",
                        "provider": provider, "provider_version": versions[provider], "policy_defaults": {},
                        "provenance": {}, "enabled": True, "representation": "single_page",
                        "evidence": {"reads": samples, "succeeded": observed}})
    return {"schema": SCHEMA, "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "recipes": recipes}


def _strip(document) -> dict:
    """The recipe document the registry reads: schema and recipes, recipe fields only."""
    if not isinstance(document, dict) or document.get("schema") != SCHEMA or not isinstance(document.get("recipes"), list):
        raise ValueError("not a Frankensurf route recipe document")
    recipes = []
    for row in document["recipes"]:
        if not isinstance(row, dict):
            raise ValueError("a recipe must be an object")
        row = {key: value for key, value in row.items() if key != "evidence"}
        if (not str(row.get("id", "")).startswith("community_") or row.get("representation", "single_page") != "single_page"
                or row.get("operation") != "read"):
            raise ValueError("community recipes are single-page reads with a community_ id: " + str(row.get("id"))[:80])
        recipes.append(row)
    return {"schema": SCHEMA, "recipes": recipes}


def validate(document) -> dict:
    """The stripped document, checked by the same rules the route registry applies."""
    from .routes import RouteRecipeRegistry, _COMMUNITY_BASIS
    stripped = _strip(document)
    RouteRecipeRegistry._decode(stripped, _COMMUNITY_BASIS)
    return stripped


def sync(state_dir, url: str = COMMUNITY_URL) -> dict:
    """Download, check and save the community recipes; see the module docstring."""
    import httpx
    with httpx.Client(transport=_TRANSPORT, timeout=30.0, follow_redirects=True) as client:
        response = client.get(url)
        response.raise_for_status()
        document = validate(response.json())
    path = community_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".community-routes-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(document, stream, sort_keys=True)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {"saved": str(path), "recipes": len(document["recipes"]), "source": url}


def remove(state_dir) -> dict:
    path = community_path(state_dir)
    existed = path.exists()
    path.unlink(missing_ok=True)
    return {"removed": str(path) if existed else None}
