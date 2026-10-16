import os
from mcp.server.fastmcp import FastMCP
from .runtime import Runtime, WebPolicy, default_state_dir

from . import skill as _skill

# The agent skill doubles as the server's instructions, so every MCP client
# learns which tool and option fits, and how to read a receipt.
server = FastMCP("FrankenSurf", instructions=_skill.body())
try:
    # Clients see FrankenSurf's version in initialize, not the MCP library's.
    from importlib.metadata import version as _package_version
    server._mcp_server.version = _package_version("frankensurf")
except Exception:
    pass


def runtime():
    return Runtime(state_dir=default_state_dir(),
                   identity_registry=os.getenv("FRANKENSURF_IDENTITIES"))


def _acquisition_overrides(acquisition_policy, named):
    # JSON key presence is the authority record: False, None and 0 are supplied.
    if acquisition_policy is not None and not isinstance(acquisition_policy, dict):
        raise ValueError("acquisition_policy must be an object")
    options = dict(acquisition_policy or {})
    if set(options) & set(named):
        raise ValueError("Do not duplicate explicit tool arguments in acquisition_policy")
    options.update({key: value for key, value in named.items() if value is not None})
    return options


@server.tool()
async def read(url: str, provider: str | None = None, render: bool | None = None,
               include_images: bool | None = None, freshness: str | None = None,
               identity: str | None = None, allow_handoff: bool | None = None,
               profile: str | None = None, try_harder_than: str | None = None,
               expect_terms: list[str] | None = None, card_images: bool | None = None,
               module: str | None = None, module_override: dict | None = None,
               allow_archive: bool | None = None, items: bool | None = None,
               main_content: bool | None = None, lightning: bool | None = None,
               allow_real_browser: bool | None = None, schema: dict | None = None,
               links: bool | None = None, markdown: bool | None = None,
               acquisition_policy: dict | None = None) -> dict:
    """Retrieve evidence. Omitted settings use runtime defaults and operator public routes.

    Direct null arguments mean omitted; acquisition_policy preserves explicit null
    operational settings. Content does not certify live or sold state.
    allow_handoff=True lets a person clear a CAPTCHA, sign-in or 2FA wall in a
    visible browser when every automatic route fails; the call waits for them.
    lightning=True starts the read and two other tools at once and keeps the
    first complete page: faster first reads of slow or walled sites, at the cost
    of up to three requests to the site at once.
    allow_real_browser=True lets the owner's real Chrome (or Edge) try a page that
    keeps meeting walls: a real, non-headless window kept off the owner's
    screen, with FrankenSurf's own profile. It never clicks; a wall that needs a
    person still fails.
    main_content=True returns the article without menus, footers or banners as
    text (receipt.main_content says how it was cut; full_text_chars is the size
    of the whole page's text). Use it for articles and news.
    items=True returns items (name, url, price, image) from a listing page with no
    saved module, found in the page's own data, plus result.auto_module: the
    drafted module, which site_modules put saves for every later read.
    allow_archive=True lets a walled page come from the Internet Archive's latest
    stored copy as the last resort; receipt.archived says when it was taken. Not
    live: don't use it for prices or stock.
    A PDF cut short by pdf_max_pages or the cost cap says so in structured.truncated
    and receipt.pdf. A paid page that cost more than max_cost_usd is still returned,
    with receipt.budget.exceeded true (strict_cost_cap=True fails it instead).
    profile names a stored FrankenSurf login (see the profiles tool); the session
    itself never reaches the agent.
    try_harder_than takes the trace_id of an earlier read whose page wasn't what
    you needed: this read skips every tool that one tried and starts from the
    strongest remaining tool. receipt.if_not_right says what is left to try.
    expect_terms are words a search page's results should mention besides the
    URL's own query; results that mention none come back flagged
    completeness.off_query, with receipt.next_step, instead of escalating.
    card_images=True adds cards: each result link with its title and its own
    thumbnail URL, from the same page load.
    links=True adds links: every link on the page (url and anchor text), in page
    order, so a results page's item URLs come back with it (max_links caps it, 500).
    markdown=True adds markdown: the page converted to markdown (headings, lists,
    links, tables), easier for a model to follow than the plain text.
    schema is a JSON Schema ({"type": "object", "properties": {...}, "required": [...]}):
    result.extracted.data fills its properties from what the page publishes for machines
    (JSON-LD, microdata, Open Graph, its own JSON), with missing, sources and valid. A page
    missing a required property counts as incomplete, so the read climbs to a stronger tool.
    No model is used: a property the page never publishes stays missing.
    A saved site module matching the URL shapes the read by default (see the
    site_modules tool): the result gains items and receipt.module. module names
    one, "none" turns modules off, module_override runs an unsaved module once.
    """
    options = _acquisition_overrides(acquisition_policy, {"provider": provider,
        "render": render, "include_images": include_images, "freshness": freshness,
        "identity": identity, "allow_handoff": allow_handoff, "profile": profile,
        "expect_terms": tuple(expect_terms) if expect_terms else None,
        "card_images": card_images, "allow_archive": allow_archive, "auto_items": items,
        "include_links": links, "markdown_output": markdown,
        "main_content": main_content, "lightning": lightning,
        "allow_real_browser": allow_real_browser, "extract_schema": schema})
    # Agents read text: ask servers for markdown first (T0). Callers can pass
    # acquisition_policy={"prefer_markdown": false} for raw HTML structure.
    # Markdown drops the JSON-LD and meta tags a schema is filled from.
    options.setdefault("prefer_markdown", not schema)
    extra = {"retry_of": try_harder_than} if try_harder_than else {}
    if module is not None:
        extra["module"] = False if module == "none" else module
    if module_override is not None:
        extra["module_override"] = module_override
    async with runtime() as web:
        result = await web.read(url, policy_overrides=options, **extra)
        result.pop("content", None)
        if main_content and "main_text" in result:
            result["full_text_chars"] = len(result.get("text") or "")
            result["text"] = result.pop("main_text")
        return result


