"""Standalone stdlib protocol worker; optional imports live only in this process.

Anonymous public reads only: no profile import, credential access, proxy, solver,
external writes or persistent identity. stdout contains exactly one JSON envelope.
"""
from __future__ import annotations
import asyncio
import base64
import json
import re
from pathlib import Path
import sys
import time
from urllib.parse import urlparse, urljoin


def _status_failure(status):
    return {401:"AUTH_REQUIRED",403:"BLOCKED",429:"RATE_LIMITED",404:"NOT_FOUND",410:"NOT_FOUND"}.get(status) or ("PROVIDER_DOWN" if status >= 500 else None)


def _patchright_disabled_features():
    # Pinned driver source is operator-installed trusted code. Playwright requires
    # exact strings in ignore_default_args, so inspect its generated feature list.
    import patchright
    source=(Path(patchright.__file__).parent/"driver/package/lib/coreBundle.js").read_text()
    match=re.search(r"disabledFeatures = \[([^]]+)\]",source)
    if not match:raise RuntimeError("Unsupported driver security defaults")
    features=re.findall(r'^\s*"([A-Za-z0-9]+)"',match.group(1),re.M)
    if not features:raise RuntimeError("Unsupported driver security defaults")
    return "--disable-features="+",".join(features)


def _scrapling_safety(session, implicit_feature_arg=None):
    # The upstream defaults ignore TLS errors and disable several browser safety
    # features. Restore those protections for our anonymous temporary profile.
    unsafe = ("--no-sandbox", "--disable-web-security", "--ignore-certificate-errors",
              "--disable-cookie-encryption", "--disable-ipc-flooding-protection",
              "--safebrowsing-disable-auto-update", "--disable-client-side-phishing-detection",
              "--password-store=", "--use-mock-keychain", "--disable-background-networking",
              "--disable-features=")
    session._browser_options["args"] = [arg for arg in session._browser_options["args"]
                                         if not arg.startswith(unsafe) and "NetworkServiceInProcess" not in arg]
    ignored=list(session._browser_options.get("ignore_default_args",[]))
    ignored.extend(["--password-store=basic","--use-mock-keychain","--disable-background-networking",
                    implicit_feature_arg or _patchright_disabled_features()])
    session._browser_options["ignore_default_args"]=list(dict.fromkeys(ignored))
    session._browser_options["chromium_sandbox"] = True
    session._context_options.update(ignore_https_errors=False, permissions=[], service_workers="block")


def _browser_internal(url):
    """about:blank, chrome-error:// and similar pages left by a failed navigation."""
    try:
        return isinstance(url, str) and urlparse(url).scheme not in {"http", "https"}
    except ValueError:
        return False


def _valid_url(url):
    try:
        if not isinstance(url, str): return False
        parsed=urlparse(url)
        parsed.port
        return parsed.scheme in {"http","https"} and bool(parsed.hostname) and parsed.username is None and parsed.password is None
    except ValueError:
        return False


FAILURE_STAGES = frozenset({"public_entry_navigation", "public_entry_settle", "document_settle", "content_readiness", "filter_panel_open", "filter_panel_apply", "catalogue_page_ready", "category_navigation", "continuation_response", "continuation_marker", "exact_listing_search", "browser_start", "continuation_settle", "continuation_click", "evidence_capture", "overlay_dismissal", "listing_links_ready", "exact_listing_click", "public_challenge", "navigation_readiness", "selector_readiness", "screenshot_capture", "response_capture"})


_CURRENT_STAGE = None
_REPORT_STAGES = False


# The protocol pipe; main() replaces it with a private handle.
_PROTOCOL = sys.__stdout__


def _report_stage(stage):
    global _CURRENT_STAGE
    if stage not in FAILURE_STAGES: return
    _CURRENT_STAGE = stage
    if _REPORT_STAGES:
        _PROTOCOL.write("@stage " + stage + "\n")
        _PROTOCOL.flush()


