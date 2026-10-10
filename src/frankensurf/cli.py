import argparse
import asyncio
import json
from .runtime import Runtime, WebPolicy, default_state_dir

_WEB_OPERATIONS = {"read", "extract", "paginate", "batch", "search", "images", "do"}
_OPERATOR_OPERATIONS = {"executor-enroll", "identity-enroll", "identity-status", "identity-revoke"}
_LOCAL_OPERATIONS = {"bot-auth-init", "bot-auth-directory"}
_PROFILE_OPERATIONS = {"profile-login", "profile-list", "profile-delete"}

_POLICY_ARGUMENTS = {
    "freshness": "freshness", "provider": "provider", "render": "render",
    "provider_candidates": "provider_candidates",
    "provider_max_attempts_per_candidate":
        "provider_max_attempts_per_candidate",
    "provider_retry_delay": "provider_retry_delay_seconds",
    "provider_retry_failures": "provider_retry_failures",
    "images": "include_images", "max_images": "max_images",
    "timeout": "timeout_seconds", "wait_selector": "wait_selector",
    "content_ready_selector": "content_ready_selector",
    "content_ready_timeout": "content_ready_timeout_seconds",
    "identity": "identity", "navigation_page": "navigation_page",
    "max_pages": "max_pages",
    "agent_task": "browser_agent_task",
    "capture_json": "capture_json_responses",
    "markdown": "prefer_markdown",
    "main": "main_content",
    "handoff": "allow_handoff",
    "lightning": "lightning",
    "real_browser": "allow_real_browser",
    "profile": "profile",
    "expect": "expect_terms",
    "card_images": "card_images",
    "scroll_screens": "scroll_screens",
    "search_source_candidates": "search_source_candidates",
    "search_source_allow": "search_source_allow",
    "search_source_prefer": "search_source_prefer",
    "search_max_attempts": "search_max_attempts",
    "search_source_timeout": "search_source_timeout_seconds",
    "grant_action": "action_classes",
    "action_origin": "browser_do_allowed_origins",
    "action_contract": "browser_do_allowed_contracts",
    "action_tool": "browser_do_allowed_tools",
}


def _policy_kwargs(args):
    # CLI omission permits operator defaults. Zero removes only the optional
    # per-source search timeout; other numeric zero values retain their meaning.
    values = {field: getattr(args, argument) for argument, field in _POLICY_ARGUMENTS.items()
              if getattr(args, argument) is not None}
    for field in ("provider_candidates", "provider_retry_failures", "expect_terms",
                  "search_source_candidates", "search_source_allow",
                  "search_source_prefer",
                  "action_classes", "browser_do_allowed_origins",
                  "browser_do_allowed_contracts", "browser_do_allowed_tools"):
        if field in values:
            values[field] = tuple(values[field])
    if values.get("search_source_timeout_seconds") == 0:
        values["search_source_timeout_seconds"] = None
    return values


def _operator(args):
    from .identity import IdentityRegistry
    registry = IdentityRegistry(args.identity_registry)
    if args.operation == "executor-enroll":
        return registry.enroll_executor(args.urls[0], endpoint=args.cdp_url,
            user_data_dir=args.user_data_dir, profile_directory=args.profile_directory,
            profile_ref=args.profile_ref, profile_version=args.profile_version,
            network_context=args.network_context, geography=args.geography)
    if args.operation == "identity-enroll":
        auth_check = None
        if args.auth_url:
            auth_check = {"url": args.auth_url,
                          "authenticated_selector": args.authenticated_selector}
            if args.login_selector:
                auth_check["login_selector"] = args.login_selector
        return registry.enroll_identity(args.urls[0], executor_id=args.executor_id,
            domains=args.domain, image_domains=args.image_domain,
            authority_mode=args.authority_mode, auth_check=auth_check,
            allowed_actions=tuple(args.allow_action or ("READ_AUTHENTICATED",)),
            snapshot_policy={"path_prefixes":args.snapshot_path_prefix,"root_selectors":args.snapshot_root} if args.owner_visible_snapshot else None)
    if args.operation == "identity-revoke":
        return registry.revoke(args.urls[0])
    return registry.status(args.urls[0] if args.urls else None)