@server.tool()
async def read_template(module: str, template: str, params: dict | None = None,
                        expect_terms: list[str] | None = None, card_images: bool | None = None,
                        acquisition_policy: dict | None = None) -> dict:
    """Read a URL built from a saved site module's template, e.g. its search URL
    with params={"query": "desk lamp"}. The module shapes the read: the result
    has items, next_url when the module knows pagination, and receipt.module
    with its assertion results."""
    options = _acquisition_overrides(acquisition_policy, {
        "expect_terms": tuple(expect_terms) if expect_terms else None, "card_images": card_images})
    options.setdefault("prefer_markdown", True)
    async with runtime() as web:
        result = await web.read_template(module, template, params, policy_overrides=options)
        result.pop("content", None)
        return result


@server.tool()
async def batch_template(module: str, template: str, params_list: list[dict],
                         acquisition_policy: dict | None = None) -> list[dict]:
    """Read one saved site module template for many parameter sets at once,
    e.g. its search template for ten queries. Reads are paced per site (the
    first goes alone, the rest follow its route). Returns, in order, each
    read's params, items, next_url and receipt status, without page text."""
    options = _acquisition_overrides(acquisition_policy, {})
    async with runtime() as web:
        results = await web.batch_template(module, template, params_list, policy_overrides=options)
    keep = ("url", "params", "items", "next_url")
    return [{**{key: result[key] for key in keep if key in result},
             "receipt": {key: (result.get("receipt") or {}).get(key)
                         for key in ("status", "method", "trace_id", "failure", "module", "latency_ms")}}
            for result in results]


@server.tool()
async def repair_site_module(trace_id: str, module: dict) -> dict:
    """Propose a fixed site module after a read reported receipt.module.status
    failed or invalid. module is the full next version (same id and origin, new
    version). FrankenSurf checks it with the old version's own assertions
    against the page that read kept and a fresh read. Activation is the owner's
    call (frankensurf repair-promote); this tool never changes the saved module."""
    async with runtime() as web:
        return await web.propose_module_repair(trace_id, module)


