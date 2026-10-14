"""ShardX as a Frankensurf provider plugin.

ShardX is ProxyShard's anti-detect browser. Its engine is a closed, prebuilt Chromium that the
shardx SDK downloads from the vendor the first time it launches, so this plugin is never part of
Frankensurf itself: you install it, then trust it by name, and only then does it run.

  pip install "frankensurf-shardx @ git+https://github.com/yail259/frankensurf#subdirectory=contrib/frankensurf-shardx"
  frankensurf plugins trust provider shardx frankensurf-shardx

One ShardX browser is started on the first read of a session and reused for the rest of it; each
read gets a fresh page. Frankensurf judges what comes back (challenge pages, empty shells, results
that never loaded), so this only loads the page and returns it.

  FRANKENSURF_SHARDX_HEADLESS=1   run without a window (the default on Linux with no display)
  FRANKENSURF_SHARDX_HEADED=1     always show the window
"""
import asyncio

from frankensurf.providers import ProviderManifest
from frankensurf.runtime import WebFailure


class ShardXBrowser:
    manifest = ProviderManifest(
        "shardx", "0.1.0",             # the ID must match the entry point name in pyproject.toml
        rendering=True,                # a real browser: it can satisfy render=True
        requires_local_browser=True,   # off when allow_local_browser=False
    )

    def available(self, configured):
        # Unavailable means skipped, never failed.
        try:
            import shardx  # noqa: F401
            from playwright.async_api import async_playwright  # noqa: F401
        except ImportError:
            return False
        return True

    async def start(self):
        # Nothing launches here: a session that never routes to ShardX never starts it.
        self.launching = asyncio.Lock()
        self.session = self.playwright = self.browser = None

    def headless(self):
        import os
        import sys
        if os.environ.get("FRANKENSURF_SHARDX_HEADED") == "1":
            return False
        if os.environ.get("FRANKENSURF_SHARDX_HEADLESS") == "1":
            return True
        return sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))

    async def ready(self):
        if getattr(self, "launching", None) is None:
            await self.start()
        async with self.launching:
            if self.browser is not None and self.browser.is_connected():
                return self.browser
            await self.shutdown()
            from playwright.async_api import async_playwright
            from shardx import ShardX
            try:
                sdk = ShardX()
                profile = sdk.create_profile(platform="Windows")
                # Timezone, language and location follow the exit address, as ShardX recommends.
                if hasattr(profile, "with_override"):
                    profile = profile.with_override(timezone="auto", navigator={"language": "auto"},
                                                    geolocation={"mode": "auto"})
                self.session = await asyncio.to_thread(
                    sdk.launch, profile, cdp=True, headless=self.headless(),
                    extra_args=["--no-sandbox", "--disable-dev-shm-usage"])
                self.playwright = await async_playwright().start()
                self.browser = await self.playwright.chromium.connect_over_cdp(self.session.cdp_url)
            except Exception as error:
                await self.shutdown()
                # Only the error's type: its text could carry a download URL or a local path.
                raise WebFailure("PROVIDER_UNAVAILABLE", f"ShardX did not start ({type(error).__name__})") from None
            return self.browser

    async def acquire(self, request, services):
        if not self.available(None):
            raise WebFailure("PROVIDER_UNAVAILABLE", "install frankensurf-shardx with its dependencies first")
        # What the page's status means, as Core reads it for its own browsers: a wall makes
        # Frankensurf climb to the next tool, not-found ends the read.
        page_failures = {401: "AUTH_REQUIRED", 403: "BLOCKED", 404: "NOT_FOUND", 410: "NOT_FOUND",
                         429: "RATE_LIMITED"}
        browser = await self.ready()
        timeout_ms = int(min(request.policy.unblocker_timeout_seconds, 60) * 1000)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await context.new_page()
        try:
            try:
                response = await page.goto(request.url, wait_until="domcontentloaded", timeout=timeout_ms)
                # Let scripted results land before the page is judged.
                await page.wait_for_timeout(2500)
                html = await page.content()
            except Exception as error:
                code = "TIMEOUT" if "Timeout" in type(error).__name__ else "PROVIDER_DOWN"
                raise WebFailure(code, f"ShardX could not load the page ({type(error).__name__})") from None
            status = response.status if response else 200
            if status in page_failures or status >= 500:
                raise WebFailure(page_failures.get(status, "PROVIDER_DOWN"), f"page answered {status}", status)
            return {"url": page.url or request.url, "content": html, "raw": html.encode(),
                    "content_type": "text/html", "http_status": status,
                    "headers": dict(response.headers) if response else {}, "cost_usd": 0.0}
        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def shutdown(self):
        browser, playwright, session = (getattr(self, name, None) for name in ("browser", "playwright", "session"))
        self.browser = self.playwright = self.session = None
        for step in (browser and browser.close, playwright and playwright.stop):
            if step:
                try:
                    await step()
                except Exception:
                    pass
        if session is not None:
            try:
                await asyncio.to_thread(session.stop)
            except Exception:
                pass

    async def aclose(self):
        await self.shutdown()