def _rows_in(value, depth=0):
    """The longest list of objects inside a JSON value: how much a captured
    response looks like a feed of items rather than config or telemetry."""
    if depth > 8:
        return 0
    if isinstance(value, list):
        here = sum(1 for item in value[:500] if isinstance(item, dict))
        return max([here] + [_rows_in(item, depth + 1) for item in value[:5]])
    if isinstance(value, dict):
        return max([0] + [_rows_in(item, depth + 1) for item in list(value.values())[:200]
                          if isinstance(item, (dict, list))])
    return 0


def _keep_capture(items, entry, limit):
    """Add entry to a bounded capture. When full, a response carrying a list of
    objects replaces the kept response with the fewest; True when kept."""
    if len(items) < limit:
        items.append(entry)
        return True
    rows = _rows_in(entry["data"])
    if rows < 3:
        return False
    weakest = min(range(len(items)), key=lambda index: _rows_in(items[index]["data"]))
    if _rows_in(items[weakest]["data"]) >= rows:
        return False
    items[weakest] = entry
    return True


_SHADOW_SCRIPT = r"""() => {
  // Open shadow roots, recursively: web components keep their content there,
  // where page.content() does not look.
  const out = [], seen = new Set();
  let size = 0;
  const collect = (root) => {
    for (const el of root.querySelectorAll('*')) {
      const shadow = el.shadowRoot;
      if (shadow && !seen.has(shadow)) {
        seen.add(shadow);
        const html = shadow.innerHTML;
        size += html.length;
        if (size > 5000000) return;
        out.push(html);
        collect(shadow);
      }
    }
  };
  collect(document);
  return out.length ? out.join('\n') : null;
}"""


def _camoufox_binaries(version):
    """The pinned Camoufox build, wherever this platform caches it."""
    import os
    roots = [Path.home()/".cache"/"camoufox", Path.home()/"Library"/"Caches"/"camoufox"]
    if os.environ.get("LOCALAPPDATA"):
        roots.append(Path(os.environ["LOCALAPPDATA"])/"camoufox")
    found = []
    for root in roots:
        for build in (root/"browsers"/"official").glob(version + "-*"):
            for name in ("camoufox-bin", "Camoufox.app/Contents/MacOS/camoufox", "camoufox.exe"):
                if (build/name).is_file():
                    found.append(build/name)
                    break
    return found


def _browser_proxy(settings):
    return {key: settings[key] for key in ("server", "username", "password") if key in settings}


def _capture_json(capture, request, url, status, content_type, body):
    """Keep one JSON (or newline-delimited JSON) body the page fetched for itself."""
    limit_items = request.get("capture_json_max_items", 20)
    limit_bytes = request.get("capture_json_max_bytes", 2 * 1024 * 1024)
    if body is None or len(body) > limit_bytes or not _valid_url(url):
        capture["json_skipped"] += 1
        return
    try:
        text = body.decode("utf-8")
        stripped = text.lstrip("\ufeff \t\r\n")
        for prefix in ("for (;;);", "for(;;);", ")]}'", "while(1);", "while (1);", "&&&START&&&"):
            if stripped.startswith(prefix):
                text = stripped[len(prefix):].lstrip(",\r\n ")
                break
        data, shape = json.loads(text), "json"
    except (UnicodeDecodeError, ValueError):
        try:
            lines = [line for line in text.splitlines() if line.strip()]
            if len(lines) < 2:
                raise ValueError()
            data, shape = [json.loads(line) for line in lines], "ndjson"
        except (UnboundLocalError, ValueError):
            capture["json_skipped"] += 1
            return
    if not _keep_capture(capture["json_items"], {"url": url, "http_status": status, "content_type": content_type,
                                                 "format": shape, "data": data}, limit_items):
        capture["json_skipped"] += 1


def _failed(code, status=None, url=None, stage=None):
    # Never send malformed URLs or embedded credentials across the worker boundary.
    # A failed navigation often leaves the page on about:blank or a browser error
    # URL; drop that URL but keep the real failure code (TIMEOUT, BLOCKED, ...).
    if url is not None and not _valid_url(url):
        if not _browser_internal(url): return {"status":"failed","failure":"INVALID_URL"}
        url = None
    result={"status":"failed","failure":code}
    if stage in FAILURE_STAGES: result["failure_stage"] = stage
    if status is not None: result["http_status"]=status
    if url is not None: result["url"]=url
    return result