@server.tool()
async def discover_site_module(url: str, save: bool = False, module_id: str | None = None,
                               query: str | None = None) -> dict:
    """Draft a site module from a listing page's own JSON feed (JSON-LD, JSON in
    a script tag, or JSON the page fetched while rendering). Returns up to three
    validated drafts, each with the item count and sample rows it extracts from
    that page. save=True saves the best one, after which reads of matching URLs
    return items. Check the sample before saving. url can be a home page with
    query: the site's search (SearchAction or search form) is found and used,
    and the draft gets a search template for read_template and batch_template."""
    async with runtime() as web:
        return await web.discover_module(url, module_id=module_id, save=save, query=query)


@server.tool()
async def site_modules(action: str = "list", module_id: str | None = None,
                       module: dict | None = None) -> dict | list:
    """Save and manage site modules: per-site knowledge as data, never code.

    action: list | get | put | enable | disable | delete. put takes module, a
    frankensurf.site-module/v1 object: id, version, match {origin, path_pattern},
    and any of templates (URLs with {params}), readiness {selector, settle_ms},
    sources (jsonld | embedded_json | captured_json | html), items {from,
    fields, join}, pagination, invalid markers, assertions
    (frankensurf.workload-assertions/v1 over {items, count, first,
    matching_query}) and operational policy_defaults. A module can never grant
    identity, providers, paid tools or other origins. Saving the same id again
    replaces it; bump version when you change it.
    """
    async with runtime() as web:
        registry = web.site_modules
        if action == "list":
            return registry.inspect()
        if action == "put":
            if module is None:
                raise ValueError("put needs module")
            return registry.put(module)
        if module_id is None:
            raise ValueError(action + " needs module_id")
        if action == "get":
            found = registry.get(module_id)
            return {**found.metadata(), "module": found.record()}
        if action in ("enable", "disable"):
            registry.enable(module_id, action == "enable")
            return registry.get(module_id).metadata()
        if action == "delete":
            registry.delete(module_id)
            return {"deleted": module_id}
        raise ValueError("action must be list, get, put, enable, disable or delete")


@server.tool()
async def watch_sites(sites: list[str], since: str | None = None, read_new: bool = False,
                      max_new_per_site: int = 50, acquisition_policy: dict | None = None) -> dict:
    """New pages on many sites since the last poll, from the feeds and sitemaps
    each site publishes (found once, then polled with conditional requests, so
    an unchanged site costs almost nothing). The same story on two URLs is
    reported once. since (ISO date) bounds the first poll. read_new=True also
    reads the new pages' main text. Cheaper than searching or re-reading front
    pages to find what is new."""
    options = _acquisition_overrides(acquisition_policy, {})
    async with runtime() as web:
        result = await web.watch_sites(sites, since=since, read_new=read_new,
                                       max_new_per_site=max_new_per_site, policy_overrides=options)
    return result


@server.tool()
async def extract(url: str, adapter: str | None = None, include_images: bool | None = None,
                  identity: str | None = None, provider: str | None = None,
                  freshness: str | None = None, acquisition_policy: dict | None = None,
                  workload_assertions: dict | None = None) -> dict:
    """Extract requested evidence with identity checks and operator public routes.

    Direct null arguments mean omitted; acquisition_policy preserves explicit null,
    false and zero operational settings. Omitting the adapter uses HTML.
    """
    options = _acquisition_overrides(acquisition_policy, {"identity": identity,
        "provider": provider, "freshness": freshness, "include_images": include_images})
    async with runtime() as web:
        kwargs = ({"workload_assertions": workload_assertions}
                  if workload_assertions is not None else {})
        result = await web.extract(
            url, adapter if adapter is not None else "html",
            policy_overrides=options, **kwargs)
        result.pop("content", None)
        return result


