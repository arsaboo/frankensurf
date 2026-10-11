"""Hosted acquisition services stitched in as provider plugins.

Each plugin wraps one mature hosted API: Jina Reader (keyless T1 fetch and
extract), Cloudflare Browser Run (rendered HTML), Firecrawl, fastCRW and
Apify's RAG Web Browser (extraction), ZenRows, Scrapfly, Bright Data Web
Unlocker and Zyte (the unblocker rung), Skyvern (an agent task, explicit only),
and the Exa, Brave, Tavily and Parallel search transports. The T2 managed
browsers live in ``managed_browsers.py`` and register here, after Jina. They only make
the API call and return the page or API bytes; Core keeps routing, pacing,
policy, receipts and evidence.

Keys are read when a call runs, from the environment
(``FRANKENSURF_<ID>_API_KEY``), then ``~/.config/frankensurf/.env``, then the
``api_keys`` object in ``~/.config/frankensurf/services.json``. Keys never
appear in results, receipts or errors. A keyed plugin reports itself
unavailable until its key exists, so routing skips it instead of failing.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import httpx


# Tests replace this with an httpx.MockTransport. Production uses the network.
TRANSPORT = None

_SERVICES = Path("~/.config/frankensurf/services.json")
_ENV_FILE = Path("~/.config/frankensurf/.env")


def _env_file():
    """Parse KEY=VALUE lines; comments, blanks and malformed lines are ignored."""
    values = {}
    try:
        lines = _ENV_FILE.expanduser().read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if name and value:
            values[name] = value
    return values


def _services_file():
    try:
        data = json.loads(_SERVICES.expanduser().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _setting(name):
    """Return a configured secret or setting, or None. Never logged or returned."""
    variable = "FRANKENSURF_" + name.upper()
    value = os.environ.get(variable) or _env_file().get(variable)
    if value:
        return value
    keys = _services_file().get("api_keys")
    if isinstance(keys, dict):
        value = keys.get(name.lower())
        if isinstance(value, str) and value:
            return value
    # Services' own conventional names (SCRAPFLY_API_KEY, ZYTE_API_KEY,
    # BRIGHTDATA_ZONE, ...) work too, for keys and zones only, so existing
    # secrets need no renaming.
    if name.lower().endswith(("_api_key", "_api_token", "_zone")):
        value = os.environ.get(name.upper()) or _env_file().get(name.upper())
        if value:
            return value
    return None


def _failure(code, message, status=None, *, response_url=None, cost=None):
    from .runtime import WebFailure
    if cost is None:
        return WebFailure(code, message, status, response_url=response_url)
    return WebFailure(code, message, status, response_url=response_url, cost_usd=cost)


def _rate(name, default):
    """A dollar rate from settings, or the documented list-price default."""
    try:
        value = float(_setting(name) or default)
    except ValueError:
        return default
    return value if value >= 0 else default


def _within_cap(policy, worst_case, service):
    """Cost-bounded plugins refuse a call that could exceed the remaining cap."""
    cap = getattr(policy, "max_cost_usd", None)
    if cap is not None and worst_case > cap:
        raise _failure("BUDGET_EXHAUSTED",
                       service + " request could exceed the remaining cost cap", cost=0.0)


async def _call(method, url, policy, *, headers=None, params=None, json_body=None):
    """One bounded HTTPS call to a hosted API. Returns (status, headers, bytes)."""
    timeout = policy.timeout_seconds
    try:
        async with httpx.AsyncClient(transport=TRANSPORT, timeout=timeout,
                                     follow_redirects=False) as client:
            async with client.stream(method, url, headers=headers, params=params,
                                     json=json_body) as response:
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > policy.max_bytes:
                        raise _failure("LIMIT_EXCEEDED", "Hosted response byte limit exceeded")
                return response.status_code, response.headers, bytes(raw)
    except httpx.TimeoutException:
        raise _failure("TIMEOUT", "Hosted service request timed out") from None
    except httpx.HTTPError:
        raise _failure("PROVIDER_DOWN", "Hosted service request failed") from None


def _service_status(status, service):
    """Map the hosted API's own status (not the target's) to a failure."""
    if status in (401, 403):
        return _failure("PROVIDER_UNAVAILABLE", service + " rejected the configured key", status)
    if status == 402:
        return _failure("BUDGET_EXHAUSTED", service + " account is out of credit", status)
    if status == 429:
        return _failure("RATE_LIMITED", service + " rate limit reached", status)
    if status >= 500:
        return _failure("PROVIDER_DOWN", service + " is unavailable", status)
    if status >= 400:
        return _failure("PROVIDER_DOWN", service + " refused the request", status)
    return None


def _target_status(status, url, cost=None):
    from .runtime import _status_failure
    code = _status_failure(status) if status else None
    if code:
        return _failure(code, "Target response requires stop", status, response_url=url, cost=cost)
    return None


def _page(url, content, content_type, status):
    return {"url": url, "content": content, "raw": content.encode(),
            "content_type": content_type, "http_status": status, "headers": {}}


def _manifest(*args, **kwargs):
    from .providers import ProviderManifest
    # Hosted services fetch from their own network.
    kwargs.setdefault("remote", True)
    return ProviderManifest(*args, **kwargs)


def _decode(raw):
    return raw.decode("utf-8", errors="replace")


class _Keyed:
    """Shared availability for plugins that need an API key."""
    required_settings: tuple[str, ...] = ()

    def available(self, configured):
        return all(_setting(name) for name in self.required_settings)

    def _required(self, name):
        value = _setting(name)
        if not value:
            raise _failure("PROVIDER_UNAVAILABLE", self.manifest.id + " has no configured key")
        return value


class JinaReaderProvider(_Keyed):
    """T1 light fetch and extract through r.jina.ai. Works without a key."""

    def __init__(self):
        self.manifest = _manifest("jina_reader", "1")

    def available(self, configured):
        return True

    async def acquire(self, request, services):
        headers = {"Accept": "application/json", "X-Return-Format": "markdown"}
        key = _setting("jina_api_key")
        if key:
            headers["Authorization"] = "Bearer " + key
        status, _, raw = await _call("GET", "https://r.jina.ai/" + request.url,
                                     request.policy, headers=headers)
        error = _service_status(status, "Jina Reader")
        if error:
            raise error
        try:
            body = json.loads(raw)
            data = body["data"]
            content = data.get("content") or ""
            title = data.get("title")
            final = data.get("url") or request.url
            warning = str(data.get("warning") or "")
            target_status = data.get("httpStatus")
        except (ValueError, KeyError, TypeError, AttributeError):
            raise _failure("SCHEMA_CHANGED", "Jina Reader returned an unexpected body") from None
        error = _target_status(target_status if type(target_status) is int else None, request.url)
        if error:
            raise error
        if not content.strip():
            # Jina answers 200 with empty content and a warning when the
            # target shows a challenge; that is a wall, not an outage.
            if "captcha" in warning.lower():
                raise _failure("CAPTCHA", "Jina Reader saw a challenge page")
            raise _failure("PROVIDER_DOWN", "Jina Reader returned no content")
        if title and not content.lstrip().startswith("# "):
            content = "# " + title + "\n\n" + content
        return _page(final, content, "text/markdown; charset=utf-8", 200)


class InternetArchiveProvider(_Keyed):
    """Last resort: the Internet Archive's closest stored copy of the page.

    Never the live page, so it only runs when a read allows archives
    (allow_archive). The result and receipt carry when it was archived."""

    def __init__(self):
        self.manifest = _manifest("internet_archive", "1", archive=True)

    def available(self, configured):
        return True

    async def acquire(self, request, services):
        if not getattr(request.policy, "allow_archive", False):
            raise _failure("POLICY_DENIED", "Archived copies need allow_archive")
        status, _, raw = await _call("GET", "https://archive.org/wayback/available",
                                     request.policy, params={"url": request.url})
        error = _service_status(status, "Internet Archive")
        if error:
            raise error
        try:
            closest = (json.loads(raw).get("archived_snapshots") or {}).get("closest") or {}
            stamp = str(closest.get("timestamp") or "")
        except (ValueError, AttributeError):
            raise _failure("SCHEMA_CHANGED", "Internet Archive returned an unexpected body") from None
        if not closest.get("available") or str(closest.get("status")) != "200" \
                or not re.fullmatch(r"\d{14}", stamp):
            raise _failure("NOT_FOUND", "The Internet Archive has no stored copy of this page")
        # id_ asks for the page as archived, without the archive's own toolbar.
        status, headers, raw = await _call("GET", f"https://web.archive.org/web/{stamp}id_/{request.url}",
                                           request.policy)
        error = _target_status(status, request.url)
        if error:
            raise error
        archived_at = (f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}T{stamp[8:10]}:{stamp[10:12]}:"
                       f"{stamp[12:14]}Z")
        page = _page(request.url, _decode(raw), headers.get("content-type") or "text/html; charset=utf-8", 200)
        page["archived"] = {"source": "Internet Archive", "archived_at": archived_at,
                            "snapshot_url": f"https://web.archive.org/web/{stamp}/{request.url}"}
        return page


class FirecrawlProvider(_Keyed):
    """Firecrawl /v2/scrape: rendered raw HTML, or markdown when preferred."""
    required_settings = ("firecrawl_api_key",)

    def __init__(self):
        self.manifest = _manifest("firecrawl", "1", rendering=True, paid=True, cost_bounded=True)

    async def acquire(self, request, services):
        key = self._required("firecrawl_api_key")
        # List price: Hobby plan, $19 for 5,000 credits (Oct 2026). A scrape is
        # 1 credit; PDFs cost one credit per page and can exceed this bound.
        per_credit = _rate("firecrawl_usd_per_credit", 0.0038)
        _within_cap(request.policy, per_credit, "Firecrawl")
        markdown = getattr(request.policy, "prefer_markdown", False)
        body = {"url": request.url, "formats": ["markdown" if markdown else "rawHtml"]}
        if request.policy.settle_ms > 1000:
            # Give script-rendered content time, as Core's own browsers settle.
            body["waitFor"] = request.policy.settle_ms
        status, _, raw = await _call(
            "POST", "https://api.firecrawl.dev/v2/scrape", request.policy,
            headers={"Authorization": "Bearer " + key}, json_body=body)
        error = _service_status(status, "Firecrawl")
        if error:
            raise error
        try:
            data = json.loads(raw)["data"]
            metadata = data.get("metadata") or {}
            target_status = metadata.get("statusCode")
            final = metadata.get("url") or metadata.get("sourceURL") or request.url
            content = data.get("markdown" if markdown else "rawHtml") or ""
            credits = metadata.get("creditsUsed")
        except (ValueError, KeyError, TypeError, AttributeError):
            raise _failure("SCHEMA_CHANGED", "Firecrawl returned an unexpected body") from None
        cost = (round(credits * per_credit, 6)
                if type(credits) in (int, float) and credits >= 0 else None)
        error = _target_status(target_status, final, cost)
        if error:
            raise error
        result = _page(final, content,
                       "text/markdown; charset=utf-8" if markdown else "text/html; charset=utf-8",
                       target_status or 200)
        if cost is not None:
            result["cost_usd"] = cost
        return result


class ZenRowsProvider(_Keyed):
    """ZenRows universal scraper API (unblocker rung)."""
    required_settings = ("zenrows_api_key",)

    def __init__(self):
        self.manifest = _manifest("zenrows", "1", rendering=True, paid=True, cost_bounded=True)

    async def acquire(self, request, services):
        # A rendered premium-proxy request was measured at 25 credits, $0.025
        # (X-Request-Cost, Oct 2026). Override if your plan prices differ.
        _within_cap(request.policy, _rate("zenrows_max_request_usd", 0.025), "ZenRows")
        # Wait for script-rendered content, as Core's own browsers settle.
        params = {"apikey": self._required("zenrows_api_key"), "url": request.url,
                  "js_render": "true", "premium_proxy": "true", "original_status": "true",
                  "wait": str(max(request.policy.settle_ms, 2500))}
        status, headers, raw = await _call("GET", "https://api.zenrows.com/v1/",
                                           request.policy, params=params)
        final = headers.get("zr-final-url") or request.url
        try:
            cost = float(headers["x-request-cost"])
            cost = cost if cost >= 0 else None
        except (KeyError, ValueError):
            cost = None
        # With original_status the API mirrors the target status; ZenRows'
        # own errors carry a JSON body with a "code" field.
        if status >= 400 and "json" in headers.get("content-type", ""):
            error = _service_status(status, "ZenRows")
            if error:
                raise error
        error = _target_status(status, final, cost)
        if error:
            raise error
        result = _page(final, _decode(raw), headers.get("content-type", "text/html"), status)
        if cost is not None:
            result["cost_usd"] = cost
        return result


class ScrapflyProvider(_Keyed):
    """Scrapfly scrape API with its unblocker and JS rendering (unblocker rung)."""
    required_settings = ("scrapfly_api_key",)

    def __init__(self):
        self.manifest = _manifest("scrapfly", "1", rendering=True, paid=True)

    async def acquire(self, request, services):
        params = {"key": self._required("scrapfly_api_key"), "url": request.url,
                  "unblocker": "true", "render_js": "true",
                  "rendering_wait": str(max(request.policy.settle_ms, 2500))}
        status, _, raw = await _call("GET", "https://api.scrapfly.io/scrape",
                                     request.policy, params=params)
        error = _service_status(status, "Scrapfly")
        if error:
            raise error
        try:
            result = json.loads(raw)["result"]
            content = result.get("content") or ""
            target_status = result.get("status_code")
            final = result.get("url") or request.url
            content_type = result.get("content_type") or "text/html"
            credits = (json.loads(raw).get("context") or {}).get("cost")
        except (ValueError, KeyError, TypeError, AttributeError):
            raise _failure("SCHEMA_CHANGED", "Scrapfly returned an unexpected body") from None
        # Scrapfly bills in credits; dollars need your plan's rate.
        per_credit = _setting("scrapfly_usd_per_credit")
        cost = None
        try:
            if per_credit and type(credits) in (int, float) and credits >= 0:
                cost = round(credits * float(per_credit), 6)
        except ValueError:
            cost = None
        error = _target_status(target_status, final, cost)
        if error:
            raise error
        page = _page(final, content, content_type, target_status or 200)
        if cost is not None:
            page["cost_usd"] = cost
        return page


class BrightDataUnlockerProvider(_Keyed):
    """Bright Data Web Unlocker REST API (unblocker rung). Needs key and zone."""
    required_settings = ("brightdata_api_key", "brightdata_zone")

    def __init__(self):
        self.manifest = _manifest("brightdata_unlocker", "1", rendering=True, paid=True)

    async def acquire(self, request, services):
        body = {"zone": self._required("brightdata_zone"), "url": request.url, "format": "raw"}
        status, headers, raw = await _call(
            "POST", "https://api.brightdata.com/request", request.policy,
            headers={"Authorization": "Bearer " + self._required("brightdata_api_key")},
            json_body=body)
        error = _service_status(status, "Bright Data")
        if error:
            raise error
        text = _decode(raw)
        # Bright Data refuses some sites (for example until a KYC form is filed)
        # with HTTP 200 and its own short message, flagged in a header: the tool
        # declined, the site was never read.
        refusal = headers.get("x-brd-error") or headers.get("x-luminati-error")
        if not refusal and len(text) < 2000:
            match = re.match(r"\s*\w[\w ]{0,40} Failed \((\w+)\)", text)
            refusal = match.group(1) if match else None
        if refusal:
            raise _failure("PROVIDER_UNAVAILABLE", "Bright Data declined this site (" + str(refusal)[:60] + ")")
        return _page(request.url, text, headers.get("content-type", "text/html"), status)


class _SearchTransport(_Keyed):
    """Search API transport. Route-scoped, so it never reads ordinary pages."""

    @staticmethod
    def _query(url):
        params = parse_qs(urlsplit(url).query)
        query = (params.get("q") or params.get("query") or [""])[0]
        if not query:
            raise _failure("POLICY_DENIED", "Search transport URL has no query")
        try:
            count = max(1, min(int((params.get("count") or ["10"])[0]), 50))
        except ValueError:
            count = 10
        return query, count

    @staticmethod
    def _options(url):
        """Search options carried on the transport URL (see search_url)."""
        params = parse_qs(urlsplit(url).query)
        options = {key: params[key][0] for key in ("site", "recency", "region") if params.get(key)}
        if params.get("exclude"):
            options["exclude_domains"] = [item for item in params["exclude"][0].split(",") if item][:200]
        return options


class ExaTransport(_SearchTransport):
    required_settings = ("exa_api_key",)
    ENDPOINT = "https://api.exa.ai/search"

    def __init__(self):
        self.manifest = _manifest("exa_api", "1", paid=True, route_scope_required=True)

    async def acquire(self, request, services):
        query, count = self._query(request.url)
        options = self._options(request.url)
        body = {"query": query, "numResults": count, "contents": {"highlights": True}}
        if options.get("site"):
            body["includeDomains"] = [options["site"]]
        if options.get("exclude_domains"):
            body["excludeDomains"] = options["exclude_domains"]
        if options.get("recency"):
            body["startPublishedDate"] = _since(options["recency"])
        status, _, raw = await _call(
            "POST", self.ENDPOINT, request.policy,
            headers={"x-api-key": self._required("exa_api_key")}, json_body=body)
        error = _service_status(status, "Exa")
        if error:
            raise error
        result = _page(request.url, _decode(raw), "application/json", 200)
        try:
            total = json.loads(raw).get("costDollars", {}).get("total")
            if isinstance(total, (int, float)) and total >= 0:
                result["cost_usd"] = float(total)
        except (ValueError, AttributeError):
            pass
        return result


class BraveTransport(_SearchTransport):
    required_settings = ("brave_api_key",)
    ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

    @staticmethod
    def _brave_params(query, count, options):
        from .search import _operators
        params = {"q": _operators(query, options)[:400], "count": min(count, 20)}
        if options.get("recency"):
            params["freshness"] = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}[options["recency"]]
        if "-" in options.get("region", ""):
            params["country"] = options["region"].split("-")[1].lower()
        return params

    def __init__(self):
        self.manifest = _manifest("brave_api", "1", paid=True, route_scope_required=True)

    async def acquire(self, request, services):
        query, count = self._query(request.url)
        status, _, raw = await _call(
            "GET", self.ENDPOINT, request.policy,
            headers={"X-Subscription-Token": self._required("brave_api_key"),
                     "Accept": "application/json"},
            params=self._brave_params(query, count, self._options(request.url)))
        error = _service_status(status, "Brave Search")
        if error:
            raise error
        result = _page(request.url, _decode(raw), "application/json", 200)
        # Brave does not report cost per call. Use the list price ($5 per
        # 1,000 Search plan requests, Oct 2026) unless the owner sets their own.
        try:
            result["cost_usd"] = float(_setting("brave_cost_per_query") or 0.005)
        except ValueError:
            result["cost_usd"] = 0.005
        return result


class CloudflareBrowserRunProvider(_Keyed):
    """Cloudflare Browser Run /content: the rendered HTML of one page."""
    required_settings = ("cloudflare_api_token", "cloudflare_account_id")

    def __init__(self):
        self.manifest = _manifest("cloudflare_browser_run", "1", rendering=True, paid=True)

    async def acquire(self, request, services):
        account = quote(self._required("cloudflare_account_id"), safe="")
        status, headers, raw = await _call(
            "POST", "https://api.cloudflare.com/client/v4/accounts/" + account + "/browser-run/content",
            request.policy, headers={"Authorization": "Bearer " + self._required("cloudflare_api_token")},
            json_body={"url": request.url, "gotoOptions": {"waitUntil": "networkidle2"}})
        error = _service_status(status, "Cloudflare Browser Run")
        if error:
            raise error
        content = _decode(raw)
        if "json" in headers.get("content-type", ""):
            # The REST API wraps results as {"success": ..., "result": "<html>"}.
            try:
                body = json.loads(raw)
                if not body.get("success", True):
                    raise _failure("PROVIDER_DOWN", "Cloudflare Browser Run could not render the page")
                content = body["result"]
            except (ValueError, KeyError, TypeError, AttributeError):
                raise _failure("SCHEMA_CHANGED", "Cloudflare Browser Run returned an unexpected body") from None
        if not isinstance(content, str):
            raise _failure("SCHEMA_CHANGED", "Cloudflare Browser Run returned an unexpected body")
        return _page(request.url, content, "text/html; rendered=1", 200)


class ZyteProvider(_Keyed):
    """Zyte API browser rendering through its anti-ban network (unblocker rung)."""
    required_settings = ("zyte_api_key",)

    def __init__(self):
        self.manifest = _manifest("zyte", "1", rendering=True, paid=True)

    async def acquire(self, request, services):
        import base64
        token = base64.b64encode((self._required("zyte_api_key") + ":").encode()).decode()
        status, _, raw = await _call(
            "POST", "https://api.zyte.com/v1/extract", request.policy,
            headers={"Authorization": "Basic " + token},
            json_body={"url": request.url, "browserHtml": True,
                       **({"actions": [{"action": "waitForTimeout",
                                         "timeout": min(request.policy.settle_ms / 1000, 15)}]}
                          if request.policy.settle_ms > 1000 else {})})
        # Zyte reports a target ban or 4xx as a 520/521-style API error with a
        # JSON "type"; those are walls on the target, not an outage.
        if status in (520, 521):
            raise _failure("BLOCKED", "Zyte could not get past the site's protection")
        error = _service_status(status, "Zyte")
        if error:
            raise error
        try:
            body = json.loads(raw)
            content = body["browserHtml"]
            final = body.get("url") or request.url
            target_status = body.get("statusCode")
        except (ValueError, KeyError, TypeError):
            raise _failure("SCHEMA_CHANGED", "Zyte returned an unexpected body") from None
        error = _target_status(target_status if type(target_status) is int else None, final)
        if error:
            raise error
        return _page(final, content, "text/html; rendered=1", target_status or 200)


class ApifyRagBrowserProvider(_Keyed):
    """Apify's RAG Web Browser actor (standby mode): one page as HTML or markdown."""
    required_settings = ("apify_api_token",)
    ENDPOINT = "https://rag-web-browser.apify.actor/search"

    def __init__(self):
        self.manifest = _manifest("apify", "1", rendering=True, paid=True)

    async def acquire(self, request, services):
        markdown = getattr(request.policy, "prefer_markdown", False)
        status, _, raw = await _call(
            "GET", self.ENDPOINT, request.policy,
            headers={"Authorization": "Bearer " + self._required("apify_api_token")},
            params={"query": request.url, "outputFormats": "markdown" if markdown else "html"})
        error = _service_status(status, "Apify")
        if error:
            raise error
        try:
            items = json.loads(raw)
            item = items[0]
            crawl = item.get("crawl") or {}
            final = (item.get("metadata") or {}).get("url") or request.url
            target_status = crawl.get("httpStatusCode")
            content = item.get("markdown" if markdown else "html") or ""
        except (ValueError, KeyError, TypeError, IndexError, AttributeError):
            raise _failure("SCHEMA_CHANGED", "Apify returned an unexpected body") from None
        error = _target_status(target_status if type(target_status) is int else None, final)
        if error:
            raise error
        if not content.strip():
            raise _failure("PROVIDER_DOWN", "Apify returned no content")
        return _page(final, content,
                     "text/markdown; charset=utf-8" if markdown else "text/html; rendered=1",
                     target_status or 200)