def _bot_auth(args):
    from . import bot_auth
    if args.operation == "bot-auth-init":
        public = bot_auth.generate()
        return {"key_file": str(bot_auth.key_file()), "keyid": bot_auth.thumbprint(public),
                "directory": {"keys": [public]}, "serve_at": bot_auth.DIRECTORY_PATH,
                "next": "Serve the directory on your agent origin and set FRANKENSURF_SIGNATURE_AGENT to it"}
    jwk = bot_auth.load()
    if jwk is None:
        raise SystemExit("No Web Bot Auth key; run: frankensurf bot-auth-init")
    return bot_auth.directory(jwk)


async def _profile_op(args):
    from .profiles import ProfileStore, login
    store = ProfileStore()
    if args.operation == "profile-list":
        return store.list()
    if args.operation == "profile-delete":
        return {"deleted": store.delete(args.urls[0])}
    print("A browser window is opening. Sign in, then close the window to save the profile.",
          flush=True)
    profile = await login(store, args.urls[0], args.site, sharing=args.sharing,
                          timeout_seconds=args.login_timeout)
    return {"profile": profile.name, "sites": profile.sites, "sharing": profile.sharing,
            "version": profile.version, "cookies": len(profile.state.get("cookies", []))}


def _load_json(path, label):
    if path is None:
        return None
    try:
        with open(path, encoding="utf-8") as source:
            value = json.load(source)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError(label + " is not valid JSON") from None
    if not isinstance(value, dict):
        raise ValueError(label + " must contain an object")
    return value


def _one_site(sites):
    if len(sites or []) > 1:
        raise ValueError("search takes one --site")
    return sites[0] if sites else None


def _module_op(args):
    """frankensurf module list | show ID | add FILE | enable ID | disable ID | rm ID"""
    from pathlib import Path
    from .site_modules import SiteModuleRegistry
    registry = SiteModuleRegistry(Path(args.state).expanduser() / "routes" / "site-modules.json")
    action, rest = args.urls[0], args.urls[1:]
    if action == "list":
        return registry.inspect()
    if action == "add":
        return registry.put(_load_json(rest[0], "site module file"))
    if action == "show":
        module = registry.get(rest[0])
        return {**module.metadata(), "module": module.record()}
    if action in ("enable", "disable"):
        registry.enable(rest[0], action == "enable")
        return registry.get(rest[0]).metadata()
    registry.delete(rest[0])
    return {"deleted": rest[0]}


def _setup(args):
    """frankensurf setup: Chromium for JavaScript pages, plus the free stealth providers."""
    import subprocess
    import sys
    say = lambda message: print(message, file=sys.stderr, flush=True)
    say("Downloading Chromium for JavaScript pages")
    chromium = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"]).returncode == 0
    stealth = "skipped (--no-stealth)"
    if not args.no_stealth:
        from .experimental import install_free_providers
        try:
            stealth = install_free_providers(log=say)
        except (OSError, subprocess.CalledProcessError) as error:
            stealth = {"error": str(error)[:300]}
    result = {"chromium": chromium, "stealth_providers": stealth,
              "next": "frankensurf read https://news.ycombinator.com --explain"}
    from pathlib import Path
    if (Path.home() / ".claude").is_dir():
        # Claude Code is here: teach its agents the tools (frankensurf skill install for other folders).
        from . import skill
        result["skill"] = str(skill.install())
    if sys.platform.startswith("linux"):
        result["if_chromium_will_not_start"] = "sudo $(which python) -m playwright install-deps chromium"
    return result


