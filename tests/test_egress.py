"""More than one connection: a walled read tries the owner's other connections, and remembers the one that worked."""
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from frankensurf import providers
from frankensurf.providers import ProviderManifest, ProviderRegistry
from frankensurf.runtime import Runtime, WebFailure, WebPolicy

PAGE = "<html><title>Lamp</title><body>" + "<p>A brass lamp, $49, ships in two days.</p>" * 30 + "</body></html>"
QUIET = {"origin_route_hint_ttl_seconds": 86400, "origin_min_interval_seconds": 0, "origin_cooldown_seconds": 0,
         "warm_up_on_wall": False, "hedge_after_seconds": 0, "second_opinion_text_chars": 0,
         "completeness_escalation": False, "provider_max_attempts_per_candidate": 1}


def test_the_pool_names_connections_and_refuses_bad_entries(monkeypatch):
    from frankensurf.egress import pool
    monkeypatch.setenv("FRANKENSURF_EGRESS", "vps=socks5://127.0.0.1:1080,\n http://user:pw@10.0.0.2:8080")
    entries = pool()
    assert [entry["name"] for entry in entries] == ["vps", "egress2"]
    assert entries[1]["username"] == "user" and entries[1]["server"] == "http://10.0.0.2:8080"
    for bad in ("vps=ftp://host:21", "direct=http://h:1", "a=http://h:1,a=http://h:2"):
        monkeypatch.setenv("FRANKENSURF_EGRESS", bad)
        with pytest.raises(ValueError):
            pool()
    monkeypatch.delenv("FRANKENSURF_EGRESS")
    assert pool() == []


def far_only(identifier, calls):
    """Walled on every connection but the one named "far"."""
    class Plugin:
        def __init__(self):
            self.manifest = ProviderManifest(identifier, "1", rendering=identifier != "http")

        def available(self, configured):
            return True

        async def acquire(self, request, services):
            from frankensurf.egress import CURRENT
            egress = (CURRENT.get() or {}).get("name", "direct")
            calls.append((identifier, egress))
            if egress == "far":
                return {"url": request.url, "content": PAGE, "content_type": "text/html", "http_status": 200}
            failure = WebFailure("BLOCKED", "fixture wall")
            failure.wall_vendor = "datadome"
            raise failure
    return Plugin()


async def test_a_walled_read_tries_the_other_connections_and_remembers_the_one_that_worked(tmp_path, monkeypatch):
    monkeypatch.setenv("FRANKENSURF_EGRESS", "near=http://127.0.0.1:9, far=http://127.0.0.1:10")
    calls = []
    registry = ProviderRegistry()
    for identifier in ("http", "camoufox"):
        registry.register(far_only(identifier, calls))
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    url = "https://walled.example.com/item/lamp-123456"
    async with Runtime(tmp_path) as web:
        first = await web.read(url, policy_overrides=QUIET)
        first_calls = list(calls)
        calls.clear()
        again = await web.read(url, policy_overrides=QUIET)
        off = await web.read(url + "?x", policy_overrides={**QUIET, "use_egress_pool": False})
    assert first["receipt"]["status"] == "observed"
    record = first["receipt"]["egress"]
    assert {key: record[key] for key in ("used", "tried", "after")} == {
        "used": "far", "tried": ["near", "far"], "after": "BLOCKED"}
    # The walled direct read is linked, and the trace names the connection too.
    assert record["direct_trace_id"] and record["direct_trace_id"] != first["receipt"]["trace_id"]
    # The direct connection first, then each connection with the vendor's best tool first.
    assert first_calls[:2] == [("http", "direct"), ("camoufox", "direct")]
    assert ("camoufox", "near") in first_calls and first_calls[-1] == ("camoufox", "far")
    # The next read of the site starts on the connection that worked.
    assert again["receipt"]["egress"] == {"used": "far", "from_hint": True}
    assert calls[0] == ("camoufox", "far")
    assert off["receipt"]["status"] == "failed" and "egress" not in off["receipt"]
    # Receipts name the connection, never its address.
    assert "127.0.0.1:10" not in str(first["receipt"])


# --- End to end: plain HTTP through a real local proxy -------------------------
class _Site(BaseHTTPRequestHandler):
    def do_GET(self):
        through = self.headers.get("X-Through-Proxy") == "1"
        body = (PAGE if through else "<html><title>Access denied</title><body>Access denied</body></html>").encode()
        self.send_response(200 if through else 403)
        self.send_header("content-type", "text/html")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _Proxy(BaseHTTPRequestHandler):
    """A forward proxy for plain HTTP that marks what it forwards."""
    def do_GET(self):
        request = urllib.request.Request(self.path, headers={"X-Through-Proxy": "1"})
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read()
            self.send_response(response.status)
            self.send_header("content-type", response.headers.get("content-type", "text/html"))
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *args):
        pass


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