class FastCrwProvider(_Keyed):
    """fastCRW /v1/scrape (Firecrawl-compatible; ``fastcrw_url`` for self-hosted CRW)."""
    required_settings = ("fastcrw_api_key",)
    API = "https://api.fastcrw.com"

    def __init__(self):
        self.manifest = _manifest("fastcrw", "1", rendering=True, paid=True)

    def available(self, configured):
        # A self-hosted CRW needs no key.
        return bool(_setting("fastcrw_api_key") or _setting("fastcrw_url"))

    async def acquire(self, request, services):
        markdown = getattr(request.policy, "prefer_markdown", False)
        custom = _setting("fastcrw_url")
        api = (custom or self.API).rstrip("/")
        key = _setting("fastcrw_api_key")
        if not key and not custom:
            raise _failure("PROVIDER_UNAVAILABLE", "fastcrw has no configured key")
        status, _, raw = await _call(
            "POST", api + "/v1/scrape", request.policy,
            headers={"Authorization": "Bearer " + key} if key else {},
            json_body={"url": request.url, "formats": ["markdown" if markdown else "rawHtml"]})
        error = _service_status(status, "fastCRW")
        if error:
            raise error
        try:
            data = json.loads(raw)["data"]
            metadata = data.get("metadata") or {}
            target_status = metadata.get("statusCode")
            final = metadata.get("url") or metadata.get("sourceURL") or request.url
            content = (data.get("markdown") if markdown
                       else data.get("rawHtml") or data.get("html")) or ""
        except (ValueError, KeyError, TypeError, AttributeError):
            raise _failure("SCHEMA_CHANGED", "fastCRW returned an unexpected body") from None
        error = _target_status(target_status if type(target_status) is int else None, final)
        if error:
            raise error
        return _page(final, content,
                     "text/markdown; charset=utf-8" if markdown else "text/html; charset=utf-8",
                     target_status or 200)


