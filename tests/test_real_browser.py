"""The real-browser fallback: opt-in, ahead after walls, waits out a challenge that clears itself."""
import glob
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from frankensurf import providers
from frankensurf.providers import ProviderManifest, ProviderRegistry
from frankensurf.runtime import Runtime, WebFailure, WebPolicy

URL = "https://walled.example.com/item/lamp-123456"
PAGE = "<html><title>Lamp</title><body>" + "<p>A brass lamp, $49, ships in two days.</p>" * 30 + "</body></html>"


def provider(identifier, outcome, *, scope=False, paid=False):
    class Plugin:
        manifest = ProviderManifest(identifier, "1", rendering=True, paid=paid, route_scope_required=scope)

        def available(self, configured):
            return True

        async def acquire(self, request, services):
            if outcome != "ok":
                raise WebFailure(outcome, "fixture outcome")
            return {"url": request.url, "content": PAGE, "content_type": "text/html", "http_status": 200}
    return Plugin()


@pytest.fixture
def registry(monkeypatch):
    def install(*plugins):
        registry = ProviderRegistry()
        for plugin in plugins:
            registry.register(plugin)
        monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    return install


POLICY = dict(origin_route_hint_ttl_seconds=0, provider_max_attempts_per_candidate=1,
              warm_up_on_wall=False, hedge_after_seconds=0, origin_min_interval_seconds=0,
              origin_cooldown_seconds=0)


def tried(result):
    return [attempt["provider"] for attempt in result["receipt"]["attempts"]]


async def test_the_real_browser_is_opt_in_and_moves_ahead_after_walls(tmp_path, registry, monkeypatch):
    monkeypatch.delenv("FRANKENSURF_REAL_BROWSER", raising=False)
    registry(provider("w1", "BLOCKED"), provider("w2", "CAPTCHA"), provider("w3", "BLOCKED"),
             provider("free_ok", "ok"), provider("real_browser", "ok", scope=True))
    async with Runtime(tmp_path) as web:
        off = await web.read(URL, WebPolicy(escalate_after_walls=2, **POLICY))
        on = await web.read(URL, WebPolicy(escalate_after_walls=2, allow_real_browser=True, **POLICY))
    assert "real_browser" not in tried(off) and tried(off)[-1] == "free_ok"
    assert tried(on) == ["w1", "w2", "real_browser"]
    assert on["receipt"]["routing"]["escalated_to"] == "real_browser"


async def test_the_real_browser_goes_before_paid_tools_and_can_be_switched_on_by_environment(
        tmp_path, registry, monkeypatch):
    monkeypatch.setenv("FRANKENSURF_REAL_BROWSER", "1")
    registry(provider("w1", "BLOCKED"), provider("w2", "BLOCKED"), provider("paid_ok", "ok", paid=True),
             provider("real_browser", "CAPTCHA", scope=True))
    async with Runtime(tmp_path) as web:
        result = await web.read(URL, policy_overrides=dict(escalate_after_walls=2, allow_paid_fallbacks=True,
                                                           **POLICY))
        # The switch is a default: a call that says no wins, and a whole
        # WebPolicy fixes every field itself.
        declined = await web.read(URL + "?b", policy_overrides=dict(escalate_after_walls=2, allow_real_browser=False,
                                                                     **POLICY))
        whole = await web.read(URL + "?c", WebPolicy(escalate_after_walls=2, **POLICY))
    # Paid tools move ahead at the first wall now; the real browser still goes first.
    assert tried(result) == ["w1", "real_browser", "paid_ok"]
    assert result["receipt"]["status"] == "observed"
    assert "real_browser" not in tried(declined) and "real_browser" not in tried(whole)


async def test_a_site_cooling_down_after_walls_still_gets_the_real_browser(tmp_path, registry, monkeypatch):
    monkeypatch.delenv("FRANKENSURF_REAL_BROWSER", raising=False)
    monkeypatch.setattr(Runtime, "pacing_enabled", True)
    registry(provider("w1", "BLOCKED"), provider("real_browser", "ok", scope=True))
    cooling = {**POLICY, "origin_cooldown_seconds": 900}
    async with Runtime(tmp_path) as web:
        first = await web.read(URL, policy_overrides=cooling)
        refused = await web.read(URL, policy_overrides=cooling)
        retried = await web.read(URL, policy_overrides={**cooling, "allow_real_browser": True})
    assert first["receipt"]["failure"]["code"] == "BLOCKED"
    assert tried(refused) == [] and refused["receipt"]["failure"]["code"] == "BLOCKED"
    # An agent that retries a walled page with the real browser gets it, alone.
    assert tried(retried) == ["real_browser"] and retried["receipt"]["status"] == "observed"