def _explain(result):
    """A short human trail of one read: each tool tried, what happened, and the page."""
    receipt = result.get("receipt") or {}
    lines = [receipt.get("requested_url") or result.get("url") or ""]
    steps = list(receipt.get("attempts") or [])
    steps += [{"provider": step.get("provider"), "status": step.get("status"), "failure": step.get("failure"),
               "escalation": True} for step in (receipt.get("completeness") or {}).get("escalations") or ()]
    for step in steps:
        ok = step.get("status") == "observed"
        seconds = f"{step['latency_ms'] / 1000:.1f}s" if isinstance(step.get("latency_ms"), (int, float)) else ""
        what = "got the page" if ok else (step.get("failure") or "failed")
        tag = "  (completeness check)" if step.get("escalation") else ""
        lines.append(f"  {'✓' if ok else '✗'} {step.get('provider') or '?':<22} {what:<22} {seconds}{tag}")
    if receipt.get("status") == "observed":
        done = receipt.get("completeness") or {}
        text = " ".join((result.get("text") or "").split())
        cost = receipt.get("cost_usd")
        links = done.get("item_links") or 0
        lines += ["", f"{result.get('title') or '(no title)'}",
                  f"{len(text):,} characters via {receipt.get('method')}"
                  + ((f", complete ({links} results)" if links >= 10 else ", complete") if done.get("complete") else "")
                  + (", free" if cost == 0 else f", cost ${cost:.4f}" if isinstance(cost, (int, float)) else ""),
                  "", text[:400] + ("…" if len(text) > 400 else "")]
        if done.get("off_query"):
            lines.append("\n! These results don't mention the query: the search URL may be wrong.")
    else:
        failure = receipt.get("failure") or {}
        lines += ["", f"Failed: {failure.get('code')} {failure.get('message') or ''}".rstrip()]
        if receipt.get("next_step"):
            lines.append(f"Next step: {receipt['next_step'].get('how') or receipt['next_step']}")
    lines += ["", f"trace {receipt.get('trace_id')}  (frankensurf trace <id> for the full receipt)"]
    return "\n".join(lines)


def _template_params(pairs):
    params = {}
    for pair in pairs or ():
        name, separator, value = pair.partition("=")
        if not separator or not name:
            raise ValueError("--param takes NAME=VALUE")
        params[name] = value
    return params