async def test_plain_http_goes_through_the_connection(tmp_path, monkeypatch):
    site, proxy = _serve(_Site), _serve(_Proxy)
    try:
        monkeypatch.setenv("FRANKENSURF_EGRESS", f"lab=http://127.0.0.1:{proxy.server_port}")
        others = [item["id"] for item in providers.DEFAULT_PROVIDERS.inspect() if item["id"] != "http"]
        async with Runtime(tmp_path) as web:
            result = await web.read(f"http://127.0.0.1:{site.server_port}/item/lamp-123456",
                                    policy_overrides={**QUIET, "exclude_providers": others,
                                                      "egress_tools": ["http"]})
    finally:
        site.shutdown()
        proxy.shutdown()
    receipt = result["receipt"]
    assert receipt["status"] == "observed", receipt.get("failure")
    assert receipt["egress"]["used"] == "lab" and "ships in two days" in result["text"]
    with pytest.raises(ValueError):
        WebPolicy(egress_tools=("http", "http"))


async def test_tools_that_cannot_carry_a_connection_never_make_a_pass(tmp_path, monkeypatch):
    monkeypatch.setenv("FRANKENSURF_EGRESS", "far=http://127.0.0.1:10")
    calls = []
    registry = ProviderRegistry()
    for identifier in ("http", "camoufox", "local"):
        registry.register(far_only(identifier, calls))
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    async with Runtime(tmp_path) as web:
        result = await web.read("https://walled.example.com/item/lamp-1",
                                policy_overrides={**QUIET, "egress_tools": ["local", "camoufox"]})
    # "local" takes its proxy when it starts, so it is left out of the pass.
    assert ("local", "far") not in calls and ("camoufox", "far") in calls
    assert result["receipt"]["egress"]["used"] == "far"


async def test_a_site_cooling_down_still_gets_its_known_connection(tmp_path, monkeypatch):
    monkeypatch.setenv("FRANKENSURF_EGRESS", "near=http://127.0.0.1:9, far=http://127.0.0.1:10")
    monkeypatch.setattr(Runtime, "pacing_enabled", True)
    calls = []
    registry = ProviderRegistry()
    for identifier in ("http", "camoufox"):
        registry.register(far_only(identifier, calls))
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    url = "https://walled.example.com/item/lamp-2"
    cooling = {**QUIET, "origin_cooldown_seconds": 900}
    async with Runtime(tmp_path) as web:
        await web.read(url, policy_overrides=cooling)  # Through "far"; the hint is kept.
        await web.read(url, policy_overrides={**cooling, "use_egress_pool": False})  # Walled: cool-down.
        calls.clear()
        again = await web.read(url, policy_overrides=cooling)
    assert again["receipt"]["status"] == "observed" and again["receipt"]["egress"]["from_hint"] is True
    assert calls and all(egress == "far" for _tool, egress in calls)



class _Trace(BaseHTTPRequestHandler):
    def do_GET(self):
        through = self.headers.get("X-Through-Proxy") == "1"
        body = f"ip={'203.0.113.9' if through else '198.51.100.7'}\nloc={'NZ' if through else 'AU'}\nwarp=off\n".encode()
        self.send_response(200)
        self.send_header("content-type", "text/plain")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


async def test_egress_check_reports_where_each_connection_comes_out(monkeypatch):
    from frankensurf.egress import check
    trace, proxy = _serve(_Trace), _serve(_Proxy)
    try:
        rows = await check([{"name": "lab", "url": f"http://127.0.0.1:{proxy.server_port}"},
                            {"name": "dead", "url": "http://127.0.0.1:9"}],
                           url=f"http://127.0.0.1:{trace.server_port}/cdn-cgi/trace", timeout=5)
    finally:
        trace.shutdown()
        proxy.shutdown()
    by_name = {row["name"]: row for row in rows}
    assert by_name["direct"]["country"] == "AU" and by_name["lab"]["country"] == "NZ"
    assert by_name["lab"]["ip"] == "203.0.113.9" and by_name["dead"]["ok"] is False
    assert "127.0.0.1:9" not in str(rows)  # never a connection's address