def _failure_content(packet, content, content_type, request):
    """Bounded anonymous response evidence; never upgrades a failed packet."""
    if (request.get("retain_public_failure_evidence") and packet.get("url")
            and isinstance(content, str) and isinstance(content_type, str)
            and len(content.encode()) <= request["max_bytes"]):
        packet["content"] = content
        packet["content_type"] = content_type
    return packet


async def _page_failure(page, request, code, status=None, stage=None):
    """Retain opt-in, byte-bounded anonymous browser failure evidence."""
    packet = _failed(code, status, page.url, stage)
    if request.get("retain_public_failure_evidence"):
        try:
            content = await page.content()
        except Exception:
            return packet
        return _failure_content(packet, content, "text/html; rendered=1", request)
    return packet


_SCROLL_SCRIPT = """async (screens) => {
  for (let i = 0; i < screens; i++) {
    window.scrollBy(0, window.innerHeight);
    await new Promise((done) => setTimeout(done, 350));
  }
  window.scrollTo(0, 0);
}"""


async def _scroll_for_lazy(page, request):
    """Scroll a few screens so lazy images and cards load; best effort."""
    screens = request.get("scroll_screens") or 0
    if screens:
        try:
            await page.evaluate(_SCROLL_SCRIPT, screens)
        except Exception:
            pass


async def _content_readiness(page, request):
    """Bounded optional semantic readiness; timeout still captures page truth."""
    selector = request.get("content_ready_selector")
    if selector is None:
        return None
    _report_stage("content_readiness")
    requested_timeout = request["content_ready_timeout_seconds"]
    wait_timeout = requested_timeout
    deadline = request.get("_deadline_monotonic")
    if deadline is not None:
        available = max(0.0, deadline - time.monotonic())
        # Keep a bounded share of the operation deadline for the authoritative
        # HTML/screenshot capture that follows a soft readiness timeout.
        capture_reserve = min(available / 2, 5.0)
        wait_timeout = min(wait_timeout, max(0.0, available - capture_reserve))
    if wait_timeout <= 0:
        status = "timed_out"
    else:
        try:
            await page.locator(selector).first.wait_for(
                state=request["wait_state"], timeout=wait_timeout * 1000)
            status = "satisfied"
        except Exception as exc:
            if "timeout" not in type(exc).__name__.lower():
                raise
            status = "timed_out"
    return {"status": status, "timeout_seconds": requested_timeout}


def _result(url, content, content_type, status, screenshot=None, navigation=None,
            content_readiness=None):
    # The requested URL was validated before the worker ran, so a browser-internal
    # final URL means the browser never landed on a page: a transient failure.
    if not _valid_url(url): return _failed("PROVIDER_DOWN" if _browser_internal(url) else "INVALID_URL")
    if status is None: return _failed("PROVIDER_DOWN",url=url)
    failure = _status_failure(status) if status is not None else None
    if failure: return _failed(failure, status, url)
    result = {"status":"ok","url":url,"content":content,"content_type":content_type,"http_status":status}
    if navigation: result["navigation"] = navigation
    if content_readiness is not None:
        result["content_readiness"] = content_readiness
    if screenshot and len(screenshot) <= 8*1024*1024:
        result["screenshot_base64"] = base64.b64encode(screenshot).decode()
    return result


async def _representation(page, response, maximum):
    mime = response.headers.get("content-type", "")
    if "json" in mime or any(t in mime for t in ("javascript", "text/plain")):
        raw = await response.body()
        if len(raw)>maximum: return None, None
        text = raw.decode("utf-8-sig", errors="replace")
        try: json.loads(text)
        except ValueError: pass
        else: return text, "application/json"
    text = await page.content()
    try:
        shadow = await page.evaluate(_SHADOW_SCRIPT)
    except Exception:
        shadow = None
    if shadow:
        block = '<div data-frankensurf-shadow-dom="1">' + shadow + "</div>"
        index = text.lower().rfind("</body>")
        text = text[:index] + block + text[index:] if index >= 0 else text + block
    return text, "text/html; rendered=1"