@server.tool()
async def do(intent: dict, identity: str, action_classes: list[str],
             provider: str | None = None,
             action_policy: dict | None = None) -> dict:
    """Execute one contract-bound authenticated local action with a typed receipt.

    ``action_classes`` is an explicit operation-policy grant separate from the
    class declared by the intent and the enrolled identity grant. Raw browser
    control requires the full four-class mutation ceiling, explicitly named
    provider/contract/origins and an effective tool set exactly matching the
    intent; its semantic result stays unknown.
    """
    if (not isinstance(action_classes, list) or not action_classes
            or any(not isinstance(value, str) for value in action_classes)):
        raise ValueError("action_classes must be a nonempty list")
    options = _acquisition_overrides(action_policy, {
        "identity": identity, "provider": provider,
        "action_classes": tuple(action_classes)})
    for field in ("provider_candidates", "browser_do_allowed_origins",
                  "browser_do_allowed_tools",
                  "browser_do_allowed_contracts"):
        if isinstance(options.get(field), list):
            options[field] = tuple(options[field])
    async with runtime() as web:
        return await web.do(intent, WebPolicy(**options))


@server.tool()
async def repair(trace_id: str, repair_policy: dict | None = None) -> dict:
    """Diagnose one retained public read failure and validate an inert proposal.

    The operation never uses named identity authority and never promotes its
    output. Owner promotion remains an explicit local operator API/CLI action.
    """
    from .repair import RepairPolicy
    options = dict(repair_policy or {})
    for field in (
            "allowed_failure_codes", "allowed_attributes",
            "allowed_route_providers"):
        if isinstance(options.get(field), list):
            options[field] = tuple(options[field])
    async with runtime() as web:
        return await web.repair(trace_id, RepairPolicy(**options))


@server.tool()
async def paginate(url: str, adapter: str,
                   continuation_adapter: str | None = None,
                   identity: str | None = None, provider: str | None = None,
                   freshness: str | None = None, max_pages: int | None = None,
                   acquisition_policy: dict | None = None) -> dict:
    """Traverse pages with the requested leaf schemas and one aggregate receipt.

    Omitted settings permit version-bound bundled or operator routes. Direct null
    arguments mean omitted; acquisition_policy preserves explicit null, false and
    zero operational settings. Public read authority is unchanged.
    """
    options = _acquisition_overrides(acquisition_policy, {"identity": identity,
        "provider": provider, "freshness": freshness, "max_pages": max_pages})
    async with runtime() as web:
        result = await web.paginate(url, adapter,
            continuation_adapter=continuation_adapter, policy_overrides=options)
        result.pop("content", None)
        for page in result.get("pages", []):
            if isinstance(page, dict):
                page.pop("content", None)
        return result


@server.tool()
async def batch(urls: list[str], provider: str | None = None, adapter: str | None = None,
                identity: str | None = None, include_images: bool = False,
                freshness: str = "now") -> list[dict]:
    """Bounded batch retrieval with per-result receipts and named identity policy enforcement."""
    async with runtime() as web:
        results = await web.batch(urls, WebPolicy(provider=provider, identity=identity,
            include_images=include_images, freshness=freshness), adapter=adapter)
        for result in results:
            result.pop("content", None)
        return results


