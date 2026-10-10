"""Third-party plugins from the owner's side: the example package loads only once
trusted, `frankensurf plugins` says why one was turned away, and the trust file
stays private."""
import json
import stat
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from frankensurf import Runtime, plugin_admin
from frankensurf.plugin_catalog import ENTRY_POINT_GROUPS, build_plugin_catalog, load_plugin_policy

EXAMPLE = Path(__file__).resolve().parents[1] / "examples/frankensurf-plugin-example/src"
PAGE = "<html><head><title>Lamp</title></head><body>" + "<p>A brass desk lamp, $49.</p>" * 20 + "</body></html>"


class EntryPoint:
    def __init__(self, name, value, distribution="frankensurf-plugin-example"):
        self.group = ENTRY_POINT_GROUPS["provider"]
        self.name = name
        self.dist = SimpleNamespace(name=distribution, version="0.1.0")
        self.value = value

    def load(self):
        return self.value


@pytest.fixture
def example(monkeypatch):
    monkeypatch.syspath_prepend(str(EXAMPLE))
    sys.modules.pop("frankensurf_plugin_example", None)
    import frankensurf_plugin_example
    return [EntryPoint("example_fetch", frankensurf_plugin_example.ExampleFetch)]


@pytest.fixture
def service(monkeypatch):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append((self.headers.get("Authorization"),
                         json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            body = json.dumps({"status": 200, "url": seen[-1][1]["url"], "html": PAGE}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("EXAMPLE_API_URL", f"http://127.0.0.1:{server.server_port}/read")
    monkeypatch.setenv("EXAMPLE_API_KEY", "test-key")
    yield seen
    server.shutdown()


async def test_the_example_plugin_runs_only_once_trusted(tmp_path, example, service):
    trust = tmp_path / "config" / "plugins.json"
    before = plugin_admin.status(trust, entry_points=example)
    assert before["loaded"] == [] and before["rejected"][0]["code"] == "PLUGIN_NOT_TRUSTED"
    assert before["rejected"][0]["hint"].endswith(
        "frankensurf plugins trust provider example_fetch frankensurf-plugin-example")

    plugin_admin.change("trust", "provider", "example_fetch", "frankensurf-plugin-example", path=trust)
    assert stat.S_IMODE(trust.stat().st_mode) == 0o600 and stat.S_IMODE(trust.parent.stat().st_mode) == 0o700
    after = plugin_admin.status(trust, entry_points=example)
    assert [item["id"] for item in after["loaded"]] == ["example_fetch"] and after["rejected"] == []

    catalog = build_plugin_catalog(config_path=trust, entry_points=example)
    async with Runtime(tmp_path / "state", plugin_catalog=catalog) as web:
        result = await web.read("https://shop.example.com/lamp", policy_overrides={
            "provider": "example_fetch", "allow_paid_fallbacks": True})
    receipt = result["receipt"]
    assert receipt["status"] == "observed" and receipt["method"] == "example_fetch"
    assert "brass desk lamp" in result["text"] and receipt["cost_usd"] == 0.001
    assert service == [("Bearer test-key", {"url": "https://shop.example.com/lamp"})]


def test_trust_file_edits_round_trip_and_refuse_bad_input(tmp_path):
    trust = tmp_path / "config" / "plugins.json"
    plugin_admin.change("trust", "search", "my_search", "My_Search.Pkg", path=trust)
    plugin_admin.change("disable", "provider", "zenrows", path=trust)
    policy = load_plugin_policy(trust)
    assert policy.trusted_distribution("search", "my_search") == "my-search-pkg"
    assert policy.is_disabled("provider", "zenrows")
    plugin_admin.change("enable", "provider", "zenrows", path=trust)
    plugin_admin.change("untrust", "search", "my_search", path=trust)
    policy = load_plugin_policy(trust)
    assert not policy.is_disabled("provider", "zenrows") and policy.trusted_distribution("search", "my_search") is None
    for bad in (("trust", "widget", "x", "pkg"), ("trust", "provider", "Bad-ID", "pkg"),
                ("trust", "provider", "ok_id", None), ("remove", "provider", "ok_id")):
        with pytest.raises(ValueError):
            plugin_admin.change(*bad, path=trust)


def test_cli_plugins_arguments():
    from frankensurf.cli import parse_args
    assert parse_args(["plugins"]).urls == []
    assert parse_args(["plugins", "trust", "provider", "acme", "frankensurf-acme"]).urls[0] == "trust"
    args = parse_args(["read", "https://example.com", "--paid", "--max-cost", "0.05"])
    assert args.paid is True and args.max_cost == 0.05
    for bad in (["plugins", "trust", "provider", "acme"], ["plugins", "wipe"]):
        with pytest.raises(SystemExit):
            parse_args(bad)