async def test_a_busy_profile_skips_the_real_browser(tmp_path, monkeypatch):
    from frankensurf import real_browser
    fake = tmp_path / "chrome"
    fake.write_text("")
    profile = tmp_path / "profile"
    monkeypatch.setenv("FRANKENSURF_REAL_BROWSER_PATH", str(fake))
    monkeypatch.setenv("FRANKENSURF_REAL_BROWSER_PROFILE", str(profile))
    monkeypatch.setattr(real_browser, "_has_display", lambda: True)
    async with Runtime(tmp_path / "state") as web:
        with real_browser._claim(profile):
            result = await web.read("https://example.com/item", WebPolicy(provider="real_browser"))
    assert result["receipt"]["failure"]["code"] == "PROVIDER_UNAVAILABLE"


async def test_trying_harder_still_reaches_the_real_browser(tmp_path, registry, monkeypatch):
    monkeypatch.delenv("FRANKENSURF_REAL_BROWSER", raising=False)
    registry(provider("w1", "BLOCKED"), provider("real_browser", "ok", scope=True))
    async with Runtime(tmp_path) as web:
        first = await web.read(URL, policy_overrides=POLICY)
        harder = await web.read(URL, policy_overrides={**POLICY, "allow_real_browser": True},
                                retry_of=first["receipt"]["trace_id"])
    assert first["receipt"]["status"] == "failed"
    assert tried(harder) == ["real_browser"] and harder["receipt"]["status"] == "observed"


def test_the_real_browser_is_found_where_it_installs_or_where_you_point(tmp_path, monkeypatch):
    from frankensurf import real_browser
    fake = tmp_path / "chrome"
    fake.write_text("")
    monkeypatch.setenv("FRANKENSURF_REAL_BROWSER_PATH", str(fake))
    assert real_browser.executable() == fake
    monkeypatch.setenv("FRANKENSURF_REAL_BROWSER_PATH", str(tmp_path / "missing"))
    assert real_browser.executable() is None
    monkeypatch.delenv("FRANKENSURF_REAL_BROWSER_PATH")
    monkeypatch.setattr(real_browser, "_candidates", lambda: [tmp_path / "missing", fake])
    assert real_browser.executable() == fake
    with pytest.raises(ValueError):
        WebPolicy(real_browser_wait_seconds=-1)


# --- End to end: a challenge page that clears by itself ----------------------
CHALLENGE = """<html><head><title>Just a moment...</title></head><body><p>Checking your browser.</p>
<script>setTimeout(() => { document.title = 'Brass lamp';
  document.body.innerHTML = '<h1>Brass lamp</h1>' + '<p>A brass lamp, $49, ships in two days.</p>'.repeat(30);
}, 2000);</script></body></html>"""
STUCK = """<html><head><title>Just a moment...</title></head><body><p>Checking your browser.</p></body></html>"""
# A challenge served as 403 that reloads into the page, as managed challenges do.
RELOADS = """<html><head><title>Just a moment...</title></head><body><p>Checking your browser.</p>
<script>setTimeout(() => location.replace('/cleared'), 1500);</script></body></html>"""
CLEARED = "<html><head><title>Brass lamp</title></head><body>" + "<p>A brass lamp, $49, ships in two days.</p>" * 30 + "</body></html>"
PAGES = {"/stuck": (200, STUCK), "/reloads": (403, RELOADS), "/cleared": (200, CLEARED),
         "/empty403": (403, "")}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        status, text = PAGES.get(self.path.split("?")[0], (200, CHALLENGE))
        body = text.encode()
        self.send_response(status)
        self.send_header("content-type", "text/html")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def _chromium():
    found = sorted(glob.glob(str(Path.home() / ".cache/ms-playwright/chromium-*/chrome-linux*/chrome")))
    return found[-1] if found else None