class SkyvernProvider(_Keyed):
    """Skyvern Cloud agent task: a browser agent reads the page and returns what it found.

    Explicit only (``provider="skyvern"``): the result is the agent's output as
    JSON, not the page itself. ``browser_agent_task`` sets the prompt.
    """
    required_settings = ("skyvern_api_key",)
    API = "https://api.skyvern.com"
    PROMPT = ("Read this page and return its title and main content, including every "
              "listed item with its price if there are any. Do not click or type anything "
              "except to dismiss a cookie banner.")

    def __init__(self):
        self.manifest = _manifest("skyvern", "1", rendering=True, paid=True, navigation=True,
                                  route_scope_required=True)

    async def acquire(self, request, services):
        import asyncio
        import time
        policy = request.policy
        api = (_setting("skyvern_url") or self.API).rstrip("/")
        headers = {"x-api-key": self._required("skyvern_api_key")}
        body = {"prompt": getattr(policy, "browser_agent_task", None) or self.PROMPT,
                "url": request.url, "max_steps": min(getattr(policy, "browser_agent_max_steps", 10), 10)}
        status, _, raw = await _call("POST", api + "/v1/run/tasks", policy, headers=headers, json_body=body)
        error = _service_status(status, "Skyvern")
        if error:
            raise error
        try:
            run_id = json.loads(raw)["run_id"]
        except (ValueError, KeyError, TypeError):
            raise _failure("SCHEMA_CHANGED", "Skyvern returned an unexpected body") from None
        deadline = time.monotonic() + policy.timeout_seconds
        while True:
            status, _, raw = await _call("GET", api + "/v1/runs/" + quote(run_id, safe=""), policy,
                                         headers=headers)
            error = _service_status(status, "Skyvern")
            if error:
                raise error
            try:
                run = json.loads(raw)
                state = run["status"]
            except (ValueError, KeyError, TypeError):
                raise _failure("SCHEMA_CHANGED", "Skyvern returned an unexpected run") from None
            if state == "completed":
                output = run.get("output")
                content = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
                return _page(request.url, content, "application/json", 200)
            if state in ("failed", "terminated", "canceled"):
                raise _failure("PROVIDER_DOWN", "Skyvern run did not complete")
            if state == "timed_out" or time.monotonic() > deadline:
                raise _failure("TIMEOUT", "Skyvern run did not finish in time")
            await asyncio.sleep(3)