@server.tool()
async def search(query: str, source: str | None = None, limit: int = 10,
                 identity: str | None = None, provider: str | None = None,
                 site: str | None = None, exclude_domains: list[str] | None = None,
                 recency: str | None = None, region: str | None = None,
                 vertical: str | None = None, mode: str = "fallback",
                 acquisition_policy: dict | None = None) -> dict:
    """Discover indexed candidates through installed sources with exact attribution.

    Omit source to permit policy-bounded fallback. An explicit source is never
    substituted. Search source candidate, allow and preference fields can be set
    in acquisition_policy without exposing service credentials.

    site keeps one domain; exclude_domains (up to 200) never come back from any
    source; recency is day, week, month or year; region looks like en-AU. Each
    source applies them natively where it can and every list is filtered after
    (receipt.search_options says what was dropped). vertical picks the kind of
    source: web, news (Bing News), reference (Wikipedia), discussions (Hacker
    News), qa (Stack Overflow), code (GitHub), papers (arXiv), books (Open
    Library), site (one site's own feeds and sitemaps; pass site=). Leave
    vertical out and it is inferred from the query: platform words and "<language>
    library" or "library for" mean code; "papers on", arxiv or "survey of" mean
    papers; "news", headlines or "latest on" mean news (announcement and launch
    phrasing stays web); fact questions (who, what is, when was, how tall,
    capital of, who wrote) mean reference; an error or how-to with code tokens
    means qa; anything else is web. receipt.search_routing.vertical_inferred
    says what was picked and why; an explicit vertical always wins.
    mode="merge" asks every eligible source at once and fuses them.
    """
    config = ({"base_url": os.environ["FRANKENSURF_SEARCH_URL"]}
              if os.getenv("FRANKENSURF_SEARCH_URL") and source in (None, "searxng") else None)
    options = _acquisition_overrides(acquisition_policy,
        {"identity": identity, "provider": provider})
    for field in ("provider_candidates", "search_source_candidates", "search_source_allow",
                  "search_source_prefer", "search_terminal_failures"):
        if isinstance(options.get(field), list):
            options[field] = tuple(options[field])
    if ("provider" not in options and "provider_candidates" not in options
            and not options.get("identity")):
        options["provider"] = "http"
    async with runtime() as web:
        return await web.search(query, source=source, limit=limit, engine_config=config,
                                policy=WebPolicy(**options), site=site, exclude_domains=exclude_domains,
                                recency=recency, region=region, vertical=vertical, mode=mode)


@server.tool()
async def images(urls: list[str], identity: str | None = None,
                 max_images: int = 50, provider: str | None = None) -> list[dict]:
    """Download and decode exact image URLs under public or named identity policy; return private evidence references."""
    async with runtime() as web:
        return await web.download_images(urls, WebPolicy(identity=identity,
            max_images=max_images, provider=provider))


@server.tool()
async def identity_status(identity: str | None = None) -> dict:
    """Inspect safe identity/executor health and authority metadata. Enrollment is operator CLI only."""
    from .identity import IdentityFailure
    try:
        return runtime().identity_status(identity)
    except IdentityFailure as exc:
        return {"status": "failed", "failure": {"code": exc.code, "message": exc.message}}


@server.tool()
async def import_evidence(content: str, url: str, observed_at: str, content_type: str = "text/html", image_urls: list[str] | None = None) -> dict:
    """Import actual supplied operator browser evidence. Does not verify automated access or live availability."""
    async with runtime() as web:
        result = web.import_evidence(content, url, observed_at, content_type=content_type, image_urls=image_urls)
        result.pop("content", None)
        return result


@server.tool()
async def import_image_evidence(path: str, url: str, observed_at: str,
                                max_image_bytes: int = 20 * 1024 * 1024) -> dict:
    """Import a caller-selected public browser image file. Source binding is operator-supplied, never identity attestation."""
    from pathlib import Path
    from .runtime import WebFailure
    try:
        policy = WebPolicy(max_image_bytes=max_image_bytes)
        with Path(path).expanduser().open("rb") as source:
            raw = source.read(policy.max_image_bytes + 1)
        async with runtime() as web:
            return web.import_image_evidence(raw, url, observed_at, policy)
    except WebFailure as exc:
        return {"status": "failed", "failure": {"code": exc.code, "message": exc.message}}
    except (OSError, ValueError):
        return {"status": "failed", "failure": {"code": "INVALID_EVIDENCE", "message": "Image file or observation metadata is invalid"}}


@server.tool()
async def map_site(url: str, search: str | None = None, limit: int = 5000,
                   include_subdomains: bool = False) -> dict:
    """A site's URLs from its sitemaps and the start page's links, without reading them. search keeps
    the URLs that mention its words, best match first."""
    from .crawl import site_map
    async with runtime() as web:
        return await site_map(web, url, limit=limit, search=search, include_subdomains=include_subdomains)