async def _direct_camoufox(page, url, request, timeout, maximum):
    if request.get("public_entry_url"):
        try:
            from public_entry import navigate_entry
            await navigate_entry(page,request,_status_failure,_report_stage)
        except Exception as exc:
            code=getattr(exc,"code",None) or ("TIMEOUT" if "timeout" in type(exc).__name__.lower() else "PROVIDER_DOWN")
            if code=="POLICY_DENIED": return _failed(code,stage=_CURRENT_STAGE)
            return await _page_failure(page,request,code,stage=_CURRENT_STAGE)
    _report_stage("navigation_readiness")
    response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout*1000)
    if response is None:
        return _failed("PROVIDER_DOWN", stage=_CURRENT_STAGE)
    status = response.status
    failure = _status_failure(status)
    if failure:
        return await _page_failure(page, request, failure, status, stage=_CURRENT_STAGE)
    if request.get("wait_selector"):
        _report_stage("selector_readiness")
        await page.locator(request["wait_selector"]).first.wait_for(state=request["wait_state"])
    if request["settle_ms"]:
        _report_stage("document_settle")
        await page.wait_for_timeout(request["settle_ms"])
    await _scroll_for_lazy(page, request)
    content_readiness = await _content_readiness(page, request)
    _report_stage("response_capture")
    content, mime = await _representation(page, response, maximum)
    if content is None or len(content.encode()) > maximum:
        return _failed("LIMIT_EXCEEDED", status, page.url, stage=_CURRENT_STAGE)
    _report_stage("screenshot_capture")
    screenshot = await page.screenshot(full_page=False)
    return _result(page.url, content, mime, status, screenshot,
                   content_readiness=content_readiness)


def _chromium_executable():
    """nodriver drives an installed Chrome; fall back to Playwright's Chromium."""
    import shutil
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    for app in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Chromium.app/Contents/MacOS/Chromium"):
        if Path(app).is_file():
            return app
    caches = [Path.home()/".cache/ms-playwright", Path.home()/"Library/Caches/ms-playwright"]
    patterns = ("chromium-*/chrome-linux*/chrome", "chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium",
                "chromium-*/chrome-mac*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing")
    builds = sorted(path for cache in caches for pattern in patterns for path in cache.glob(pattern))
    return str(builds[-1]) if builds else None


async def _nodriver(url, request, timeout, maximum):
    """nodriver: CDP-direct Chrome with no WebDriver or Playwright layer.

    nodriver does not expose the document's HTTP status, so a page counts as
    200 unless its content is a challenge; Core's content checks still apply.
    """
    _report_stage("browser_start")
    import nodriver
    executable = _chromium_executable()
    if executable is None:
        return _failed("PROVIDER_UNAVAILABLE")
    proxy = request.get("proxy") or {}
    if proxy.get("username"):
        return _failed("PROVIDER_UNAVAILABLE")  # Chrome flags cannot carry proxy credentials.
    browser = await nodriver.start(headless=request.get("public_browser_headless", True),
                                   browser_executable_path=executable,
                                   browser_args=["--no-first-run", "--no-default-browser-check"]
                                   + (["--proxy-server=" + proxy["server"]] if proxy.get("server") else []))
    try:
        _report_stage("navigation_readiness")
        tab = await asyncio.wait_for(browser.get(url), timeout)
        await tab.sleep(max(request["settle_ms"], 1000) / 1000)
        _report_stage("response_capture")
        content = await asyncio.wait_for(tab.get_content(), timeout)
        final_url = tab.target.url if getattr(tab, "target", None) else url
        if content is None or len(content.encode()) > maximum:
            return _failed("LIMIT_EXCEEDED", url=final_url)
        return _result(final_url, content, "text/html; rendered=1", 200)
    finally:
        try:
            browser.stop()
        except Exception:
            pass


