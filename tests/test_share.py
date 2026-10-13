"""Shared site knowledge (share.py): export what reads learned, sync the community's, both opt in.
Synthetic route memory and a mocked download; nothing touches the network."""
import json
import stat
from pathlib import Path

import httpx
import pytest

from frankensurf import route_memory, share
from frankensurf.routes import RouteRecipeRegistry


def group(domain, provider, samples, observed, path="/search", identity="public", operation="read"):
    return {"domain": domain, "path_pattern": path, "operation": operation, "adapter": None,
            "identity_class": identity, "provider": provider, "sample_count": samples, "observed_count": observed}


def test_paths_are_cut_to_their_first_segment_or_the_whole_site():
    assert share._general_pattern("/search") == "/search(?:/.*)?"
    assert share._general_pattern("/sch/i.html") == "/sch(?:/.*)?"
    assert share._general_pattern("/pdp/{number}-cat-d{number}-crawler") == "/pdp(?:/.*)?"
    assert share._general_pattern("/{number}-blue-desk-lamp") == ".*"
    assert share._general_pattern("/2026/10/story") == ".*"
    assert share._general_pattern(None) == ".*"


def test_export_keeps_reliable_public_reads_by_tools_beyond_the_first_rung(monkeypatch, tmp_path):
    monkeypatch.setattr(route_memory, "capability_summary", lambda state: {"groups": [
        group("shop.example.com", "camoufox", 5, 5),
        group("shop.example.com", "scrapling", 4, 2),                           # unreliable: out
        group("news.example.com", "http", 9, 9),                                # first rung: teaches nothing
        group("bank.example.com", "camoufox", 6, 6, identity="profile:me"),     # signed in: never shared
        group("rare.example.com", "camoufox", 2, 2),                            # too few reads
        group("docs.example.com", "jina_reader", 4, 4, path="/{number}-guide"),
    ]})
    document = share.export(tmp_path)
    recipes = {recipe["origin"]: recipe for recipe in document["recipes"]}
    assert set(recipes) == {"https://shop.example.com", "https://docs.example.com"}
    shop = recipes["https://shop.example.com"]
    assert shop["provider"] == "camoufox" and shop["path_pattern"] == "/search(?:/.*)?"
    assert shop["id"].startswith("community_shop_example_com") and shop["evidence"] == {"reads": 5, "succeeded": 5}
    assert recipes["https://docs.example.com"]["path_pattern"] == ".*"
    assert share.validate(document)["recipes"][0]["id"] == document["recipes"][0]["id"]   # exportable as is


def recipe(**changes):
    return {"id": "community_shop_example_com", "version": "1", "origin": "https://shop.example.com",
            "path_pattern": ".*", "operation": "read", "provider": "camoufox", "provider_version": "1",
            "policy_defaults": {}, "provenance": {}, "enabled": True, "representation": "single_page", **changes}


def test_validation_applies_the_bundled_seed_rules():
    ok = share.validate({"schema": share.SCHEMA, "recipes": [recipe(evidence={"reads": 3})]})
    assert "evidence" not in ok["recipes"][0]
    for bad in (recipe(id="legacy_shop_read"), recipe(operation="paginate", representation="native_sequence"),
                recipe(policy_defaults={"allow_paid_fallbacks": True}),
                recipe(policy_defaults={"provider_candidates": ["firecrawl"]}),
                recipe(origin="http://shop.example.com"), recipe(origin="https://shop.example.com/path")):
        with pytest.raises(ValueError):
            share.validate({"schema": share.SCHEMA, "recipes": [bad]})
    with pytest.raises(ValueError):
        share.validate({"recipes": []})


def test_sync_saves_a_private_checked_file_and_remove_deletes_it(monkeypatch, tmp_path):
    body = {"schema": share.SCHEMA, "recipes": [recipe()]}
    monkeypatch.setattr(share, "_TRANSPORT", httpx.MockTransport(lambda request: httpx.Response(200, json=body)))
    result = share.sync(tmp_path)
    path = share.community_path(tmp_path)
    assert result["recipes"] == 1 and Path(result["saved"]) == path
    assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0
    assert share.remove(tmp_path)["removed"] == str(path) and not path.exists()


def test_sync_refuses_a_bad_document(monkeypatch, tmp_path):
    body = {"schema": share.SCHEMA, "recipes": [recipe(policy_defaults={"identity": "me"})]}
    monkeypatch.setattr(share, "_TRANSPORT", httpx.MockTransport(lambda request: httpx.Response(200, json=body)))
    with pytest.raises(ValueError):
        share.sync(tmp_path)
    assert not share.community_path(tmp_path).exists()


def test_the_registry_uses_community_seeds_only_for_sites_nothing_else_covers(tmp_path):
    registry = RouteRecipeRegistry(tmp_path / "routes" / "recipes.json")
    packaged = registry._load_bundled() + registry._load_compatibility()
    covered = packaged[0].origin if packaged else None
    path = share.community_path(tmp_path)
    path.parent.mkdir(parents=True)
    rows = [recipe()]
    if covered:
        rows.append(recipe(id="community_covered", origin=covered))
    path.write_text(json.dumps({"schema": share.SCHEMA, "recipes": rows}))
    ids = {item["id"]: item for item in registry.inspect()}
    assert ids["community_shop_example_com"]["basis"] == "community provisional seed"
    assert "community_covered" not in ids
    path.write_text("not json")
    assert "community_shop_example_com" not in {item["id"] for item in registry.inspect()}   # ignored, not fatal


def test_the_repository_community_files_are_valid():
    root = Path(__file__).resolve().parents[1] / "community"
    share.validate(json.loads((root / "routes.json").read_text()))
    for path in sorted((root / "recipes").glob("*.json")):
        share.validate(json.loads(path.read_text()))


def test_cli_share_arguments():
    from frankensurf.cli import parse_args
    assert parse_args(["share", "export", "--out", "x.json"]).out == "x.json"
    for bad in (["share"], ["share", "publish"], ["share", "sync", "now"]):
        with pytest.raises(SystemExit):
            parse_args(bad)