@server.tool()
async def crawl(url: str, limit: int = 20, max_depth: int = 2, include_paths: list[str] | None = None,
                exclude_paths: list[str] | None = None, entire_domain: bool = False,
                acquisition_policy: dict | None = None) -> dict:
    """Read a site breadth-first from url: its sitemap and the links it leads to, up to max_depth hops
    and limit pages, only under url's path unless entire_domain. include_paths / exclude_paths are
    regular expressions on the path. Each page comes back as markdown with its status and trace_id;
    robots.txt is honoured. Pages are paced per site, so a big crawl takes a while: keep limit small."""
    from .crawl import crawl as crawl_site
    async with runtime() as web:
        return await crawl_site(web, url, limit=limit, max_depth=max_depth, include=include_paths,
                                exclude=exclude_paths, entire_domain=entire_domain,
                                policy_overrides=_acquisition_overrides(acquisition_policy, {}))


@server.tool()
async def trace(trace_id: str) -> dict:
    """The full record of one earlier read: every tool tried, failures, timings,
    completeness steps and evidence references. Page content is not included."""
    async with runtime() as web:
        result = web.trace(trace_id)
    result.pop("content", None)
    return result


@server.tool()
async def profiles() -> list[dict]:
    """List stored FrankenSurf profiles: names, sites, sharing level. Never sessions."""
    from .profiles import ProfileStore
    return ProfileStore().list()


def _with_key(app, api_key):
    """ASGI wrapper: every request needs Authorization: Bearer <api_key> (when one is set)."""
    from .firecrawl_compat import authorized

    async def guarded(scope, receive, send):
        if scope["type"] == "http":
            headers = {name.decode("latin-1").lower(): value.decode("latin-1")
                       for name, value in scope.get("headers") or []}
            if not authorized(headers, api_key):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body",
                            "body": b'{"error": "Unauthorized: send Authorization: Bearer <FRANKENSURF_API_KEY>"}'})
                return
        await app(scope, receive, send)
    return guarded


def main(argv=None):
    """frankensurf-mcp: MCP over stdio (what local clients start). --http serves it over the network
    (streamable HTTP at /mcp), for one Frankensurf shared by many machines, e.g. in Docker."""
    import argparse
    import os
    import sys
    parser = argparse.ArgumentParser(prog="frankensurf-mcp", description=main.__doc__)
    parser.add_argument("--http", action="store_true", help="serve MCP over HTTP instead of stdio")
    parser.add_argument("--host", default="127.0.0.1", help="--http: address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="--http: port (default 8000)")
    parser.add_argument("--api-key", help="--http: require this Bearer token (default $FRANKENSURF_API_KEY)")
    parser.add_argument("--allow-unauthenticated", action="store_true",
                        help="--http: listen beyond this machine without an API key")
    args = parser.parse_args(argv)
    if not args.http:
        server.run()
        return
    from .firecrawl_compat import network_guard
    api_key = args.api_key or os.getenv("FRANKENSURF_API_KEY") or None
    try:
        network_guard(args.host, api_key, args.allow_unauthenticated)
    except ValueError as error:
        print(f"frankensurf-mcp: {error}", file=sys.stderr)
        raise SystemExit(2) from None
    import logging
    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings
    # Every URL read (query strings included) would otherwise land in the server's logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    server.settings.host, server.settings.port = args.host, args.port
    # Clients reach a networked server by its LAN or container name, not localhost; the API key is
    # the guard there, so DNS-rebinding host checks would only block legitimate clients.
    server.settings.transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    print(f"Frankensurf MCP on http://{args.host}:{args.port}/mcp"
          + (" (Bearer API key required)" if api_key else ""), file=sys.stderr, flush=True)
    uvicorn.run(_with_key(server.streamable_http_app(), api_key), host=args.host, port=args.port,
                log_level="warning")