class TavilyTransport(_SearchTransport):
    required_settings = ("tavily_api_key",)
    ENDPOINT = "https://api.tavily.com/search"

    @staticmethod
    def _tavily_body(query, count, options):
        body = {"query": query, "max_results": min(count, 20)}
        if options.get("site"):
            body["include_domains"] = [options["site"]]
        if options.get("exclude_domains"):
            body["exclude_domains"] = options["exclude_domains"]
        if options.get("recency"):
            body["time_range"] = options["recency"]
        return body

    def __init__(self):
        self.manifest = _manifest("tavily_api", "1", paid=True, route_scope_required=True)

    async def acquire(self, request, services):
        query, count = self._query(request.url)
        status, _, raw = await _call(
            "POST", self.ENDPOINT, request.policy,
            headers={"Authorization": "Bearer " + self._required("tavily_api_key")},
            json_body=self._tavily_body(query, count, self._options(request.url)))
        error = _service_status(status, "Tavily")
        if error:
            raise error
        result = _page(request.url, _decode(raw), "application/json", 200)
        # A basic search is 1 credit; $0.008 per credit pay-as-you-go (Oct 2026).
        result["cost_usd"] = _rate("tavily_cost_per_query", 0.008)
        return result


class ParallelTransport(_SearchTransport):
    required_settings = ("parallel_api_key",)
    ENDPOINT = "https://api.parallel.ai/v1/search"

    def __init__(self):
        self.manifest = _manifest("parallel_api", "1", paid=True, route_scope_required=True)

    async def acquire(self, request, services):
        query, _ = self._query(request.url)
        status, _, raw = await _call(
            "POST", self.ENDPOINT, request.policy,
            headers={"x-api-key": self._required("parallel_api_key")},
            json_body={"objective": query, "search_queries": [query], "mode": "fast"})
        error = _service_status(status, "Parallel")
        if error:
            raise error
        result = _page(request.url, _decode(raw), "application/json", 200)
        result["cost_usd"] = _rate("parallel_cost_per_query", 0.005)
        return result


