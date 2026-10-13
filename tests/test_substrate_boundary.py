"""FrankenSurf is the substrate: it provides access, not answers.

Site-specific modules were removed in v0.7.0. Site knowledge belongs in callers
or in per-site recipe data (route seeds), never in Python modules here. A new
core module must be added to CORE on purpose.
"""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "frankensurf"

CORE = frozenset({
    "__init__", "actions", "adapters", "bot_auth",
    "browser_use_action_provider", "browser_use_action_worker", "browser_use_binding",
    "browser_use_config", "browser_use_provider", "browser_use_worker", "cli", "completeness",
    "browser_install", "crawl4ai_config", "crawl4ai_preflight", "crawl4ai_provider", "crawl4ai_worker", "egress",
    "experimental", "installer",
    "handoff", "hosted_providers", "identity",
    "identity_snapshots", "interactions", "local_search", "main_content", "managed_browsers", "site_feeds", "mcp_server", "module_discovery", "pagination", "plugin_admin", "plugin_catalog", "profiles",
    "provider_worker", "providers", "public_entry", "real_browser", "recovery", "repair",
    "route_memory", "routes", "runtime", "scrapling_ready", "search", "search_checks", "search_plugins",
    "site_watch",
    "site_modules", "web_do", "schema_extract", "agent_tools", "share",
})

SITE_WORDS = re.compile(r"ebay|gumtree|depop|reverb|carsales|bikesales|cashconverters|grays|allbids|lloyds|pickles"
                        r"|manheim|slattery|salvos|vinnies|tradingpost|machines4u|carsguide|autotrader|reebelo|cex"
                        r"|backmarket|etsy|bidsonline|ritchie|shopify|facebook_marketplace")


def test_no_new_modules_outside_core():
    modules = {path.stem for path in SRC.glob("*.py")}
    unexpected = modules - CORE
    assert not unexpected, f"New modules must be substrate code (add to CORE deliberately): {sorted(unexpected)}"


def test_no_site_named_modules():
    assert not [path.name for path in SRC.glob("*.py") if SITE_WORDS.search(path.stem)]


def test_no_site_modules_ship_with_frankensurf(tmp_path):
    # Site modules are the operator's data: no bundled layer, no packaged module files.
    from frankensurf.site_modules import SiteModuleRegistry
    assert SiteModuleRegistry(tmp_path / "site-modules.json").inspect() == []
    packaged = [path.name for path in SRC.rglob("*") if "module" in path.name and path.suffix == ".json"]
    assert packaged == []


def test_only_generic_adapters_are_registered():
    from frankensurf.adapters import DEFAULT_ADAPTERS
    assert {item["id"] for item in DEFAULT_ADAPTERS.inspect()} == {"html", "json", "rss"}