async def acquire(request):
    url=request["url"]; provider=request["provider"]
    timeout=float(request["timeout_seconds"]); maximum=int(request["max_bytes"])
    request["_deadline_monotonic"]=time.monotonic()+timeout
    if not _valid_url(url): return _failed("INVALID_URL")
    if provider == "camoufox":
        _report_stage("browser_start")
        from camoufox.async_api import AsyncCamoufox
        from camoufox.addons import DefaultAddons
        binaries=_camoufox_binaries("152.0.4-beta.31")
        if len(binaries)!=1:return {"status":"failed","failure":"PROVIDER_UNAVAILABLE"}
        async with AsyncCamoufox(headless=request.get("public_browser_headless",True), geoip=False, humanize=False,
                executable_path=str(binaries[0]),exclude_addons=[DefaultAddons.UBO],
                **({"proxy": _browser_proxy(request["proxy"])} if request.get("proxy") else {})) as browser:
            context=await browser.new_context(ignore_https_errors=False, permissions=[])
            page=await context.new_page()
            page.set_default_timeout(timeout*1000)
            return await _direct_camoufox(page, url, request, timeout, maximum)
    if provider == "scrapling":
        _report_stage("browser_start")
        from scrapling.fetchers import AsyncStealthySession
        capture={}
        async def observed(page):
            try:
                if request["settle_ms"]: await page.wait_for_timeout(request["settle_ms"])
                if request.get("wait_selector"):
                    _report_stage("selector_readiness")
                    await page.locator(request["wait_selector"]).first.wait_for(state=request["wait_state"])
                await _scroll_for_lazy(page, request)
                capture["content_readiness"] = await _content_readiness(page, request)
                _report_stage("screenshot_capture")
                capture["screenshot"]=await page.screenshot(full_page=False)
                _report_stage("response_capture")
                capture["ready"]=True
            except Exception as exc:
                capture["failure"]=getattr(exc,"code",None) or ("TIMEOUT" if "timeout" in type(exc).__name__.lower() else "PROVIDER_DOWN")
        async def setup(page):
            capture["page"] = page
            if request.get("capture_json_responses"):
                capture["json_items"], capture["json_skipped"], capture["json_tasks"] = [], 0, []
                def on_response(response):
                    try:
                        kind = (response.headers.get("content-type") or "").lower()
                        if (response.request.resource_type not in {"xhr", "fetch"}
                                or any(skip in kind for skip in ("image/", "font/", "video/", "audio/", "text/css"))):
                            return
                    except Exception:
                        return
                    async def read_body():
                        try:
                            body = await response.body()
                        except Exception:
                            body = None
                        _capture_json(capture, request, response.url, response.status,
                                      response.headers.get("content-type"), body)
                    capture["json_tasks"].append(asyncio.ensure_future(read_body()))
                page.on("response", on_response)
            if request.get("retain_public_failure_evidence"):
                original_close = page.close
                async def close_with_evidence(*args, **kwargs):
                    try:
                        content = await page.content()
                        if len(content.encode()) <= maximum:
                            capture["closing_page"] = {"content": content, "url": page.url}
                    except Exception:
                        pass  # Closed/detached pages do not change the original outcome.
                    finally:
                        await original_close(*args, **kwargs)
                page.close = close_with_evidence
            mode=request.get("scrapling_navigation_wait_until")
            if mode is not None:
                from scrapling_ready import configure_navigation_readiness
                await configure_navigation_readiness(page, mode, _report_stage)
        session=AsyncStealthySession(headless=request.get("public_browser_headless",True), solve_cloudflare=request.get("scrapling_solve_cloudflare", False), retries=1,
            google_search=request.get("scrapling_google_search",False), disable_resources=False, network_idle=False,
            load_dom=request.get("scrapling_load_dom",True),
            timeout=timeout*1000, wait=0, page_action=observed, page_setup=setup,
            locale="en-AU",timezone_id="Australia/Sydney",
            **({"proxy": _browser_proxy(request["proxy"])} if request.get("proxy") else {}))
        _scrapling_safety(session)
        async with session:
            try:
                response=await session.fetch(url)
            except Exception as exc:
                failure = "TIMEOUT" if "timeout" in type(exc).__name__.lower() else "PROVIDER_DOWN"
                if "page" in capture:
                    packet = await _page_failure(capture["page"], request, failure, stage=_CURRENT_STAGE)
                    retained = capture.get("closing_page")
                    if "content" not in packet and retained is not None:
                        packet["url"] = retained["url"]
                        packet = _failure_content(packet, retained["content"], "text/html; rendered=1", request)
                    return packet
                return _failed(failure)
        content=getattr(response, "html_content", None)
        status=response.status
        final_url=response.url
        failure = _status_failure(status) or capture.get("failure")
        if failure:
            return _failure_content(_failed(failure, status, final_url, stage=_CURRENT_STAGE if _status_failure(status) is None else None), content, "text/html; rendered=1", request)
        if len(content.encode())>maximum:return _failed("LIMIT_EXCEEDED", response.status, response.url)
        result=_result(final_url,content,"text/html; rendered=1",status,
                       capture.get("screenshot"),
                       content_readiness=capture.get("content_readiness"))
        if "json_tasks" in capture and result.get("status") == "ok":
            pending = capture["json_tasks"]
            if pending:
                done, still = await asyncio.wait(pending, timeout=3)
                for task in still:
                    task.cancel()
                    capture["json_skipped"] += 1
            result["captured_json"] = {"items": capture["json_items"], "skipped": capture["json_skipped"]}
        return result
    if provider == "patchright":
        # Patchright: Playwright patched against CDP leaks, on its own Chromium.
        _report_stage("browser_start")
        from patchright.async_api import async_playwright as patchright
        async with patchright() as playwright:
            browser = await playwright.chromium.launch(headless=request.get("public_browser_headless", True),
                **({"proxy": _browser_proxy(request["proxy"])} if request.get("proxy") else {}))
            try:
                context = await browser.new_context(**(request.get("profile_context") or {}))
                page = await context.new_page()
                page.set_default_timeout(timeout*1000)
                result = await _direct_camoufox(page, url, request, timeout, maximum)
                if request.get("profile_context") and result.get("status") == "ok":
                    result["storage_state"] = await context.storage_state()
                return result
            finally:
                await browser.close()
    if provider == "nodriver":
        return await _nodriver(url, request, timeout, maximum)
    if provider == "scrapling_http":
        from scrapling.fetchers import AsyncFetcher
        response=await AsyncFetcher.get(url, impersonate="chrome", verify=True,
            retries=1,timeout=timeout,follow_redirects=True,max_redirects=10,
            stealthy_headers=False,
            **({"proxy": request["proxy"]["url"]} if (request.get("proxy") or {}).get("url") else {}))
        if failure:=_status_failure(response.status):
            raw = response.body
            content = raw if isinstance(raw, str) else raw.decode(response.encoding or "utf-8", errors="replace")
            return _failure_content(_failed(failure, response.status, response.url), content, response.headers.get("content-type", "text/html"), request)
        raw=response.body
        if isinstance(raw,str):raw=raw.encode()
        if len(raw)>maximum:return _failed("LIMIT_EXCEEDED", response.status, response.url)
        mime=response.headers.get("content-type", "text/html")
        return _result(response.url,raw.decode(response.encoding or "utf-8",errors="replace"),mime,response.status)
    return {"status":"failed","failure":"PROVIDER_DOWN"}