async def run(args):
    if args.operation in _PROFILE_OPERATIONS:
        result = await _profile_op(args)
    elif args.operation == "setup":
        result = _setup(args)
    elif args.operation == "egress":
        from .egress import check
        result = {"connections": await check(),
                  "note": "direct is this machine; the rest come from FRANKENSURF_EGRESS"}
    elif args.operation == "skill":
        from . import skill
        if args.urls and args.urls[0] == "show":
            print(skill.text())
            return
        result = {"installed": str(skill.install(args.skill_dir)),
                  "note": "Agents that load skills from this folder now know how to use FrankenSurf"}
    elif args.operation == "module" and args.urls[0] == "discover":
        async with Runtime(state_dir=args.state) as web:
            result = await web.discover_module(args.urls[1], save=args.save, query=args.query)
    elif args.operation == "module" and args.urls[0] == "repair":
        async with Runtime(state_dir=args.state) as web:
            result = await web.propose_module_repair(args.urls[1], _load_json(args.urls[2], "site module file"))
    elif args.operation == "module":
        result = _module_op(args)
    elif args.operation in _LOCAL_OPERATIONS:
        result = _bot_auth(args)
    elif args.operation in _OPERATOR_OPERATIONS:
        result = _operator(args)
    else:
        policy_kwargs = _policy_kwargs(args)
        policy = WebPolicy(**policy_kwargs)
        async with Runtime(state_dir=args.state, steel_api_url=args.steel_url,
                           local_cdp_url=args.cdp_url,
                           identity_registry=args.identity_registry) as web:
            if args.operation == "search":
                config = {"base_url": args.search_url} if args.search_url else None
                search_policy = policy if any(field in policy_kwargs for field in (
                    "provider", "provider_candidates", "identity")) else WebPolicy(
                    **{**policy_kwargs, "provider": "http"})
                result = await web.search(" ".join(args.urls), source=args.source,
                    limit=args.limit, engine_config=config, policy=search_policy,
                    site=_one_site(args.site), exclude_domains=args.exclude_domains, recency=args.recency,
                    region=args.region, vertical=args.vertical, mode=args.mode)
            elif args.operation == "do":
                with open(args.intent_file, encoding="utf-8") as source:
                    raw = source.read(policy.browser_do_packet_max_bytes + 1)
                if len(raw.encode()) > policy.browser_do_packet_max_bytes:
                    raise ValueError("Action intent file exceeds WebPolicy")
                result = await web.do(json.loads(raw), policy)
            elif args.operation == "images":
                result = await web.download_images(args.urls, policy)
            elif args.operation == "import":
                with open(args.evidence_file) as evidence:
                    result = web.import_evidence(evidence.read(), args.urls[0],
                        args.observed_at, content_type=args.content_type)
            elif args.operation == "trace":
                result = web.trace(args.urls[0])
            elif args.operation == "batch":
                result = await web.batch(args.urls, policy, adapter=args.adapter)
            elif args.operation == "paginate":
                result = await web.paginate(
                    args.urls[0], args.adapter,
                    continuation_adapter=args.continuation_adapter,
                    policy_overrides=policy_kwargs)
            elif args.operation == "extract":
                assertions = _load_json(
                    args.assertions_file, "assertions file")
                extract_options = {"policy_overrides": policy_kwargs}
                if assertions is not None:
                    extract_options["workload_assertions"] = assertions
                result = await web.extract(
                    args.urls[0], args.adapter or "html", **extract_options)
            elif args.operation == "repair":
                from .repair import RepairPolicy
                repair_options = _load_json(
                    args.repair_policy_file, "repair policy file") or {}
                for field in (
                        "allowed_failure_codes", "allowed_attributes",
                        "allowed_route_providers"):
                    if isinstance(repair_options.get(field), list):
                        repair_options[field] = tuple(repair_options[field])
                result = await web.repair(
                    args.urls[0], RepairPolicy(**repair_options))
            elif args.operation == "repair-promote":
                result = web.promote_repair(
                    args.urls[0], args.proposal_sha256)
            elif args.operation == "repair-disable":
                result = web.disable_repair(
                    args.urls[0], args.disable_reason)
            elif args.operation == "watch-sites":
                result = await web.watch_sites(list(args.urls), since=args.since, read_new=args.read_new)
            elif args.operation == "watch":
                result = await web.watch(args.urls[0], link_pattern=args.link_pattern,
                                         policy_overrides=policy_kwargs)
            elif args.operation == "read-template":
                result = await web.read_template(args.urls[0], args.urls[1],
                                                 _template_params(args.param),
                                                 policy_overrides=policy_kwargs)
            else:
                extra = {"retry_of": args.try_harder_than} if args.try_harder_than else {}
                if args.module is not None:
                    extra["module"] = False if args.module == "none" else args.module
                result = await web.read(args.urls[0], policy_overrides=policy_kwargs, **extra)
    if not args.raw:
        for entry in result if isinstance(result, list) else [result]:
            entry.pop("content", None)
            for page in entry.get("pages", []) if isinstance(entry, dict) else []:
                if isinstance(page, dict):
                    page.pop("content", None)
    if getattr(args, "explain", False) and isinstance(result, dict) and "receipt" in result:
        print(_explain(result))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def build_parser():
    from .adapters import DEFAULT_ADAPTERS
    from .providers import DEFAULT_PROVIDERS
    provider_ids = [item["id"] for item in DEFAULT_PROVIDERS.inspect()]
    parser = argparse.ArgumentParser(description="Free local evidence-first web runtime")
    parser.add_argument("operation", choices=["read", "extract", "paginate", "batch", "trace",
        "search", "images", "do", "import", "repair", "repair-promote", "watch", "watch-sites",
        "repair-disable", "executor-enroll", "identity-enroll",
        "identity-status", "identity-revoke", "bot-auth-init", "bot-auth-directory",
        "profile-login", "profile-list", "profile-delete", "module", "read-template", "setup", "skill",
        "egress"])
    parser.add_argument("urls", nargs="*")
    parser.add_argument("--explain", action="store_true",
        help="read: print each tool tried and what happened, then the page, instead of JSON")
    parser.add_argument("--no-stealth", action="store_true",
        help="setup: skip the free stealth providers (Camoufox, Scrapling, Patchright)")
    parser.add_argument("--query", help="module discover: from a home page, search this and draft from the results")
    parser.add_argument("--since", help="watch-sites: only pages published since this ISO date (first poll)")
    parser.add_argument("--read-new", action="store_true", help="watch-sites: also read the new pages' main text")
    parser.add_argument("--skill-dir", help="skill install: the skills folder (default ~/.claude/skills)")
    parser.add_argument("--save", action="store_true",
        help="module discover: save the best drafted module")
    parser.add_argument("--module", metavar="ID",
        help="read: shape the read with this saved site module, or 'none' to turn modules off")
    parser.add_argument("--param", action="append", metavar="NAME=VALUE",
        help="read-template: a template parameter; repeat for each")
    parser.add_argument("--link-pattern",
        help="watch: regular expression a link URL must match to count as an item")
    parser.add_argument("--state", default=default_state_dir(),
        help="state directory (default: $FRANKENSURF_STATE or ~/.local/share/frankensurf/state)")
    parser.add_argument("--provider", choices=provider_ids)
    parser.add_argument("--provider-candidate", dest="provider_candidates", action="append",
        choices=provider_ids, help="Ordered acquisition provider candidate; repeat to set the route")
    parser.add_argument("--provider-max-attempts-per-candidate", type=int,
        help="Maximum Core-receipted attempts for each provider candidate")
    parser.add_argument("--provider-retry-delay", type=float,
        help="Seconds between retryable provider attempts")
    parser.add_argument("--provider-retry-failure", dest="provider_retry_failures",
        action="append", help="Retryable typed provider failure; repeat as needed")
    parser.add_argument("--steel-url")
    parser.add_argument("--cdp-url")
    parser.add_argument("--identity", help="Named local identity; authority is checked before retrieval")
    parser.add_argument("--intent-file",
        help="Private JSON action intent for web.do; field values are not copied into receipts")
    parser.add_argument("--identity-registry", help="Operator registry path; defaults to FRANKENSURF_IDENTITIES or the user registry")
    parser.add_argument("--freshness", choices=["now", "hour", "day", "cached"])
    parser.add_argument(
        "--adapter", choices=[item["id"] for item in DEFAULT_ADAPTERS.inspect()])
    parser.add_argument("--continuation-adapter", choices=[item["id"] for item in DEFAULT_ADAPTERS.inspect()])
    parser.add_argument("--render", action="store_true", default=None)
    parser.add_argument("--images", action="store_true", default=None)
    parser.add_argument("--max-images", type=int)
    parser.add_argument("--card-images", action="store_true", default=None,
        help="Add cards: each result link with its title and its own thumbnail URL")
    parser.add_argument("--scroll-screens", type=int,
        help="Scroll this many screens before capture so lazy content loads")
    parser.add_argument("--expect", action="append",
        help="A word the search results should mention; repeat for more")
    parser.add_argument("--agent-task",
        help="Task for the browser agent, e.g. 'search for sony a7iii'; the agent may finish on any allowed page")
    parser.add_argument("--main", action="store_true", default=None,
        help="Also return main_text: the article without menus, footers and banners")
    parser.add_argument("--markdown", action="store_true", default=None,
        help="Ask servers for text/markdown first (content negotiation)")
    parser.add_argument("--profile", help="Read with a stored FrankenSurf profile (see profile-login)")
    parser.add_argument("--try-harder-than", metavar="TRACE_ID",
        help="read: skip every tool an earlier read used and start from the strongest remaining one")
    parser.add_argument("--site", action="append", default=[],
        help="profile-login: a site the profile covers; repeat for each."
             " search: only results from this one domain")
    parser.add_argument("--sharing", choices=["local", "hosted"], default="local",
        help="profile-login: which providers may carry the session")
    parser.add_argument("--login-timeout", type=float, default=600,
        help="profile-login: seconds to wait for the window to close")
    parser.add_argument("--handoff", action="store_true", default=None,
        help="If every automatic route fails, open the page for you to clear, then resume")
    parser.add_argument("--lightning", action="store_true", default=None,
        help="Start the read and two other tools at once and keep the first complete page"
             " (faster; up to three requests to the site at once)")
    parser.add_argument("--real-browser", action="store_true", default=None,
        help="When walls keep blocking, try your real Chrome or Edge (a real window kept off your"
             " screen; FrankenSurf's own profile, never yours; it never clicks)")
    parser.add_argument("--capture-json", action="store_true", default=None,
        help="Capture JSON the page's own frontend fetched (rendered and identity reads)")
    parser.add_argument("--max-pages", type=int)
    parser.add_argument("--navigation-page",type=int,choices=range(1,4),help="Carsales page selection through normal Next navigation")
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--wait-selector")
    parser.add_argument("--content-ready-selector",
        help="Preferred semantic-ready CSS selector; timeout captures current truth")
    parser.add_argument("--content-ready-timeout", type=float,
        help="Bounded seconds to wait for --content-ready-selector")
    from .search_plugins import DEFAULT_SEARCHES
    search_sources = [item["id"] for item in DEFAULT_SEARCHES.inspect()]
    parser.add_argument("--exclude-domain", dest="exclude_domains", action="append",
        help="search: never return results from this domain; repeat for more")
    parser.add_argument("--recency", choices=["day", "week", "month", "year"],
        help="search: only results from the last day, week, month or year")
    parser.add_argument("--region", help="search: region and language, like en-AU")
    parser.add_argument("--vertical", default="web",
        choices=["web", "news", "reference", "discussions", "qa", "code", "papers", "books"],
        help="search: the kind of source (default web)")
    parser.add_argument("--mode", default="fallback", choices=["fallback", "merge"],
        help="search: merge asks every eligible source at once and fuses the results")
    parser.add_argument("--source", choices=search_sources,
        help="Explicit single search source; omission permits bounded source fallback")
    parser.add_argument("--search-source-candidate", dest="search_source_candidates",
        action="append", choices=search_sources, help="Ordered search-source candidate; repeat to set the route")
    parser.add_argument("--search-source-allow", dest="search_source_allow",
        action="append", choices=search_sources, help="Allowed search source; repeat as needed")
    parser.add_argument("--search-source-prefer", dest="search_source_prefer",
        action="append", choices=search_sources, help="Preferred search source; repeat in priority order")
    parser.add_argument("--search-max-attempts", type=int,
        help="Maximum search sources attempted for an omitted --source")
    parser.add_argument("--search-source-timeout", type=float,
        help="Per-source search timeout in seconds; zero uses the operation timeout")
    parser.add_argument("--search-url")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--evidence-file")
    parser.add_argument("--observed-at")
    parser.add_argument("--content-type", default="text/html")
    parser.add_argument("--assertions-file",
        help="Closed-schema workload assertions retained for extract repair")
    parser.add_argument("--repair-policy-file",
        help="RepairPolicy JSON for the explicit repair operation")
    parser.add_argument("--proposal-sha256",
        help="Exact proposal artifact hash required for owner promotion")
    parser.add_argument("--disable-reason",
        help="Audited reason for disabling an active repair overlay")
    parser.add_argument("--raw", action="store_true")
    parser.add_argument("--executor-id", help="Registered local executor used by identity-enroll")
    parser.add_argument("--domain", action="append", default=[], help="Allowed page domain; repeat for each domain")
    parser.add_argument("--image-domain", action="append", default=[], help="Allowed image/CDN domain; repeat for each domain")
    parser.add_argument("--authority-mode", default="LOCAL_ONLY",
        choices=["LOCAL_ONLY", "SYNC_ALLOWED", "CLOUD_MANAGED", "EPHEMERAL"],
        help="Only LOCAL_ONLY executes in this release; other modes may be registered for future backends")
    from .actions import ACTION_CLASSES
    from .web_do import ACTION_CONTRACTS, ACTION_TOOLS
    parser.add_argument("--grant-action", action="append",
        choices=ACTION_CLASSES,
        help="Action class granted by this operation policy; required for web.do")
    parser.add_argument("--action-origin", action="append",
        help="Exact HTTP(S) origin permitted for web.do; repeat as needed")
    parser.add_argument("--action-contract", action="append",
        choices=tuple(ACTION_CONTRACTS),
        help="Core operation contract permitted for web.do")
    parser.add_argument("--action-tool", action="append",
        choices=ACTION_TOOLS, help="Low-level browser tool permitted for web.do")
    parser.add_argument("--allow-action", action="append", choices=ACTION_CLASSES,
        help="Action class granted to this identity; repeat as needed (default: READ_AUTHENTICATED)")
    parser.add_argument("--user-data-dir", help="Canonical absolute Chrome user-data directory; never exported")
    parser.add_argument("--profile-directory", default="Default")
    parser.add_argument("--profile-ref", help="Opaque canonical profile identifier")
    parser.add_argument("--profile-version", type=int, default=1)
    parser.add_argument("--network-context", default="local")
    parser.add_argument("--geography")
    parser.add_argument("--auth-url", help="Domain-scoped page used to validate authenticated state")
    parser.add_argument("--authenticated-selector", help="Expected authenticated-state selector on --auth-url")
    parser.add_argument("--login-selector", help="Optional expired-login selector on --auth-url")
    parser.add_argument("--owner-visible-snapshot", action="store_true", help="Operator opt-in: observe an exact already-open URL without navigation")
    parser.add_argument("--snapshot-path-prefix", action="append", default=[], help="Allowed visible page path prefix, ending in /; repeat as needed")
    parser.add_argument("--snapshot-root", action="append", default=[], help="Visible listing-region selector; first matching visible region is captured")
    return parser