@pytest.fixture
def real(monkeypatch, tmp_path):
    pytest.importorskip("playwright")
    import shutil
    chromium = _chromium()
    if not chromium or not shutil.which("Xvfb"):
        pytest.skip("needs a Chromium build and Xvfb (the real browser runs hidden on a virtual display)")
    monkeypatch.delenv("FRANKENSURF_REAL_BROWSER_WINDOW", raising=False)
    # Playwright's own Chromium stands in for Chrome: the same code path.
    monkeypatch.setenv("FRANKENSURF_REAL_BROWSER_PATH", chromium)
    monkeypatch.setenv("FRANKENSURF_REAL_BROWSER_PROFILE", str(tmp_path / "profile"))


async def test_the_real_browser_waits_out_a_challenge_that_clears_itself(tmp_path, site, real):
    async with Runtime(tmp_path / "state") as web:
        result = await web.read(site + "/item", WebPolicy(provider="real_browser", settle_ms=0,
                                                          real_browser_wait_seconds=10))
    receipt = result["receipt"]
    assert receipt["status"] == "observed", receipt.get("failure")
    assert receipt["method"] == "real_browser" and "ships in two days" in result["text"]


async def test_a_challenge_that_needs_a_person_fails_as_a_wall(tmp_path, site, real):
    async with Runtime(tmp_path / "state") as web:
        result = await web.read(site + "/stuck", WebPolicy(provider="real_browser", settle_ms=0,
                                                           real_browser_wait_seconds=2))
    assert result["receipt"]["failure"]["code"] == "CAPTCHA"


async def test_a_403_challenge_that_reloads_into_the_page_is_read(tmp_path, site, real):
    async with Runtime(tmp_path / "state") as web:
        result = await web.read(site + "/reloads", WebPolicy(provider="real_browser", settle_ms=0,
                                                             real_browser_wait_seconds=10))
    receipt = result["receipt"]
    assert receipt["status"] == "observed", receipt.get("failure")
    assert receipt["http_status"] == 200 and "ships in two days" in result["text"]


async def test_a_bare_403_is_a_wall_not_the_browsers_error_page(tmp_path, site, real):
    async with Runtime(tmp_path / "state") as web:
        result = await web.read(site + "/empty403", WebPolicy(provider="real_browser", settle_ms=0,
                                                              provider_max_attempts_per_candidate=1))
    assert result["receipt"]["failure"]["code"] == "BLOCKED"


async def test_the_window_stays_off_your_screen_unless_you_ask(monkeypatch):
    from frankensurf import real_browser
    monkeypatch.delenv("FRANKENSURF_REAL_BROWSER_WINDOW", raising=False)

    async def private():
        return ":97"

    async def none():
        return None
    monkeypatch.setattr(real_browser, "_virtual_display", private)
    hidden = await real_browser._launch_options()
    assert hidden["env"]["DISPLAY"] == ":97" and "WAYLAND_DISPLAY" not in hidden["env"]
    assert hidden["env"]["XDG_SESSION_TYPE"] == "x11" and "--ozone-platform=x11" in hidden["args"]
    monkeypatch.setattr(real_browser, "_virtual_display", none)
    assert "--window-position=-32000,-32000" in (await real_browser._launch_options())["args"]
    monkeypatch.setenv("FRANKENSURF_REAL_BROWSER_WINDOW", "visible")
    assert await real_browser._launch_options() == {"args": []}


async def test_a_private_display_starts_where_the_socket_folder_is_read_only(monkeypatch):
    """WSLg mounts /tmp/.X11-unix read-only: Xvfb then listens on the abstract socket only."""
    import io
    from frankensurf import real_browser
    started = []

    class Fake:
        pid = 4242

        def __init__(self, args, **kwargs):
            started.append(args)
            self.stdout = io.BytesIO((args[1][1:] + "\n").encode())

        def kill(self):
            pass

        def wait(self):
            pass
    monkeypatch.delenv("FRANKENSURF_REAL_BROWSER_WINDOW", raising=False)
    monkeypatch.setattr(real_browser, "_VIRTUAL", {})
    monkeypatch.setattr(real_browser.shutil, "which", lambda name: "/usr/bin/Xvfb")
    monkeypatch.setattr(real_browser.os, "access", lambda path, mode: False)
    monkeypatch.setattr(real_browser.os.path, "isdir", lambda path: True)
    monkeypatch.setattr(real_browser.os.path, "exists", lambda path: path.endswith("X99"))
    monkeypatch.setattr(real_browser.subprocess, "Popen", Fake)
    monkeypatch.setattr(real_browser.atexit, "register", lambda *args: None)
    assert await real_browser._virtual_display() == ":100"
    assert started[0][1] == ":100" and started[0][-2:] == ["-nolisten", "unix"]