def _since(recency):
    from datetime import datetime, timedelta, timezone
    from .search import RECENCY
    return (datetime.now(timezone.utc) - timedelta(days=RECENCY[recency])).strftime("%Y-%m-%dT%H:%M:%SZ")


def search_url(transport, query, limit, options=None):
    """The Core read URL for a search transport (the plugin turns it into the API call).
    Search options ride along as parameters; each transport applies them natively."""
    endpoint = {"exa_api": ExaTransport.ENDPOINT, "brave_api": BraveTransport.ENDPOINT,
                "tavily_api": TavilyTransport.ENDPOINT, "parallel_api": ParallelTransport.ENDPOINT}[transport]
    url = endpoint + "?q=" + quote(query, safe="") + "&count=" + str(limit)
    options = options or {}
    for key in ("site", "recency", "region"):
        if options.get(key):
            url += "&" + key + "=" + quote(str(options[key]), safe="")
    if options.get("exclude_domains"):
        url += "&exclude=" + quote(",".join(options["exclude_domains"]), safe="")
    return url


# Providers that reach the target through their own unblocking network. An
# explicit read through one of them is not held by an origin cool-down.
UNBLOCKERS = frozenset({"zenrows", "scrapfly", "brightdata_unlocker", "firecrawl", "zyte"})

# Agent providers run a whole browser task, so an attempt gets minutes.
AGENT_PROVIDERS = frozenset({"skyvern", "browser_use"})


HOSTED_PROVIDERS = (JinaReaderProvider, CloudflareBrowserRunProvider, FirecrawlProvider,
                    FastCrwProvider, ApifyRagBrowserProvider, ZenRowsProvider,
                    ScrapflyProvider, BrightDataUnlockerProvider, ZyteProvider,
                    SkyvernProvider, ExaTransport, BraveTransport, TavilyTransport,
                    ParallelTransport, InternetArchiveProvider)


def register(registry):
    from .managed_browsers import MANAGED_BROWSERS
    # Registration order is the fallback order: keyless T1, then T2 managed
    # browsers, then extraction and the T4 unblockers, then search transports.
    order = (JinaReaderProvider, *MANAGED_BROWSERS, *HOSTED_PROVIDERS[1:])
    for plugin in order:
        registry.register(plugin())