def parse_args(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.operation == "profile-list":
        if args.urls:
            parser.error("profile-list takes no arguments")
    elif args.operation in {"profile-login", "profile-delete"}:
        if len(args.urls) != 1:
            parser.error(args.operation + " takes one profile name")
        if args.operation == "profile-login" and not args.site:
            parser.error("profile-login needs at least one --site")
    elif args.operation in _LOCAL_OPERATIONS:
        if args.urls:
            parser.error(args.operation + " takes no arguments")
    elif args.operation == "module":
        action = args.urls[0] if args.urls else None
        if action == "list" and len(args.urls) == 1:
            pass
        elif action == "repair" and len(args.urls) == 3:
            pass
        elif action == "discover" and len(args.urls) == 2:
            pass
        elif action not in {"show", "add", "enable", "disable", "rm"} or len(args.urls) != 2:
            parser.error("module takes: list | show ID | add FILE | enable ID | disable ID | rm ID"
                         " | repair TRACE_ID FILE | discover URL [--save]")
    elif args.operation == "skill":
        if args.urls not in ([], ["install"], ["show"]):
            parser.error("skill takes: install [--skill-dir DIR] | show")
    elif args.operation == "egress":
        if args.urls != ["check"]:
            parser.error("egress takes: check")
    elif args.operation == "setup":
        if args.urls:
            parser.error("setup takes no arguments (add --no-stealth to skip the stealth providers)")
    elif args.operation == "read-template":
        if len(args.urls) != 2:
            parser.error("read-template takes MODULE_ID TEMPLATE (and --param NAME=VALUE)")
    elif args.operation == "identity-status":
        if len(args.urls) > 1:
            parser.error("identity-status accepts at most one identity ID")
    elif args.operation in {"batch", "images", "search", "watch-sites"}:
        if not args.urls:
            parser.error(f"{args.operation} requires at least one URL or query token")
    elif args.operation == "do":
        if args.urls:
            parser.error("do accepts its target only through --intent-file")
        if not (args.intent_file and args.identity and args.grant_action
                and args.action_origin):
            parser.error("do requires --intent-file, --identity, --grant-action and --action-origin")
    elif len(args.urls) != 1:
        parser.error(f"{args.operation} requires exactly one URL, trace ID or registration ID")
    if args.identity and args.operation not in _WEB_OPERATIONS:
        parser.error("--identity is supported only for read, extract, paginate, batch, search, images and do")
    if args.assertions_file and args.operation != "extract":
        parser.error("--assertions-file is supported only for extract")
    if args.repair_policy_file and args.operation != "repair":
        parser.error("--repair-policy-file is supported only for repair")
    if args.operation == "repair-promote" and not args.proposal_sha256:
        parser.error("repair-promote requires --proposal-sha256")
    if args.proposal_sha256 and args.operation != "repair-promote":
        parser.error("--proposal-sha256 is supported only for repair-promote")
    if args.operation == "repair-disable" and not args.disable_reason:
        parser.error("repair-disable requires --disable-reason")
    if args.disable_reason and args.operation != "repair-disable":
        parser.error("--disable-reason is supported only for repair-disable")
    if args.operation == "paginate" and not args.adapter:
        parser.error("paginate requires --adapter")
    if args.continuation_adapter and args.operation != "paginate":
        parser.error("--continuation-adapter is supported only for paginate")
    if args.operation == "import" and not (args.evidence_file and args.observed_at):
        parser.error("import requires --evidence-file and --observed-at")
    if args.operation == "executor-enroll" and not (args.cdp_url and args.user_data_dir):
        parser.error("executor-enroll requires --cdp-url and --user-data-dir")
    if args.operation == "identity-enroll":
        if not args.executor_id or not args.domain:
            parser.error("identity-enroll requires --executor-id and at least one --domain")
        if any([args.auth_url, args.authenticated_selector, args.login_selector]) and not (args.auth_url and args.authenticated_selector):
            parser.error("authentication validation requires --auth-url and --authenticated-selector")
        if args.owner_visible_snapshot and not (args.snapshot_path_prefix and args.snapshot_root):
            parser.error("visible snapshot requires explicit path prefixes and region selectors")
        if not args.owner_visible_snapshot and (args.snapshot_path_prefix or args.snapshot_root):
            parser.error("snapshot configuration requires --owner-visible-snapshot")
    return args


def main(argv=None):
    args = parse_args(argv)
    try:
        asyncio.run(run(args))
    except Exception as exc:
        from .identity import IdentityFailure
        if isinstance(exc, IdentityFailure):
            print(json.dumps({"status": "failed", "failure": {"code": exc.code,
                "message": exc.message}}, ensure_ascii=False))
            raise SystemExit(1) from None
        if args.operation in _OPERATOR_OPERATIONS and isinstance(exc, (ValueError, OSError)):
            print(json.dumps({"status": "failed", "failure": {"code": "INVALID_REGISTRATION",
                "message": "Identity registry operation failed; verify the registration and local registry access"}}))
            raise SystemExit(1) from None
        raise