def _debug_traceback():
    import traceback
    traceback.print_exc(file=sys.__stderr__)


def main():
    global _REPORT_STAGES, _PROTOCOL
    # Keep a private handle on the protocol pipe and point fd 1 (and
    # sys.stdout) at stderr, so library exit hooks and child browsers cannot
    # write into the protocol stream: nodriver prints when it exits.
    import os
    _PROTOCOL = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    try:
        # A profile's storage state can be large; the request stays bounded.
        request=json.loads(sys.stdin.buffer.read(16*1024*1024))
        _REPORT_STAGES = request.get("report_stages") is True
        result=asyncio.run(acquire(request))
    except (ImportError,FileNotFoundError):
        _debug_traceback()
        result={"status":"failed","failure":"PROVIDER_UNAVAILABLE"}
    except Exception as exc:
        # Do not emit exception text in the result: library errors can contain
        # sensitive context. The traceback goes to stderr, which the runtime
        # discards unless its owner sets FRANKENSURF_DEBUG_WORKERS=1.
        _debug_traceback()
        failure="TIMEOUT" if "timeout" in type(exc).__name__.lower() else "PROVIDER_DOWN"
        result=_failed(failure,stage=_CURRENT_STAGE)
    _PROTOCOL.write(json.dumps(result,ensure_ascii=False))
    _PROTOCOL.flush()


if __name__=="__main__":main()
