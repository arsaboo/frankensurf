from __future__ import annotations

import asyncio
import ipaddress
import copy
import hashlib
import html
import io
import math
import ntpath
from contextlib import nullcontext
from contextvars import ContextVar
import json
import os
import re
import statistics
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal
from urllib.parse import unquote_plus, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from PIL import Image, UnidentifiedImageError

from .identity import IdentityRegistry, IdentityFailure


_PLUGIN_ID = re.compile(r"[a-z][a-z0-9_.-]{0,127}")
_PLUGIN_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+:-]{0,191}")
_STAGE_IMAGE_EVIDENCE = ContextVar("frankensurf_stage_image_evidence", default=False)
# Tools racing the read already running for this URL: (url, set of tool ids). The
# main read's plan skips them so one tool never reads the same page twice at once.
_RACE_CLAIMED = ContextVar("frankensurf_race_claimed", default=None)


def _valid_plugin_id(value):
    return isinstance(value, str) and _PLUGIN_ID.fullmatch(value) is not None


@dataclass(frozen=True)
class _IdentityProviderAuthority:
    """The exact startup-catalog binding approved for one named identity."""
    provider_id: str
    version: str
    binding_id: str
    operation: str


@dataclass(frozen=True)
class _IdentityCaptureStrategy:
    """Exact Core strategy implementation pinned for one Runtime."""
    id: str
    version: str
    binding_id: str
    health: object
    acquire: object
    current_binding_id: object


@dataclass
class _AuthenticatedProviderCapability:
    """Core-owned one-shot state; this object is never exposed to plugins."""
    authority: _IdentityProviderAuthority
    identity_scope: str
    url: str
    policy: object
    capture_strategy: _IdentityCaptureStrategy | None = None
    state: str = "unused"
    response_fingerprint: str | None = None
    capture_metadata: dict | None = None
    private_image_sources: dict | None = None


@dataclass
class _AuthenticatedActionCapability:
    """Core-owned one-shot authority for an exact catalogued action run."""
    authority: _IdentityProviderAuthority
    identity_scope: str
    required_action_classes: tuple[str, ...]
    request: object
    state: str = "unused"
    response_fingerprint: str | None = None


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class WebPolicy:
    # Operation authority is explicit. Read grants never imply any write,
    # purchase, or account-security authority.
    action_classes: tuple[str, ...] = ("READ_PUBLIC", "READ_AUTHENTICATED")
    freshness: Literal["now", "hour", "day", "cached"] = "now"
    provider: str | None = None
    provider_candidates: tuple[str, ...] | None = None
    compound_source_candidates: tuple[str, ...] = ("http", "camoufox", "scrapling")
    render: bool = False
    # An unrendered HTML page with scripts and less text than this is an app
    # shell; a rendered page with less than rendered_min_text_chars is empty
    # (a failed render or a silent challenge). Both move on to the next provider.
    html_shell_min_text_chars: int = 200
    rendered_min_text_chars: int = 50
    # A large unrendered page with little text is a script-built shell too:
    # an Airbnb room page is 650 KB of HTML carrying 423 characters of text.
    html_shell_large_bytes: int = 250_000
    html_shell_large_min_text_chars: int = 1000
    # An automatic plain-HTTP page with scripts and less text than this also
    # gets a rendered read; the page with clearly more content is returned
    # (see Runtime._second_opinion). 0 disables.
    second_opinion_text_chars: int = 5000
    # After an automatic read, check the page's structure (see completeness.py).
    # A search page without item links or prices, or a page with almost no
    # text, is re-read with the next providers on this ladder (paid ones only
    # with allow_paid_fallbacks), keeping the most complete page.
    completeness_escalation: bool = True
    completeness_ladder: tuple[str, ...] = (
        "jina_reader", "scrapling", "camoufox", "firecrawl", "zyte", "zenrows",
        "scrapfly", "patchright", "brightdata_unlocker")
    completeness_max_extra_reads: int = 5
    # Free ladder tools tried at once while escalating (the first complete page
    # wins, the rest are cancelled). Paid tools always run one at a time.
    completeness_parallel: int = 2
    # Stop escalating once two tools have read the same page as the first one.
    completeness_stop_on_agreement: bool = True
    # Smart lightning: when an automatic public read has no page after this many
    # seconds, hedge_racers reads pinned to the first free tools on
    # completeness_ladder start beside it; the first complete page wins and the
    # rest are cancelled. Easy pages finish first and never race. 0 turns it off.
    hedge_after_seconds: float = 3.0
    hedge_racers: int = 2
    # Lightning mode: the read and up to two different free tools (the first
    # ones on completeness_ladder) start at once, the first complete page wins,
    # and escalation races four tools. Faster first reads, but a site sees up to
    # three requests at once. FRANKENSURF_LIGHTNING=1 turns it on for every read.
    lightning: bool = False
    # A search page that passes with fewer item links than this (and few
    # prices) also gets one read from the first allowed ladder tool.
    completeness_borderline_items: int = 20
    # Escalation reads exist because a page looked unfinished, so they give
    # script-rendered content at least this long to appear.
    completeness_settle_ms: int = 5000
    # Words a search page's results should mention, on top of the query in its
    # URL. A complete-looking results page that mentions none of them is
    # flagged off_query instead of escalated (see completeness.py).
    expect_terms: tuple[str, ...] = ()
    # Pair each result card's link with its own image in the same load
    # (result["cards"]). Browsers scroll scroll_screens viewport heights first
    # so lazy thumbnails load; card_images alone scrolls 3.
    card_images: bool = False
    scroll_screens: int = 0
    # The calling agent's "try harder" lever (see Runtime.read retry_of): skip
    # these providers, and order the rest strongest-first.
    exclude_providers: tuple[str, ...] = ()
    try_harder: bool = False
    retry_of_trace: str | None = None
    completeness_deadline_seconds: float = 150.0
    # Once a read holds a page, one extra tool trying to complete it gets this
    # long before it is cut off (and the climb's deadline cuts off a running
    # tool too). Measured: only Zyte ever passed 45 s, mostly timing out.
    completeness_step_timeout_seconds: float = 45.0
    # Hosted unblockers render through residential proxies and need longer than
    # an ordinary provider attempt.
    unblocker_timeout_seconds: float = 90.0
    # Hosted agent providers (Skyvern) run a whole browser task.
    agent_provider_timeout_seconds: float = 300.0
    include_images: bool = False
    retain_public_failure_evidence: bool = True
    max_images: int = 50
    timeout_seconds: float = 25
    provider_deadline_grace_seconds: float = 10
    provider_cleanup_grace_seconds: float = 5
    provider_composition_max_depth: int = 8
    provider_composition_max_attempts: int = 64
    provider_max_attempts_per_candidate: int = 2
    provider_retry_delay_seconds: float = 1.0
    provider_retry_failures: tuple[str, ...] = ("PROVIDER_DOWN",)
    max_bytes: int = 40 * 1024 * 1024
    max_image_bytes: int = 20 * 1024 * 1024
    workload_assertion_max_count: int = 32
    workload_assertion_max_bytes: int = 64 * 1024
    repair_overlay_registry_max_bytes: int = 4 * 1024 * 1024
    image_max_attempts: int = 2
    image_retry_delay_seconds: float = 0.25
    image_retry_failures: tuple[str, ...] = ("PROVIDER_DOWN", "TIMEOUT")
    allow_local_browser: bool = True
    allow_paid_fallbacks: bool = False
    # A stored copy (Internet Archive) as the very last resort. The result is
    # not live: result.archived and receipt.archived say when it was taken.
    allow_archive: bool = False
    # Refuse loopback, private, link-local and local-only addresses (and names
    # that resolve to them, and redirects into them). For servers reading URLs
    # they did not choose; FRANKENSURF_BLOCK_PRIVATE_NETWORK=1 sets it for all reads.
    block_private_network: bool = False
    # Also return main_text: the article without menus, footers and banners
    # (main_content.py; trafilatura when installed).
    main_content: bool = False
    # Safe read-only browser actions on browser reads (interactions.py):
    # dismiss_consent (reject, never accept), load_more, reveal.
    interactions: tuple[str, ...] = ()
    # When a page needs a choice or is only a consent notice, try one
    # local-browser read with those actions first.
    interact_on_escalation: bool = True
    # A public read that ends at a wall (BLOCKED, CAPTCHA) gets one more try:
    # a stealth browser that enters through the site's home page first, as a
    # person arrives, instead of landing cold on the deep link.
    warm_up_on_wall: bool = True
    # The local browser keeps each site's cookies (a challenge it passed, the
    # consent it rejected) for this many hours; 0 turns it off.
    site_session_hours: float = 24.0
    # Items from any listing page with no saved module: the page's own data is
    # searched for its item list (module discovery, no extra read) and the
    # draft module comes back too, ready to save.
    auto_items: bool = False
    max_cost_usd: float | None = None
    search_source_candidates: tuple[str, ...] | None = None
    search_source_allow: tuple[str, ...] | None = None
    search_source_prefer: tuple[str, ...] = ()
    search_max_attempts: int | None = None
    search_source_timeout_seconds: float | None = 8
    # Sources asked at once by search(mode="merge").
    search_merge_sources: int = 4
    # The search ladder: an automatic search climbs sources until one result set
    # passes the checks (search_checks: on the query, inside the date window, real
    # pages, deduplicated; search_good_results of them, or the limit if lower)
    # instead of stopping at the first answer, carrying the passing results of
    # weak sets up with it. False keeps the first answer.
    search_ladder: bool = True
    search_good_results: int = 3
    # When the source being tried has no answer after this many seconds, the next
    # free source starts beside it (up to search_hedge_racers at once); the first
    # set that passes wins and the rest are cancelled. 0 turns it off.
    search_hedge_after_seconds: float = 3.0
    search_hedge_racers: int = 2
    # Whole-search ceilings: seconds, and sources started.
    search_max_seconds: float = 30.0
    search_max_sources: int = 6
    # With allow_paid_fallbacks, paid search sources (cheapest likely success
    # first) join the ladder once this many free sources have come back weak or
    # empty; 0 leaves them until every free source has been tried.
    search_paid_after_weak: int = 2
    # Try first the source that most often passed the checks for this kind of query
    # (vertical, recency or not, site filter or not), learned on this machine.
    search_learned_routing: bool = True
    # Most -site: operators sent to an engine; the whole exclude list is enforced
    # on the results either way.
    search_exclude_operators: int = 3
    search_terminal_failures: tuple[str, ...] = ("POLICY_DENIED", "BUDGET_EXHAUSTED")
    browser_agent_max_steps: int = 40
    browser_agent_max_model_calls: int = 40
    browser_agent_max_actions: int = 80
    browser_agent_max_actions_per_step: int = 3
    browser_agent_max_failures: int = 3
    browser_agent_allowed_actions: tuple[str, ...] = ("navigate_public", "inspect_page", "follow_link", "click_element", "search_site", "wait_readiness", "scroll_page", "done")
    browser_agent_allowed_origins: tuple[str, ...] | None = None
    browser_agent_entry_url: str | None = None
    # A caller task ("search for X and open the first result"). When set,
    # the agent may finish on any page inside its allowed origins.
    browser_agent_task: str | None = None
    browser_agent_llm_timeout_seconds: float = 20
    browser_agent_step_timeout_seconds: float = 30
    browser_agent_action_timeout_seconds: float = 10
    browser_agent_readiness_poll_ms: int = 100
    browser_agent_packet_max_bytes: int = 48 * 1024 * 1024
    browser_agent_use_vision: bool = False
    # Authenticated ``web.do`` uses a separate tool surface. Public read actions
    # remain read-only even when an operation policy grants a write class.
    browser_do_allowed_tools: tuple[str, ...] = (
        "fill", "click", "wait_for", "assert_text", "assert_value")
    browser_do_allowed_contracts: tuple[str, ...] = (
        "local_fixture.reversible_draft.v1",)
    browser_do_allowed_origins: tuple[str, ...] | None = None
    browser_do_max_actions: int = 20
    browser_do_action_timeout_seconds: float = 10
    browser_do_settle_ms: int = 200
    browser_do_packet_max_bytes: int = 48 * 1024 * 1024
    browser_do_journal_max_bytes: int = 16 * 1024 * 1024
    browser_do_selector_max_bytes: int = 4096
    browser_do_value_max_bytes: int = 1024 * 1024
    browser_do_description_max_bytes: int = 8192
    browser_do_idempotency_key_max_bytes: int = 256
    browser_do_require_auth_check: bool = True
    identity: str | None = None
    # A FrankenSurf profile (profiles.py): a stored login any capable provider
    # can carry. Exclusive with identity, which reads through the owner's Chrome.
    profile: str | None = None
    wait_selector: str | None = None
    wait_state: str = "attached"
    # A required wait fails acquisition. A content-readiness wait is a bounded
    # semantic preference: timeout still captures the page so an adapter can
    # preserve unknown rather than turning missing readiness into truth.
    content_ready_selector: str | None = None
    content_ready_timeout_seconds: float = 10
    settle_ms: int = 400
    navigation_page: int = 1
    scrapling_navigation_wait_until: str | None = None
    scrapling_solve_cloudflare: bool = False
    scrapling_load_dom: bool = True
    scrapling_google_search: bool = False
    public_entry_url: str | None = None
    public_entry_continue_failures: tuple[str, ...] = ("BLOCKED",)
    public_browser_headless: bool = True
    max_pages: int = 2
    use_route_memory: bool = True
    # Start a fresh install's wall-vendor routing from measured priors
    # (bundled_wall_priors.json); this machine's own reads outweigh them quickly.
    use_wall_priors: bool = True
    route_memory_ttl_seconds: float = 3600
    route_memory_min_samples: int = 3
    terminal_failures: tuple[str, ...] = ("AUTH_REQUIRED", "AUTH_EXPIRED", "NOT_FOUND")
    # Polite pacing is the default. Reads to one origin (per identity) are spaced
    # by at least this many seconds, and a block, challenge or rate limit pauses
    # that origin for the cool-down, across processes sharing a state directory.
    # Blocks are waited out, never evaded: FrankenSurf does not rotate IPs,
    # proxies or VPN egress to get around them.
    # Opt-in raw materials for the calling agent: JSON the page's own frontend
    # fetched while a browser rendered it (XHR/fetch). FrankenSurf returns it
    # as data and makes no semantic claims about it.
    capture_json_responses: bool = False
    # T0 content negotiation: ask for text/markdown (Cloudflare "Markdown for
    # Agents" and others). Off by default because markdown drops JSON-LD and
    # embedded JSON that structured callers read; the MCP read tool turns it on.
    prefer_markdown: bool = False
    # T3 signed identity: sign HTTP reads with Web Bot Auth when a key exists
    # (see bot_auth.py). Without a key nothing is signed.
    sign_requests: bool = True
    # T5 human handoff (see handoff.py): after every automatic provider has
    # failed, open the page for a person to clear, then resume. Off by default
    # because it waits for someone, up to handoff_timeout_seconds.
    allow_handoff: bool = False
    handoff_timeout_seconds: float = 300.0
    # Your real Chrome or Edge (see real_browser.py), opt-in: it joins an
    # automatic read's route and moves ahead once walls do (escalate_after_walls).
    # FRANKENSURF_REAL_BROWSER=1 turns it on for every read.
    allow_real_browser: bool = False
    # How long the real browser lets a challenge clear by itself; it never clicks.
    real_browser_wait_seconds: float = 15.0
    # Find the page when the URL is wrong: a public read that ends NOT_FOUND
    # searches the site itself (its search from the home page) for the words the
    # URL asked for, and returns those results when they mention them
    # (receipt.rescued says so).
    rescue_not_found: bool = True
    # More than one connection (see egress.py): with FRANKENSURF_EGRESS set, a
    # read the direct connection ended at one of egress_failures gets one pass
    # through each other connection, with these tools (the wall vendor's best first).
    use_egress_pool: bool = True
    egress_failures: tuple[str, ...] = ("BLOCKED", "CAPTCHA", "RATE_LIMITED")
    egress_tools: tuple[str, ...] = ("camoufox", "scrapling", "http")
    # PDFs are read with pypdf; text from at most this many pages is returned.
    pdf_max_pages: int = 50
    capture_json_max_items: int = 20
    capture_json_max_bytes: int = 2 * 1024 * 1024
    origin_min_interval_seconds: float = 2.0
    origin_cooldown_seconds: float = 900.0
    origin_cooldown_failures: tuple[str, ...] = ("BLOCKED", "CAPTCHA", "RATE_LIMITED")
    # After this many failed tries in one read, free tools that fetch from their
    # own network move ahead; after this many walls, the real browser (when
    # allowed) does. 0 disables both.
    escalate_after_walls: int = 4
    # After this many walls, allowed paid tools move ahead, cheapest likely
    # success first (one more when a free tool usually beats this wall vendor).
    # 0 leaves them to escalate_after_walls.
    paid_after_walls: int = 1
    escalation_failures: tuple[str, ...] = ("BLOCKED", "CAPTCHA")
    # Remember which provider got through a site after earlier ones failed and
    # try it first next time. An ordering hint, not a reliability claim. 0 disables.
    origin_route_hint_ttl_seconds: float = 86400.0
    context_stop_failures: tuple[str, ...] = ("AUTH_REQUIRED", "AUTH_EXPIRED")
    # A public read stops at a sign-in wall only after this many providers in a
    # row hit one; fewer may be a sign-in redirect served to bots.
    auth_wall_confirmations: int = 5
    # After a suspected fake wall on a public read, these move to the front of
    # the remaining providers (those allowed and available), in this order.
    fake_wall_confirmers: tuple[str, ...] = ("jina_reader", "camoufox", "firecrawl", "zyte",
                                             "scrapling", "zenrows")

    def __post_init__(self):
        from .actions import validate_action_classes
        validate_action_classes(self.action_classes)
        self._validate_capture_and_pacing()
        self._validate_search()
        self._validate_browser_agent()
        self._validate_browser_do()
        self._validate_entry_and_providers()
        self._validate_route_memory_and_failures()
        self._validate_routing()
        self._validate_profile()
        self._validate_readiness()
        self._validate_budgets()

    def _validate_capture_and_pacing(self):
        _require(type(self.allow_archive) is bool, "allow_archive must be a boolean")
        _require(type(self.block_private_network) is bool, "block_private_network must be a boolean")
        _require(type(self.main_content) is bool, "main_content must be a boolean")
        from .interactions import ACTIONS as _ACTIONS
        _require(isinstance(self.interactions, tuple) and set(self.interactions) <= set(_ACTIONS),
                 "interactions is a tuple of: " + ", ".join(_ACTIONS))
        _require(type(self.interact_on_escalation) is bool, "interact_on_escalation must be a boolean")
        _require(type(self.warm_up_on_wall) is bool, "warm_up_on_wall must be a boolean")
        _require(_is_number(self.site_session_hours), "site_session_hours must be finite and nonnegative")
        _require(type(self.auto_items) is bool, "auto_items must be a boolean")
        _require(_is_int(self.search_merge_sources, 1) and self.search_merge_sources <= 10,
                 "search_merge_sources must be an integer from 1 to 10")
        _require(type(self.search_ladder) is bool, "search_ladder must be a boolean")
        _require(type(self.search_learned_routing) is bool, "search_learned_routing must be a boolean")
        _require(_is_int(self.search_good_results, 1) and self.search_good_results <= 100,
                 "search_good_results must be an integer from 1 to 100")
        _require(_is_number(self.search_hedge_after_seconds),
                 "search_hedge_after_seconds must be finite and nonnegative")
        _require(_is_int(self.search_hedge_racers, 1) and self.search_hedge_racers <= 3,
                 "search_hedge_racers must be 1 to 3")
        _require(_is_number(self.search_max_seconds, positive=True),
                 "search_max_seconds must be positive and finite")
        _require(_is_int(self.search_max_sources, 1) and self.search_max_sources <= 20,
                 "search_max_sources must be an integer from 1 to 20")
        _require(_is_int(self.search_paid_after_weak, 0) and self.search_paid_after_weak <= 20,
                 "search_paid_after_weak must be an integer from 0 to 20")
        _require(_is_int(self.search_exclude_operators, 0) and self.search_exclude_operators <= 20,
                 "search_exclude_operators must be an integer from 0 to 20")
        _require(type(self.capture_json_responses) is bool,
                 "capture_json_responses must be a boolean")
        _require(type(self.prefer_markdown) is bool, "prefer_markdown must be a boolean")
        _require(type(self.sign_requests) is bool, "sign_requests must be a boolean")
        _require(type(self.allow_handoff) is bool, "allow_handoff must be a boolean")
        _require(type(self.completeness_escalation) is bool, "completeness_escalation must be a boolean")
        _require(_is_provider_ids(self.completeness_ladder) or self.completeness_ladder == (),
                 "completeness_ladder must be distinct provider IDs")
        _require(_is_int(self.auth_wall_confirmations, 1),
                 "auth_wall_confirmations must be a positive integer")
        _require(_is_provider_ids(self.fake_wall_confirmers) or self.fake_wall_confirmers == (),
                 "fake_wall_confirmers must be distinct provider IDs")
        _require(self.exclude_providers == () or _is_provider_ids(self.exclude_providers),
                 "exclude_providers must be distinct provider IDs")
        _require(type(self.try_harder) is bool, "try_harder must be a boolean")
        _require(self.retry_of_trace is None or (isinstance(self.retry_of_trace, str)
                 and re.fullmatch(r"[0-9a-f]{32}", self.retry_of_trace) is not None),
                 "retry_of_trace must be a trace ID")
        _require(_is_int(self.completeness_settle_ms, 0),
                 "completeness_settle_ms must be a nonnegative integer")
        _require(isinstance(self.expect_terms, tuple) and len(self.expect_terms) <= 10
                 and all(isinstance(term, str) and 0 < len(term.strip()) <= 100
                         for term in self.expect_terms),
                 "expect_terms must be up to 10 nonempty strings")
        _require(type(self.card_images) is bool, "card_images must be a boolean")
        _require(_is_int(self.scroll_screens, 0) and self.scroll_screens <= 20,
                 "scroll_screens must be an integer from 0 to 20")
        _require(_is_int(self.completeness_borderline_items, 0),
                 "completeness_borderline_items must be a nonnegative integer")
        _require(_is_int(self.completeness_max_extra_reads, 0),
                 "completeness_max_extra_reads must be a nonnegative integer")
        _require(type(self.completeness_stop_on_agreement) is bool,
                 "completeness_stop_on_agreement must be a boolean")
        _require(_is_int(self.completeness_parallel, 1) and self.completeness_parallel <= 4,
                 "completeness_parallel must be an integer from 1 to 4")
        _require(_is_number(self.hedge_after_seconds), "hedge_after_seconds must be finite and nonnegative")
        _require(type(self.lightning) is bool, "lightning must be a boolean")
        _require(_is_int(self.hedge_racers, 1) and self.hedge_racers <= 3, "hedge_racers must be 1 to 3")
        _require(type(self.use_wall_priors) is bool, "use_wall_priors must be a boolean")
        _require(type(self.allow_real_browser) is bool, "allow_real_browser must be a boolean")
        _require(type(self.use_egress_pool) is bool, "use_egress_pool must be a boolean")
        _require(type(self.rescue_not_found) is bool, "rescue_not_found must be a boolean")
        _require(_is_codes(self.egress_failures), "egress_failures must be a tuple of nonempty codes")
        _require(_is_provider_ids(self.egress_tools), "egress_tools must be distinct provider IDs")
        _require(_is_number(self.real_browser_wait_seconds),
                 "real_browser_wait_seconds must be finite and nonnegative")
        _require(_is_number(self.completeness_deadline_seconds, positive=True),
                 "completeness_deadline_seconds must be positive and finite")
        _require(_is_number(self.completeness_step_timeout_seconds, positive=True),
                 "completeness_step_timeout_seconds must be positive and finite")
        _require(_is_number(self.handoff_timeout_seconds, positive=True),
                 "handoff_timeout_seconds must be positive and finite")
        _require(_is_int(self.pdf_max_pages, 1), "pdf_max_pages must be a positive integer")
        for field in ("capture_json_max_items", "capture_json_max_bytes"):
            _require(_is_int(getattr(self, field), 1), field + " must be a positive integer")
        for field in ("origin_min_interval_seconds", "origin_cooldown_seconds"):
            _require(_is_number(getattr(self, field)), field + " must be finite and nonnegative")
        _require(_is_codes(self.origin_cooldown_failures),
                 "origin_cooldown_failures must be a tuple of nonempty codes")
        _require(_is_int(self.paid_after_walls, 0), "paid_after_walls must be a nonnegative integer")
        _require(_is_int(self.escalate_after_walls, 0),
                 "escalate_after_walls must be a nonnegative integer")
        _require(_is_codes(self.escalation_failures),
                 "escalation_failures must be a tuple of nonempty codes")
        _require(_is_number(self.origin_route_hint_ttl_seconds),
                 "origin_route_hint_ttl_seconds must be finite and nonnegative")
        _require(self.max_cost_usd is None or _is_number(self.max_cost_usd),
                 "max_cost_usd must be finite and nonnegative or None")

    def _validate_search(self):
        for field, value, allow_none in (
                ("search_source_candidates", self.search_source_candidates, True),
                ("search_source_allow", self.search_source_allow, True),
                ("search_source_prefer", self.search_source_prefer, False)):
            if value is None and allow_none:
                continue
            _require(type(value) is tuple
                     and (bool(value) or field != "search_source_candidates")
                     and all(_valid_plugin_id(item) for item in value)
                     and len(set(value)) == len(value),
                     field + " must contain distinct nonempty search source IDs")
        _require(self.search_max_attempts is None or _is_int(self.search_max_attempts, 1),
                 "search_max_attempts must be a positive integer or None")
        _require(self.search_source_timeout_seconds is None
                 or _is_number(self.search_source_timeout_seconds, positive=True),
                 "search_source_timeout_seconds must be positive and finite or None")
        _require(_is_codes(self.search_terminal_failures),
                 "search_terminal_failures must be a tuple of nonempty codes")

    def _validate_browser_agent(self):
        from .browser_use_config import READ_ACTIONS, origin
        _require(all(_is_int(value, 1) for value in (
                     self.browser_agent_max_steps, self.browser_agent_max_model_calls,
                     self.browser_agent_max_actions, self.browser_agent_max_actions_per_step,
                     self.browser_agent_max_failures, self.browser_agent_packet_max_bytes)),
                 "browser agent counts and packet budget must be positive integers")
        _require(_is_int(self.browser_agent_readiness_poll_ms, 1),
                 "browser agent readiness polling must be positive milliseconds")
        _require(all(_is_number(value, positive=True) for value in (
                     self.browser_agent_llm_timeout_seconds,
                     self.browser_agent_step_timeout_seconds,
                     self.browser_agent_action_timeout_seconds)),
                 "browser agent timeouts must be positive and finite")
        actions = self.browser_agent_allowed_actions
        _require(type(actions) is tuple and bool(actions)
                 and all(isinstance(value, str) and value in READ_ACTIONS for value in actions)
                 and len(set(actions)) == len(actions),
                 "browser agent actions must be distinct public read actions")
        _require(type(self.browser_agent_use_vision) is bool,
                 "browser agent vision flag must be boolean")
        if self.browser_agent_allowed_origins is not None:
            _require(type(self.browser_agent_allowed_origins) is tuple
                     and bool(self.browser_agent_allowed_origins),
                     "browser agent origins must be a nonempty tuple or None")
            _require(_are_origins(self.browser_agent_allowed_origins, origin),
                     "browser agent origins must be HTTP(S) origins without credentials, paths or fragments")
        _require(self.browser_agent_task is None
                 or (isinstance(self.browser_agent_task, str)
                     and bool(self.browser_agent_task.strip())
                     and len(self.browser_agent_task) <= 2000),
                 "browser agent task must be a 1-2000 character string or None")
        if self.browser_agent_entry_url is not None:
            try:
                origin(self.browser_agent_entry_url)
            except (ValueError, TypeError):
                raise ValueError("browser agent entry must be a public HTTP(S) URL") from None

    def _validate_browser_do(self):
        from .browser_use_config import origin
        from .web_do import ACTION_CONTRACTS, ACTION_TOOLS
        _require(_is_distinct_subset(self.browser_do_allowed_tools, ACTION_TOOLS),
                 "browser do tools must be distinct supported action tools")
        _require(_is_distinct_subset(self.browser_do_allowed_contracts, ACTION_CONTRACTS),
                 "browser do contracts must be distinct Core action contracts")
        if self.browser_do_allowed_origins is not None:
            _require(type(self.browser_do_allowed_origins) is tuple
                     and bool(self.browser_do_allowed_origins),
                     "browser do origins must be a nonempty tuple or None")
            _require(_are_origins(self.browser_do_allowed_origins, origin),
                     "browser do origins must be HTTP(S) origins without credentials, paths or fragments")
        _require(all(_is_int(value, 1) for value in (
                     self.browser_do_max_actions, self.browser_do_packet_max_bytes,
                     self.browser_do_journal_max_bytes, self.browser_do_selector_max_bytes,
                     self.browser_do_value_max_bytes, self.browser_do_description_max_bytes,
                     self.browser_do_idempotency_key_max_bytes)),
                 "browser do counts and byte budgets must be positive integers")
        _require(_is_number(self.browser_do_action_timeout_seconds, positive=True),
                 "browser do action timeout must be positive and finite")
        _require(_is_int(self.browser_do_settle_ms, 0),
                 "browser do settle milliseconds must be nonnegative")
        _require(type(self.browser_do_require_auth_check) is bool,
                 "browser do authentication check flag must be boolean")

    def _validate_entry_and_providers(self):
        from .public_entry import valid_entry
        _require(self.public_entry_url is None or valid_entry(self.public_entry_url),
                 "public_entry_url must be HTTPS without credentials or fragment")
        _require(isinstance(self.public_entry_continue_failures, tuple)
                 and all(isinstance(x, str) for x in self.public_entry_continue_failures),
                 "public_entry_continue_failures must be a tuple of failure codes")
        _require(_is_number(self.provider_cleanup_grace_seconds),
                 "provider cleanup grace must be finite and nonnegative")
        _require(type(self.retain_public_failure_evidence) is bool,
                 "retain_public_failure_evidence must be boolean")
        _require(_is_int(self.provider_composition_max_depth, 1)
                 and _is_int(self.provider_composition_max_attempts, 1),
                 "provider composition limits must be positive integers")
        _require(_is_int(self.provider_max_attempts_per_candidate, 1),
                 "provider_max_attempts_per_candidate must be a positive integer")
        _require(_is_number(self.provider_retry_delay_seconds),
                 "provider retry delay must be finite and nonnegative")
        _require(_is_number(self.provider_deadline_grace_seconds),
                 "provider deadline grace must be finite and nonnegative")
        _require(_is_int(self.image_max_attempts, 1), "image_max_attempts must be a positive integer")
        _require(_is_number(self.image_retry_delay_seconds),
                 "image retry delay must be finite and nonnegative")

    def _validate_route_memory_and_failures(self):
        _require(type(self.use_route_memory) is bool, "use_route_memory must be boolean")
        _require(_is_int(self.route_memory_min_samples, 1),
                 "route memory sample requirement must be positive")
        _require(_is_number(self.route_memory_ttl_seconds),
                 "route memory expiry must be finite and nonnegative")
        _require(_is_int(self.max_pages, 1), "max_pages must be a positive integer")
        _require(_is_int(self.workload_assertion_max_count, 1)
                 and _is_int(self.workload_assertion_max_bytes, 1)
                 and _is_int(self.repair_overlay_registry_max_bytes, 1),
                 "workload assertion and repair registry limits must be positive integers")
        _require(all(_is_codes(codes) for codes in (
                     self.terminal_failures, self.context_stop_failures,
                     self.image_retry_failures, self.provider_retry_failures)),
                 "failure policies must be tuples of nonempty codes")

    def _validate_profile(self):
        from .profiles import NAME
        _require(self.profile is None or (isinstance(self.profile, str) and NAME.match(self.profile)),
                 "profile must be a profile name or None")
        _require(self.profile is None or self.identity is None,
                 "Use either profile or identity, not both")

    def _validate_routing(self):
        _require(_is_int(self.navigation_page, 1), "navigation_page must be a positive integer")
        _require(self.freshness in {"now", "hour", "day", "cached"}, "invalid freshness")
        _require(self.provider is None or _valid_plugin_id(self.provider),
                 "provider must be a safe provider ID or None")
        _require(_is_provider_ids(self.compound_source_candidates),
                 "compound_source_candidates requires distinct nonempty provider IDs")
        _require(self.provider_candidates is None or _is_provider_ids(self.provider_candidates),
                 "provider_candidates must be a nonempty tuple of distinct provider IDs")

    def _validate_readiness(self):
        _require(self.wait_state in {"attached", "visible"}, "invalid wait state")
        for name, selector in (("wait_selector", self.wait_selector),
                               ("content_ready_selector", self.content_ready_selector)):
            _require(selector is None or _is_selector(selector),
                     name + " must be a bounded printable CSS selector or None")
        _require(_is_number(self.content_ready_timeout_seconds, positive=True),
                 "content readiness timeout must be positive and finite")
        _require(self.content_ready_selector is None
                 or not _is_number(self.timeout_seconds, positive=True)
                 or self.content_ready_timeout_seconds <= self.timeout_seconds,
                 "content readiness timeout cannot exceed the operation timeout")
        for field in ("public_browser_headless", "scrapling_google_search",
                      "scrapling_solve_cloudflare", "scrapling_load_dom"):
            _require(type(getattr(self, field)) is bool, field + " must be boolean")
        _require(self.scrapling_navigation_wait_until
                 in (None, "load", "domcontentloaded", "networkidle", "commit"),
                 "unsupported Scrapling navigation readiness")

    def _validate_budgets(self):
        _require(_is_number(self.unblocker_timeout_seconds, positive=True),
                 "unblocker_timeout_seconds must be positive and finite")
        _require(_is_number(self.agent_provider_timeout_seconds, positive=True),
                 "agent_provider_timeout_seconds must be positive and finite")
        _require(all(_is_int(value, 0) for value in (
                     self.max_images, self.settle_ms, self.html_shell_min_text_chars,
                     self.rendered_min_text_chars, self.html_shell_large_bytes,
                     self.html_shell_large_min_text_chars, self.second_opinion_text_chars)),
                 "resource counts must be nonnegative integers")
        _require(_is_int(self.max_bytes, 1) and _is_int(self.max_image_bytes, 1),
                 "byte budgets must be positive integers")
        _require(_is_number(self.timeout_seconds, positive=True),
                 "timeout must be positive and finite")


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _is_int(value, minimum):
    return type(value) is int and value >= minimum


def _is_number(value, *, positive=False):
    """A finite int or float, at least zero (or above zero when positive)."""
    if type(value) not in (int, float) or not math.isfinite(value):
        return False
    return value > 0 if positive else value >= 0


def _is_codes(value):
    return type(value) is tuple and all(isinstance(code, str) and code for code in value)


def _is_distinct_subset(value, allowed):
    return (type(value) is tuple and bool(value)
            and all(item in allowed for item in value) and len(set(value)) == len(value))


def _is_provider_ids(value):
    return (type(value) is tuple and bool(value)
            and all(_valid_plugin_id(item) for item in value) and len(set(value)) == len(value))


def _are_origins(values, origin):
    try:
        return all(isinstance(value, str) and origin(value) == value for value in values)
    except (ValueError, TypeError):
        return False


def _is_selector(selector):
    return (isinstance(selector, str) and bool(selector.strip())
            and len(selector.encode()) <= 4096
            and not any(ord(character) < 32 or ord(character) == 127 for character in selector))


_NO_REPORTED_COST = object()


def _validated_content_readiness(value, policy):
    """Validate provider readiness metadata against the exact Core policy."""
    if policy.content_ready_selector is None:
        if value is not None:
            raise WebFailure("PROVIDER_DOWN",
                "Provider returned unsolicited content readiness metadata")
        return None
    if (type(value) is not dict
            or set(value) != {"status", "timeout_seconds"}
            or value.get("status") not in {"satisfied", "timed_out"}
            or type(value.get("timeout_seconds")) not in (int, float)
            or value["timeout_seconds"] != policy.content_ready_timeout_seconds):
        raise WebFailure("PROVIDER_DOWN",
            "Provider omitted or returned invalid content readiness metadata")
    return {"status": value["status"],
            "timeout_seconds": value["timeout_seconds"]}


def _measured_cost(value):
    if type(value) not in (int, float):
        return None
    try:
        return value if math.isfinite(value) and value >= 0 else None
    except OverflowError:
        return None


def _total_cost(values):
    if any(value is None for value in values):
        return None
    return _measured_cost(float(sum((Decimal(str(value)) for value in values), Decimal(0))))


def _remaining_cost_policy(policy, costs, *, paid):
    """Pass the operation's remaining cap to each acquisition, never a fresh cap."""
    if policy.max_cost_usd is None:
        return policy
    spent = _total_cost(costs)
    if spent is None:
        if paid:
            raise WebFailure("BUDGET_EXHAUSTED", "Prior acquisition cost is unknown; no verified paid budget remains")
        return replace(policy, max_cost_usd=0)
    remaining = float(Decimal(str(policy.max_cost_usd)) - Decimal(str(spent)))
    if paid and (remaining < 0 or (remaining == 0 and spent > 0)):
        raise WebFailure("BUDGET_EXHAUSTED", "Aggregate acquisition cost cap is exhausted")
    return replace(policy, max_cost_usd=max(0, remaining))


class WebFailure(Exception):
    def __init__(self, code: str, message: str, http_status: int | None = None, *,
                 response_url: str | None = None, failure_stage: str | None = None,
                 cost_usd=_NO_REPORTED_COST):
        if response_url is not None: _validate_url(response_url)
        super().__init__(message)
        self.code, self.message, self.http_status = code, message, http_status
        if cost_usd is not _NO_REPORTED_COST:
            if cost_usd is not None and _measured_cost(cost_usd) is None:
                raise ValueError("Reported cost must be finite and nonnegative, or unknown")
            self.cost_usd = cost_usd
        self.response_url = response_url
        from .provider_worker import FAILURE_STAGES
        self.failure_stage = failure_stage if isinstance(failure_stage, str) and failure_stage in FAILURE_STAGES else None
        self._public_failure_evidence = []

    def __deepcopy__(self, memo):
        """Copy a recorded failure without retaining exception runtime state."""
        kwargs = {
            "response_url": self.response_url,
            "failure_stage": self.failure_stage,
        }
        if hasattr(self, "cost_usd"):
            kwargs["cost_usd"] = copy.deepcopy(self.cost_usd, memo)
        duplicate = type(self)(
            self.code, self.message, self.http_status, **kwargs)
        memo[id(self)] = duplicate
        duplicate._public_failure_evidence = copy.deepcopy(
            self._public_failure_evidence, memo)
        return duplicate


_SAFE_HEADERS = {"content-type", "x-wp-total", "x-wp-totalpages", "last-modified", "etag"}


def _status_failure(status: int) -> str | None:
    known = {401: "AUTH_REQUIRED", 403: "BLOCKED", 429: "RATE_LIMITED", 404: "NOT_FOUND", 410: "NOT_FOUND"}
    if status in known: return known[status]
    # A client error cannot be successful acquisition evidence. The status alone
    # does not establish an access challenge, so keep unclassified errors UNKNOWN.
    if 400 <= status < 500: return "UNKNOWN"
    return "PROVIDER_DOWN" if status >= 500 else None


def _validate_url(url: str) -> None:
    try:
        if (not isinstance(url, str)
                or any(ord(char) < 32 or ord(char) == 127
                       for char in url)):
            raise ValueError()
        parsed = urlparse(url)
        parsed.port  # Reject malformed ports as well as malformed bracketed hosts.
        valid = parsed.scheme in {"http", "https"} and bool(parsed.hostname) and parsed.username is None and parsed.password is None
    except ValueError:
        valid = False
    if not valid:
        raise WebFailure("INVALID_URL", "Only HTTP(S) URLs without embedded credentials are supported")


def _core_identity_capture(response, max_bytes, *, capture_strategy=None):
    """Detach and validate Core-only capture provenance and fetch URLs."""
    if type(response) is not dict:
        raise WebFailure("PROVIDER_DOWN",
            "Identity executor returned an invalid acquisition envelope")
    acquired = dict(response)
    absent = object()
    metadata = acquired.pop("visible_snapshot", absent)
    private_sources = acquired.pop(
        "identity_private_image_sources", absent)
    safe_metadata = None
    safe_sources = {}
    try:
        if metadata is not absent:
            expected = {
                "strategy", "strategy_version", "strategy_binding_id",
                "scope", "navigation_performed",
                "source_refresh_performed", "source_freshness",
                "gallery_coverage",
            }
            if (capture_strategy is None or type(metadata) is not dict
                    or set(metadata) != expected
                    or metadata.get("strategy") != capture_strategy.id
                    or metadata.get("strategy_version")
                        != capture_strategy.version
                    or metadata.get("strategy_binding_id")
                        != capture_strategy.binding_id
                    or metadata.get("scope")
                        != "owner_opened_visible_region"
                    or metadata.get("navigation_performed") is not False
                    or metadata.get("source_refresh_performed") is not False
                    or metadata.get("source_freshness") != "unknown"
                    or metadata.get("gallery_coverage")
                        != "partial_unverified_candidates"):
                raise ValueError()
            safe_metadata = json.loads(json.dumps(metadata, sort_keys=True,
                separators=(",", ":"), allow_nan=False))
        elif capture_strategy is not None:
            raise ValueError()
        if private_sources is not absent:
            if type(private_sources) is not dict:
                raise ValueError()
            for public_url, private_url in private_sources.items():
                if (not isinstance(public_url, str)
                        or not isinstance(private_url, str)):
                    raise ValueError()
                _validate_url(public_url)
                _validate_url(private_url)
                if (public_url != _output_url(
                        public_url, named_identity=True)
                        or public_url != _output_url(
                            private_url, named_identity=True)):
                    raise ValueError()
                safe_sources[public_url] = private_url
                # The executor must export the redacted URL in its projection;
                # the signed fetch URL remains only in this detached map.
                content = acquired.get("content")
                raw = acquired.get("raw")
                if (isinstance(content, str)
                        and (private_url in content
                            or html.escape(private_url, quote=True)
                                in content)):
                    raise ValueError()
                if (isinstance(raw, bytes)
                        and (private_url.encode() in raw
                            or html.escape(private_url, quote=True).encode()
                                in raw)):
                    raise ValueError()
        encoded = json.dumps({
            "visible_snapshot": safe_metadata,
            "identity_private_image_sources": safe_sources,
        }, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        if len(encoded) > max_bytes:
            raise ValueError()
    except WebFailure:
        raise WebFailure("PROVIDER_DOWN",
            "Identity executor returned invalid private capture data") from None
    except (TypeError, ValueError, OverflowError, UnicodeError):
        raise WebFailure("PROVIDER_DOWN",
            "Identity executor returned invalid private capture data") from None
    return acquired, safe_metadata, safe_sources


def _identity_acquisition_fingerprint(response, max_bytes, *,
                                      capture_metadata=None):
    """Bind provider output to the exact Core acquisition without exporting it."""
    try:
        if (type(response) is not dict
                or not all(isinstance(response.get(key), str)
                           for key in ("url", "content", "content_type"))):
            raise ValueError()
        _validate_url(response["url"])
        content = response["content"].encode()
        raw = response.get("raw", content)
        status = response.get("http_status")
        headers = response.get("headers", {})
        if (not isinstance(raw, bytes)
                or len(content) > max_bytes or len(raw) > max_bytes
                or status is not None and (type(status) is not int
                    or not 100 <= status <= 599)
                or type(headers) is not dict
                or any(not isinstance(key, str)
                       or not isinstance(value, str)
                       for key, value in headers.items())):
            raise ValueError()
        payload = {
            "url": response["url"],
            "content_sha256": hashlib.sha256(content).hexdigest(),
            "raw_sha256": hashlib.sha256(raw).hexdigest(),
            "content_type": response["content_type"],
            "http_status": status,
            "headers": {key: value for key, value in headers.items()
                        if key in _SAFE_HEADERS},
            "screenshot": response.get("screenshot"),
            "navigation": response.get("navigation"),
            "navigation_data": response.get("navigation_data"),
            "content_readiness": response.get("content_readiness"),
            "capture_metadata": capture_metadata,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode()
        if len(encoded) > max_bytes:
            raise ValueError()
    except WebFailure:
        raise WebFailure("PROVIDER_DOWN",
            "Identity executor returned an invalid acquisition envelope") from None
    except (TypeError, ValueError, OverflowError, UnicodeError):
        raise WebFailure("PROVIDER_DOWN",
            "Identity executor returned an invalid acquisition envelope") from None
    return hashlib.sha256(encoded).hexdigest()


# Query parameters that carry account credentials rather than page state.
# Signed CDN parameters (for example Facebook's oh/oe) are kept: they are what
# makes the link load, and they expire.
_CREDENTIAL_QUERY_KEYS = frozenset({
    "access_token", "accesstoken", "auth", "auth_token", "authorization",
    "code", "csrf", "csrf_token", "fb_dtsg", "id_token", "jazoest", "key",
    "password", "passwd", "pwd", "refresh_token", "session", "session_id",
    "sessionid", "sid", "token", "xsrf", "xsrf_token"})


def _strip_credential_query(url):
    parsed = urlparse(url)
    if not parsed.query:
        return parsed._replace(fragment="").geturl()
    # Remove whole parameters without re-encoding the rest: signed links are
    # verified against their exact bytes.
    kept = [part for part in parsed.query.split("&")
            if unquote_plus(part.partition("=")[0]).lower() not in _CREDENTIAL_QUERY_KEYS]
    return parsed._replace(query="&".join(kept), fragment="").geturl()


def _output_url(url, *, named_identity=False):
    # Identity URLs keep their page and CDN parameters; only parameters that
    # carry account credentials are removed before output.
    try: _validate_url(url)
    except WebFailure: return None
    return _strip_credential_query(url) if named_identity else url


def _redact_result_urls(result):
    receipt = result.get("receipt", {})
    named = bool(receipt.get("identity"))
    if "url" in result: result["url"] = _output_url(result["url"], named_identity=named)
    if named and isinstance(result.get("image_urls"), list):
        result["image_urls"] = [
            _output_url(url, named_identity=True)
            for url in result["image_urls"]]
    if named and isinstance(result.get("images"), list):
        for image in result["images"]:
            if isinstance(image, dict) and "url" in image:
                image["url"] = _output_url(
                    image["url"], named_identity=True)
    for key in ("requested_url", "final_url"):
        if key in receipt: receipt[key] = _output_url(receipt[key], named_identity=named)
    for attempt in receipt.get("attempts", []):
        for key in ("url", "requested_url", "final_url"):
            if key in attempt: attempt[key] = _output_url(attempt[key], named_identity=named)
    return result


# Interstitials from common bot walls (Cloudflare, PerimeterX/HUMAN, Akamai,
# DataDome, Imperva). Checked only on short pages so articles that quote these
# phrases are not mistaken for walls.
_CHALLENGE_TITLES = frozenset({
    "just a moment...", "attention required! | cloudflare", "access denied",
    "verify you are human", "robot or human?", "pardon our interruption",
    "security check", "access to this page has been denied", "prove your humanity",
    "human verification", "are you a human?", "are you human?", "one more step",
    "bot verification", "verify you're human", "please wait while we verify your browser",
    # Access denied, as bot walls say it in other languages.
    "zugriff verweigert", "accès refusé", "acces refuse", "acceso denegado", "accesso negato",
    "toegang geweigerd", "åtkomst nekad", "adgang nægtet", "tilgang nektet", "pääsy estetty",
    "dostęp zabroniony", "přístup odepřen", "hozzáférés megtagadva", "доступ ограничен",
    "доступ запрещён", "доступ запрещен", "アクセスが拒否されました", "访问被拒绝", "拒绝访问",
    "access blocked", "request blocked", "you have been blocked", "sorry, you have been blocked",
    "vercel security checkpoint", "ddos protection", "checking your browser"})
_CHALLENGE_PHRASES = (
    "verify you are human", "checking your browser before accessing",
    "enable javascript and cookies to continue", "please complete the following challenge",
    "we must verify your session", "activate and hold the button", "press & hold",
    "press and hold", "pardon our interruption", "are you a robot", "robot or human",
    "checking if the site connection is secure", "verifying you are human",
    "access to this page has been denied", "request unsuccessful. incapsula",
    'thinks you are a "bot"', "thinks you are a bot", "unusual traffic from your computer",
    "please verify you are a human", "confirm you are not a robot", "prove your humanity",
    "but not for bots", "verify you're human", "verify you're not a robot",
    "complete the security check to access", "help us verify you're a real person",
    "the requested url was rejected", "your support id is", "you don't have permission to access",
    "aus sicherheitsgründen mussten wir den zugriff", "доступ ограничен: проблема с ip",
    "vercel security checkpoint", "we have detected unusual activity")

# Short pages that only ask the reader to sign in, in the languages the
# benchmarks meet. Checked only on pages under _SIGN_IN_TEXT_MAX characters.
_SIGN_IN_PHRASES = (
    "sign in to continue", "log in to continue", "login to continue", "please sign in",
    "please log in", "sign in to view", "log in to view",
    "ingresa a tu cuenta", "inicia sesión para continuar", "faça login para continuar",
    "entre na sua conta", "connectez-vous pour continuer", "identifiez-vous",
    "bitte melden sie sich an", "melden sie sich an, um", "accedi per continuare",
    "log in om verder te gaan")
_SIGN_IN_TEXT_MAX = 2500
# Final URLs that are a site's own bot or block page.
_BOT_PAGE_PATH = re.compile(r"/(captcha|bots?|blocked|block|challenge|access[-_]?denied|"
                            r"are-?you-?(human|a-?robot)|robot-?check)(\.html?|/|$)", re.I)


def _challenge_text(title: str | None, text: str) -> bool:
    title = (title or "").strip().lower().lstrip("# ").strip()
    # "Reddit - Prove your humanity", "Zugriff verweigert / Access denied",
    # "Доступ ограничен: проблема с IP": a site name or a second language around the title.
    parts = [title] + [part.strip(" .!") for part in re.split(r"\s*[|\-–—:/]\s+|\s+[|\-–—/]\s*", title)]
    if any(part in _CHALLENGE_TITLES for part in parts if part):
        return True
    text = text.lower()
    return len(text) < 3000 and any(phrase in text for phrase in _CHALLENGE_PHRASES)


# Login forms: a page that asks for a password instead of showing what was asked for.
_LOGIN_TITLE = re.compile(r"^(log ?in|sign ?in|login|log into .{1,40}|sign in to .{1,40}|log in to .{1,40}|"
                          r"log in or sign up.*|sign up or log in.*)$", re.I)
_LOGIN_WORDS = ("log in", "log into", "sign in", "login")
_LOGIN_TEXT_MAX = 6000


def _login_wall(title, text, requested_url):
    """True when the page is a site's login form standing in for the page asked for.

    Either the title says so ("Log into Facebook"), or a deep link came back
    titled with nothing but the site's own name while the page is a short form
    asking for a password. A real page that merely has a login link in its
    header is longer, or its title names the thing asked for.
    """
    title = (title or "").strip().lstrip("# ").strip()
    parts = [title] + [part.strip(" .!") for part in re.split(r"\s+[|\-–—:]\s+", title)]
    if any(_LOGIN_TITLE.match(part) for part in parts if part):
        return True
    body = (text or "").strip().lower()
    if len(body) >= _LOGIN_TEXT_MAX or "password" not in body[:2500]:
        return False
    try:
        parsed = urlparse(requested_url)
    except ValueError:
        return False
    host = (parsed.hostname or "").removeprefix("www.").removeprefix("m.")
    brand = host.split(".")[0] if host else ""
    deep_link = parsed.path.strip("/") != ""
    return (deep_link and bool(brand) and title.lower() == brand
            and any(word in body[:1500] for word in _LOGIN_WORDS))


def _challenge(html: str) -> bool:
    soup = BeautifulSoup(html[:100000], "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    return _challenge_text(title, soup.get_text(" ", strip=True))


_SIGN_IN_PATH = re.compile(r"(^|[/._-])(sign-?in|log-?in|auth|ws/ebayisapi\.dll)([/._?-]|$)", re.I)


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


async def _rendered_html(page):
    """The page's HTML with the content of open shadow roots added at the end of
    the body, so text, links and items inside web components are read too."""
    content = await page.content()
    try:
        shadow = await page.evaluate(_SHADOW_SCRIPT)
    except Exception:
        shadow = None
    if not shadow:
        return content
    block = '<div data-frankensurf-shadow-dom="1">' + shadow + "</div>"
    index = content.lower().rfind("</body>")
    return content[:index] + block + content[index:] if index >= 0 else content + block


# A browser's own error page ("This site can't be reached") is not the site.
_BROWSER_ERROR = re.compile(r"\b(ERR_[A-Z0-9_]{4,}|NS_ERROR_[A-Z_]+)\b|this site can.t be reached|"
                            r"unable to connect|hmm\. we.re having trouble finding that site", re.I)
# A site down for maintenance answers with a page that is not the page.
_MAINTENANCE = re.compile(r"(?i)\b(down for maintenance|under maintenance|scheduled maintenance|"
                          r"we.ll be back (soon|shortly)|we are currently (performing|undergoing) maintenance|"
                          r"currently working on [a-z0-9.-]+\.[a-z]{2,}|temporarily unavailable|site is temporarily down)\b")


def _wall_after_parse(title, text, requested_url, final_url):
    """A provider may hand back a wall as if it were the page. Name it."""
    if _challenge_text(title, text or ""):
        return "CAPTCHA"
    short = (text or "").strip()
    if len(short) < 2000 and (_BROWSER_ERROR.search(short) or _MAINTENANCE.search(short)):
        return "PROVIDER_DOWN"  # Another tool, or a later read, may get the page.
    try:
        requested, final = urlparse(requested_url), urlparse(final_url or requested_url)
    except ValueError:
        return None
    moved = final.hostname and (final.hostname + final.path) != (requested.hostname or "") + requested.path
    if moved and _BOT_PAGE_PATH.search(final.path) and not _BOT_PAGE_PATH.search(requested.path):
        return "CAPTCHA"
    if (moved and _SIGN_IN_PATH.search(final.hostname.split(".")[0] + "/" + final.path)
            and not _SIGN_IN_PATH.search(requested.path)):
        return "AUTH_REQUIRED"
    body = (text or "").strip()
    if len(body) < _SIGN_IN_TEXT_MAX and any(p in body.lower() for p in _SIGN_IN_PHRASES):
        return "AUTH_REQUIRED"
    if not _SIGN_IN_PATH.search(requested.path) and _login_wall(title, text, requested_url):
        return "AUTH_REQUIRED"
    return None


_SCROLL_SCRIPT = """async (screens) => {
  for (let i = 0; i < screens; i++) {
    window.scrollBy(0, window.innerHeight);
    await new Promise((done) => setTimeout(done, 350));
  }
  window.scrollTo(0, 0);
}"""


def _scroll_screens(policy) -> int:
    """Viewport heights to scroll before capture, so lazy images and cards load."""
    return policy.scroll_screens or (3 if policy.card_images else 0)


async def _scroll_for_lazy(page, screens: int) -> None:
    if screens:
        try:
            await page.evaluate(_SCROLL_SCRIPT, screens)
        except Exception:
            # Scrolling is a best-effort nudge; the page as loaded still stands.
            pass


# A site's own error or not-found page, served with HTTP 200. Every tool sees
# the same page, so climbing the ladder only repeats it.
_ERROR_PAGE_PATH = re.compile(r"/(error|errors|errorpage|error-page|error_page|404|"
                              r"not-?found|page-?not-?found|pagenotfound)(\.aspx|\.html?|\.php|\.jsp|/|$)",
                              re.I)
_ERROR_TITLES = frozenset({
    "error", "404", "404 error", "404 not found", "not found", "page not found",
    "404 - page not found", "404 page not found", "error 404", "an error has occurred",
    "an error occurred", "something went wrong", "server error", "oops! something went wrong",
    "sorry, something went wrong", "page cannot be found", "the page cannot be found",
    "this page isn't available", "this page could not be found", "page unavailable"})
_ERROR_PHRASES = (
    "the page you requested could not be found", "the page you are looking for could not be found",
    "the page you were looking for doesn't exist", "the page you're looking for doesn't exist",
    "the page you are looking for does not exist", "an unexpected error has occurred",
    "we couldn't find the page you were looking for", "this page doesn't exist")
_ERROR_TEXT_MAX = 4000
# An error title is strong evidence even under a site's cookie banner and footer.
_ERROR_TITLE_TEXT_MAX = 20000


def _site_error(title, text, requested_url, final_url):
    """True for a site's error or not-found page that came back as a page."""
    try:
        requested, final = urlparse(requested_url), urlparse(final_url or requested_url)
    except ValueError:
        return False
    moved = final.hostname and (final.hostname + final.path) != (requested.hostname or "") + requested.path
    if moved and _ERROR_PAGE_PATH.search(final.path) and not _ERROR_PAGE_PATH.search(requested.path):
        return True
    body = (text or "").strip().lower()
    parts = re.split(r"\s+[|\-–—:]\s+", (title or "").strip().lower().lstrip("# ").strip())
    if (len(body) < _ERROR_TITLE_TEXT_MAX
            and any(part.strip(" .!") in _ERROR_TITLES for part in parts if part)):
        return True
    return len(body) < _ERROR_TEXT_MAX and any(phrase in body for phrase in _ERROR_PHRASES)


def _image_urls(structured: object, soup: BeautifulSoup | None, url: str) -> list[str]:
    found: list[str] = []
    def visit(obj):
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key in {"image", "images", "featured_image"}:
                    if isinstance(value, str): found.append(value)
                    elif isinstance(value, dict): found.append(value.get("src") or value.get("url") or "")
                    elif isinstance(value, list):
                        for image in value:
                            if isinstance(image, str): found.append(image)
                            elif isinstance(image, dict): found.append(image.get("src") or image.get("url") or "")
                visit(value)
        elif isinstance(obj, list):
            for value in obj: visit(value)
    visit(structured)
    if soup:
        for element in soup.select('meta[property="og:image"], meta[name="twitter:image"]'):
            found.append(element.get("content", ""))
        for element in soup.select("img"):
            found.append(element.get("src") or element.get("data-src") or "")
    normalized = []
    for image in found:
        # Structured data nests images in many shapes (an ImageObject whose url
        # is itself an object, a list): only plain strings are image URLs.
        if not isinstance(image, str) or not image:
            continue
        absolute = urljoin(url, image)
        if image and urlparse(absolute).scheme in {"http", "https"} and absolute not in normalized:
            normalized.append(absolute)
    return normalized


def parse_content(content: str, content_type: str, url: str, adapter: str | None,
                  navigation_data=None, navigation=None, policy=None,
                  requested_url=None, acquisition_attestation=None, *,
                  adapter_registry=None) -> dict:
    if adapter is None:
        return _parse_builtin_content(content,content_type,url,None,navigation_data,navigation)
    from .adapters import AdapterRequest
    if adapter_registry is None:
        # Compatibility for standalone parsing helpers. Runtime always passes
        # its frozen startup registry explicitly.
        from .adapters import DEFAULT_ADAPTERS
        adapter_registry = DEFAULT_ADAPTERS
    return adapter_registry.project(adapter,AdapterRequest(
        content, content_type, url, navigation_data, navigation, policy,
        requested_url, acquisition_attestation))


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


class _JsonCapture:
    """Collects JSON bodies a rendered page fetched for itself (XHR/fetch only)."""

    def __init__(self, max_items, max_bytes):
        self.max_items, self.max_bytes = max_items, max_bytes
        self.items, self.skipped, self._tasks = [], 0, []

    @staticmethod
    def wants(resource_type, content_type):
        # Many sites serve JSON as text/html or JavaScript (Facebook GraphQL is
        # text/html with a "for (;;);" prefix). Parse decides, not the label.
        kind = (content_type or "").lower()
        return (resource_type in {"xhr", "fetch"}
                and not any(skip in kind for skip in ("image/", "font/", "video/", "audio/", "text/css")))

    def add(self, url, status, content_type, body):
        if body is None or len(body) > self.max_bytes:
            self.skipped += 1
            return
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            self.skipped += 1
            return
        text = _strip_json_prefix(text)
        try:
            data, shape = json.loads(text), "json"
        except json.JSONDecodeError:
            records = _ndjson_records(text)
            if records is None:
                self.skipped += 1
                return
            data, shape = records, "ndjson"
        try:
            _validate_url(url)
        except WebFailure:
            self.skipped += 1
            return
        if not _keep_capture(self.items, {"url": url, "http_status": status, "content_type": content_type,
                                          "format": shape, "data": data}, self.max_items):
            self.skipped += 1

    def listener(self):
        def on_response(response):
            try:
                if not self.wants(response.request.resource_type, response.headers.get("content-type")):
                    return
            except Exception:
                return

            async def read():
                try:
                    body = await response.body()
                except Exception:
                    body = None
                self.add(response.url, response.status, response.headers.get("content-type"), body)
            self._tasks.append(asyncio.ensure_future(read()))
        return on_response

    async def settle(self, timeout):
        if self._tasks:
            await asyncio.wait(self._tasks, timeout=max(timeout, 0.1))
        for task in self._tasks:
            if not task.done():
                task.cancel()
                self.skipped += 1
        return {"items": self.items, "skipped": self.skipped}


def _embedded_json(soup):
    """JSON a page embeds for its own frontend (for example __NEXT_DATA__)."""
    found = []
    for script in soup.select('script[type="application/json"]'):
        try:
            data = json.loads(script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        found.append({"id": script.get("id"), "data": data})
    return found


_JSON_PREFIXES = ("for (;;);", "for(;;);", ")]}'", "while(1);", "while (1);", "&&&START&&&")


def _strip_json_prefix(text: str) -> str:
    """Remove the anti-hijacking prefixes sites put in front of JSON bodies."""
    stripped = text.lstrip("\ufeff \t\r\n")
    for prefix in _JSON_PREFIXES:
        if stripped.startswith(prefix):
            return stripped[len(prefix):].lstrip(",\r\n ")
    return text


def _pdf_text(raw: bytes, max_pages: int) -> str:
    """Text of a PDF via pypdf, pages separated by form feeds. Title comes first."""
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(raw))
        pages = []
        for page in reader.pages[:max_pages]:
            try:
                pages.append(page.extract_text() or "")
            except Exception:  # one unreadable page does not lose the rest
                pages.append("")
        title = ((reader.metadata or {}).get("/Title") or "").strip() if reader.metadata else ""
    except Exception as exc:
        raise WebFailure("SCHEMA_CHANGED", "PDF could not be read") from exc
    text = "\f".join(pages).strip()
    return ("Title: " + title + "\n\n" if title else "") + text


def _ndjson_records(content: str):
    lines = [line for line in content.splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    records = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            return None
    return records


def _parse_builtin_content(content: str, content_type: str, url: str, adapter: str | None, navigation_data=None, navigation=None, policy=None) -> dict:
    structured: object = None
    soup = None
    if adapter == "rss" or "xml" in content_type:
        import xml.etree.ElementTree as ET
        try: root = ET.fromstring(content)
        except ET.ParseError as exc: raise WebFailure("SCHEMA_CHANGED", "Expected XML response") from exc
        title = root.findtext("./channel/title")
        text = " ".join(root.itertext())
        structured = {"format": "xml", "root": root.tag}
    elif "pdf" in content_type and adapter not in ("json", "html"):
        title = None
        if content.startswith("Title: "):
            title = content.split("\n", 1)[0][7:].strip() or None
        if title is None:
            title = next((line.strip() for line in content.splitlines() if line.strip()), None)
            title = title[:200] if title else None
        return {"title": title, "text": content.strip(), "image_urls": [],
                "structured": {"format": "pdf", "pages": content.count("\f") + 1 if content.strip() else 0}}
    elif "markdown" in content_type and adapter not in ("json", "html"):
        title = None
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                title = stripped[2:].strip() or None
                break
            if stripped.lower().startswith("title:"):
                title = stripped[6:].strip() or None
                break
        text = content.strip()
        images = [urljoin(url, found) for found in re.findall(r"!\[[^\]]*\]\((\S+?)(?:\s+\"[^\"]*\")?\)", content)]
        structured = {"format": "markdown"}
        return {"title": title, "text": text, "structured": structured,
                "image_urls": list(dict.fromkeys(images))}
    elif "json" in content_type or adapter == "json":
        try:
            structured = json.loads(content)
        except json.JSONDecodeError as exc:
            # Some APIs stream newline-delimited JSON (one module per line) under a
            # JSON content type. Accept it only when every nonempty line parses.
            records = _ndjson_records(content)
            if records is None:
                raise WebFailure("SCHEMA_CHANGED", "Expected valid JSON response") from exc
            structured = {"format": "ndjson", "records": records}
        obj = structured.get("product", structured) if isinstance(structured, dict) else {}
        title = obj.get("title") if isinstance(obj, dict) else None
        text = BeautifulSoup(obj.get("description") or obj.get("body_html") or "", "html.parser").get_text(" ", strip=True) if isinstance(obj, dict) else ""
    else:
        soup = BeautifulSoup(content, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else None
        jsonld = []
        for script in soup.select('script[type="application/ld+json"]'):
            try: jsonld.append(json.loads(script.get_text()))
            except (json.JSONDecodeError, TypeError): pass
        structured = {"jsonld": jsonld, "embedded_json": _embedded_json(soup)}
        for remove in soup.select("script,style,noscript"):
            remove.decompose()
        text = soup.get_text(" ", strip=True)
    parsed = {"title": title, "text": text, "structured": structured, "image_urls": _image_urls(structured, soup, url)}
    return parsed


def _reject_unusable_page(candidate, candidate_record, policy, resolved, adapter,
                          response, parsed, url):
    """Raise when a provider returned an app shell, an empty render, a challenge
    or a sign-in page instead of the page, so the next provider is tried."""
    # A site's own error page is the answer, not an unfinished page: checked
    # first, so a short one is not mistaken for an empty render. A public
    # read's NOT_FOUND gets one confirming provider (fake 404s served to
    # bots), then stops instead of climbing the whole ladder.
    if (not resolved and adapter in (None, "html")
            and any(kind in response["content_type"] for kind in ("html", "markdown"))
            and _site_error(parsed.get("title"), parsed.get("text"), url, response.get("url"))):
        raise WebFailure("NOT_FOUND", "Provider returned the site's error or not-found page",
                         response.get("http_status"), response_url=response.get("url"))
    # The site's own markers, from the site module shaping this read.
    from .site_modules import ACTIVE as ACTIVE_MODULE
    active_module = ACTIVE_MODULE.get()
    if (active_module is not None and adapter in (None, "html")
            and active_module.invalid_page(parsed.get("title"), parsed.get("text"),
                                           response.get("url") or url)):
        raise WebFailure("NOT_FOUND", "The site module marks this page as the site's error or empty page",
                         response.get("http_status"), response_url=response.get("url"))
    if candidate == "http" and policy.provider is None and "html" in response["content_type"]:
        unresolved = re.search(r"\{\{[^{}]+\}\}",parsed["text"])
        # Exact adapters validate their own projections. Short product
        # titles are not evidence of an unrendered page shell.
        text_chars = len(parsed["text"].strip())
        shell = adapter in (None, "html") and "<script" in response["content"] and (
            text_chars < policy.html_shell_min_text_chars
            or (len(response["content"]) >= policy.html_shell_large_bytes
                and text_chars < policy.html_shell_large_min_text_chars))
        if unresolved or shell: raise WebFailure("VISUAL_REQUIRED", "Rendered page required for unresolved template or empty shell")
    elif (not resolved and adapter in (None, "html") and "html" in response["content_type"]
            and policy.provider is None and (candidate_record or {}).get("rendering")
            and len(parsed["text"].strip()) < policy.rendered_min_text_chars):
        raise WebFailure("EMPTY_PAGE", "Rendered page has almost no text")
    if (not resolved and any(kind in response["content_type"]
                             for kind in ("html", "markdown"))):
        wall = _wall_after_parse(parsed.get("title"), parsed.get("text"),
                                 url, response.get("url"))
        if wall:
            raise WebFailure(wall, "Provider returned a challenge or sign-in page",
                             response.get("http_status"),
                             response_url=response.get("url"))


def _wants_second_opinion(result, policy, provider):
    """An automatic plain-HTTP success that may be a page whose content renders later."""
    receipt = result.get("receipt") or {}
    if (policy.second_opinion_text_chars == 0 or provider or policy.provider
            or policy.provider_candidates is not None or policy.identity or policy.render
            or receipt.get("status") != "observed" or receipt.get("method") != "http"
            or receipt.get("cache_hit") or "html" not in (result.get("content_type") or "")):
        return False
    return ("<script" in (result.get("content") or "")
            and len((result.get("text") or "").strip()) < policy.second_opinion_text_chars)


def _automatic_observed(result, policy, provider):
    """An automatic (router-chosen) read that came back observed."""
    receipt = result.get("receipt") or {}
    return not (provider or policy.provider or policy.provider_candidates is not None
                or policy.identity or receipt.get("status") != "observed"
                or receipt.get("cache_hit"))


class _closing:
    """Close a per-read client on exit; leave the shared one open."""

    def __init__(self, client, owned):
        self.client, self.owned = client, owned

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, *args):
        if self.owned:
            await self.client.aclose()


def _merge_response_cookies(profile, response):
    """Fold Set-Cookie answers from a profile read back into the profile."""
    cookies = []
    for cookie in response.cookies.jar:
        cookies.append({"name": cookie.name, "value": cookie.value,
                        "domain": cookie.domain or (response.url.host or ""),
                        "path": cookie.path or "/", "secure": bool(cookie.secure),
                        "httpOnly": False, "sameSite": "Lax",
                        "expires": float(cookie.expires) if cookie.expires else -1})
    if cookies and profile.merge({"cookies": cookies, "origins": []}):
        profile.changed = True


def _allowed(registry, identifier, policy):
    try:
        registry.require_enabled(identifier, policy)
        return True
    except WebFailure:
        return False


_HANDOFF_WALLS = frozenset({"CAPTCHA", "BLOCKED", "AUTH_REQUIRED", "AUTH_EXPIRED"})


def _suggest_handoff(receipt):
    """A read stopped by a wall a person could clear names the T5 next step."""
    failure = receipt.get("failure") or {}
    tried = {attempt.get("provider") for attempt in receipt.get("attempts") or ()}
    if failure.get("code") in _HANDOFF_WALLS and "handoff" not in tried:
        receipt["next_step"] = {"provider": "handoff", "reason": failure["code"],
            "how": "retry with provider='handoff' (or allow_handoff=True) to open the page "
                   "for a person to clear; the read resumes once the wall is gone",
            "or": "if an older copy will do, retry with allow_archive=True for the Internet "
                  "Archive's latest stored copy (receipt.archived says when it was taken)"}


def _navigation_failure(message):
    """Chromium navigation errors that are the target's answer, not an outage.

    A site that blocks with an empty error response makes page.goto raise
    ERR_HTTP_RESPONSE_CODE_FAILURE instead of returning the response.
    """
    if "ERR_HTTP_RESPONSE_CODE_FAILURE" in message or "ERR_BLOCKED_BY_RESPONSE" in message:
        return "BLOCKED"
    if "ERR_NAME_NOT_RESOLVED" in message:
        return "NOT_FOUND"
    return None


def _search_query_type(vertical, options):
    """A coarse kind of query for learning which search source suits it:
    vertical, recency or not, site filter or not."""
    return "%s|%s|%s" % (vertical, "recent" if options.get("recency") else "any",
                         "site" if options.get("site") else "open")


def _result_key(url):
    """One search result per page: scheme, www., trailing slash, fragments and
    tracking parameters don't make a different result."""
    parsed = urlparse(url)
    query = "&".join(sorted(part for part in parsed.query.split("&")
                            if part and not re.match(r"(?i)(utm_|fbclid|gclid|ref=|ref_|mc_)", part)))
    return ((parsed.hostname or "").lower().removeprefix("www.") + parsed.path.rstrip("/")
            + ("?" + query if query else ""))


# Set once a read asks for block_private_network: every HTTP hop after that
# (redirects included) is checked before it is sent.
PRIVATE_GUARD: ContextVar = ContextVar("frankensurf_private_guard", default=False)
_PRIVATE_NAMES = (".localhost", ".local", ".internal", ".lan", ".home.arpa", ".intranet")


def _private_host(host) -> bool:
    """A loopback, private, link-local, reserved or local-only name or address."""
    host = str(host or "").strip("[]").lower().rstrip(".")
    if not host:
        return False
    if host == "localhost" or host.endswith(_PRIVATE_NAMES) or "." not in host and ":" not in host:
        return True
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        return False


async def _refuse_private(url):
    """Raise when url points into a private network, by name, literal address
    or what its name resolves to (so a public name for 10.0.0.5 is refused)."""
    host = urlparse(url).hostname or ""
    if _private_host(host):
        raise WebFailure("POLICY_DENIED", "Private network address refused (block_private_network)")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    except (OSError, UnicodeError):
        return  # Unresolvable: the request itself will fail.
    if any(_private_host(info[4][0]) for info in infos):
        raise WebFailure("POLICY_DENIED", "Name resolves to a private network address (block_private_network)")


async def _guard_hop(request) -> None:
    if PRIVATE_GUARD.get():
        await _refuse_private(str(request.url))


def proxy_settings(url: str | None) -> dict | None:
    """{"url", "server", "username", "password"} from a proxy URL such as
    http://user:pass@host:8080 or socks5://host:1080, or None."""
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https", "socks5", "socks5h") or not parsed.hostname or not parsed.port:
        raise ValueError("FRANKENSURF_PROXY must look like http://user:pass@host:port or socks5://host:port")
    server = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
    settings = {"url": url, "server": server}
    if parsed.username:
        from urllib.parse import unquote
        settings.update(username=unquote(parsed.username), password=unquote(parsed.password or ""))
    return settings


# Tools that reach the site from this machine, so the owner's proxy applies.
_PROXIED = frozenset({"http", "local", "steel", "camoufox", "scrapling", "scrapling_http", "patchright", "nodriver"})
# Of those, the ones that take a connection per read (egress.py): plain HTTP and
# the isolated stealth browsers. The shared local browser and Steel take their
# proxy when they start, so a pass through the pool never uses them.
_EGRESS_CAPABLE = frozenset({"http", "camoufox", "scrapling", "scrapling_http", "patchright", "nodriver"})


def _browser_proxy(settings):
    """Playwright's proxy argument (server plus optional credentials)."""
    return {key: settings[key] for key in ("server", "username", "password") if key in settings}


def _consent_leads(text) -> bool:
    """The page's text opens with a cookie or consent banner."""
    from .main_content import _CONSENT
    return len(_CONSENT.findall((text or "")[:2500])) >= 3


# Failures of the tool itself, not of the page.
_TOOL_OUTAGES = frozenset({"PROVIDER_DOWN", "PROVIDER_UNAVAILABLE", "TIMEOUT"})

_PRIORS_CACHE = {}


def _load_priors():
    if "vendors" not in _PRIORS_CACHE:
        try:
            data = json.loads(Path(__file__).with_name("bundled_wall_priors.json").read_text(encoding="utf-8"))
            valid = data.get("schema") == "frankensurf.wall-priors/v1"
            vendors = data.get("vendors") if valid else None
            _PRIORS_CACHE["vendors"] = {
                vendor: {tool: [int(pair[0]), int(pair[1])] for tool, pair in tools.items()
                         if isinstance(pair, list) and len(pair) == 2}
                for vendor, tools in (vendors or {}).items() if vendor in WALL_VENDORS and isinstance(tools, dict)}
            paid = data.get("paid") if valid and isinstance(data.get("paid"), dict) else {}
            _PRIORS_CACHE["paid"] = {
                tool: {"overall": [int(entry["overall"][0]), int(entry["overall"][1])],
                       "cost_usd": entry.get("cost_usd") if isinstance(entry.get("cost_usd"), (int, float)) else None}
                for tool, entry in paid.items()
                if isinstance(entry, dict) and isinstance(entry.get("overall"), list) and len(entry["overall"]) == 2}
        except (OSError, ValueError, TypeError, KeyError):
            _PRIORS_CACHE.update(vendors={}, paid={})
    return _PRIORS_CACHE


def _wall_priors():
    """Bundled per-vendor counts: {vendor: {tool: [wins, tries]}} (read once)."""
    return _load_priors()["vendors"]


def _paid_priors():
    """Bundled paid-tool facts: {tool: {"overall": [wins, tries], "cost_usd": float | None}}."""
    return _load_priors().get("paid", {})


WALL_VENDORS = ("cloudflare", "akamai", "datadome", "perimeterx", "imperva", "kasada", "aws_waf",
                "vercel", "sucuri")


def wall_vendor(headers, body: str = "") -> str | None:
    """Which bot-defence service answered, from the response's own headers,
    cookies and challenge page. Infrastructure vendors, not sites."""
    try:
        items = [(str(key).lower(), str(value).lower()) for key, value in headers.multi_items()]
    except AttributeError:
        items = [(str(key).lower(), str(value).lower()) for key, value in dict(headers or {}).items()]
    names = {key for key, _ in items}
    values = " ".join(f"{key}={value}" for key, value in items)
    body = (body or "")[:20000].lower()
    if "cf-ray" in names or "cf-mitigated" in names or "server=cloudflare" in values or "/cdn-cgi/challenge" in body:
        return "cloudflare"
    if "x-datadome" in names or "datadome=" in values or "captcha-delivery.com" in body:
        return "datadome"
    if "x-px" in names or "_px" in values and "set-cookie" in names or "perimeterx" in body or "px-captcha" in body:
        return "perimeterx"
    if "x-kpsdk-ct" in names or "x-kpsdk-r" in names or "kpsdk" in body:
        return "kasada"
    if "incap_ses" in values or "visid_incap" in values or "x-iinfo" in names or "incapsula" in body:
        return "imperva"
    if "akamaighost" in values or "akamai-grn" in names or "_abck=" in values or "bm_sz=" in values:
        return "akamai"
    if "x-amzn-waf-action" in names or "aws-waf-token" in values or "awswaf" in body:
        return "aws_waf"
    if "x-vercel-mitigated" in names or "vercel security checkpoint" in body:
        return "vercel"
    if "x-sucuri-id" in names or "sucuri website firewall" in body:
        return "sucuri"
    return None


def _user_agent() -> str:
    """Plain HTTP reads say who they are, with a contact URL, as polite-bot
    policies ask (Wikimedia refuses agents without one)."""
    try:
        from importlib.metadata import version
        release = version("frankensurf")
    except Exception:
        release = "dev"
    return f"FrankenSurf/{release} (+https://frankensurf.dev)"


def default_state_dir() -> str:
    """Where the CLI and MCP server keep state: $FRANKENSURF_STATE, else one
    per-user folder, so state doesn't land in whatever directory a client starts in."""
    return os.environ.get("FRANKENSURF_STATE") or str(Path.home() / ".local" / "share" / "frankensurf" / "state")


class Runtime:
    """Local-first async web runtime. Observation receipts do not certify listing availability.

    ``steel_api_url`` is a self-hosted Steel API (hosted Steel Cloud is the
    ``steel_cloud`` provider). local_cdp opens a new tab in an explicitly
    configured browser, never exports cookies.
    """
    def __init__(self, state_dir: str | Path = "state", steel_api_url: str | None = None,
                 local_cdp_url: str | None = None, concurrency: int = 4,
                 per_domain: int = 1, domain_delay: float = 0.25,
                 transport=None, identity_registry: str | Path | None = None,
                 plugin_catalog=None, plugin_config_path: str | Path | None = None):
        self.state_dir = Path(state_dir).expanduser()
        from .plugin_catalog import PluginCatalog, build_plugin_catalog
        if plugin_catalog is not None and plugin_config_path is not None:
            raise ValueError("Use either plugin_catalog or plugin_config_path")
        if plugin_catalog is not None:
            if not isinstance(plugin_catalog, PluginCatalog):
                raise ValueError("plugin_catalog must be a PluginCatalog snapshot")
            catalog = plugin_catalog
        else:
            # Resolve compatibility globals once. Later mutation cannot change
            # the registries or plugin set owned by this Runtime.
            from . import providers as provider_module
            from . import search_plugins as search_module
            from . import adapters as adapter_module
            catalog = build_plugin_catalog(config_path=plugin_config_path,
                base_providers=provider_module.DEFAULT_PROVIDERS,
                base_searches=search_module.DEFAULT_SEARCHES,
                base_adapters=adapter_module.DEFAULT_ADAPTERS)
        self.steel_api_url = steel_api_url or os.getenv("FRANKENSURF_STEEL_URL")
        # Servers that read URLs from search results or users should set this:
        # every read then refuses private network addresses.
        self.block_private_network = os.getenv("FRANKENSURF_BLOCK_PRIVATE_NETWORK", "").lower() in (
            "1", "true", "yes")
        # A proxy the owner brings (a residential or mobile pool for servers whose
        # data-centre address sites refuse). From the environment only: it never
        # appears in policies, receipts or traces (receipts say via_proxy).
        self.proxy = proxy_settings(os.getenv("FRANKENSURF_PROXY"))
        self.local_cdp_url = local_cdp_url or os.getenv("FRANKENSURF_LOCAL_CDP")
        # Opt-ins the owner can set once for every read (an MCP server, say).
        from .hosted_providers import _setting
        self.real_browser_default = (_setting("real_browser") or "").lower() in ("1", "true", "yes")
        # Other connections the owner configured (FRANKENSURF_EGRESS); never logged.
        from .egress import pool as _egress_pool
        self.egress_pool = _egress_pool()
        self._egress_http = {}
        self.lightning_default = (_setting("lightning") or "").lower() in ("1", "true", "yes")
        if self.steel_api_url:
            p = urlparse(self.steel_api_url)
            if p.scheme not in {"http", "https"} or not p.hostname:
                raise ValueError("steel_api_url must be an HTTP(S) Steel API URL")
        if concurrency < 1 or per_domain < 1 or domain_delay < 0:
            raise ValueError("invalid concurrency")
        self.identities = IdentityRegistry(identity_registry)
        from .identity_snapshots import (
            bind_owner_visible_capture_strategy,
            owner_visible_capture_binding_id,
        )
        strategy = bind_owner_visible_capture_strategy()
        strategy_binding_id = owner_visible_capture_binding_id(strategy)
        if (not _valid_plugin_id(strategy.id)
                or not isinstance(strategy.version, str)
                or _PLUGIN_VERSION.fullmatch(strategy.version) is None
                or re.fullmatch(r"[0-9a-f]{64}", strategy_binding_id)
                    is None
                or strategy_binding_id
                    != owner_visible_capture_binding_id()):
            raise ValueError("Invalid Core capture strategy")

        def current_capture_binding(
                execution=strategy,
                binding=owner_visible_capture_binding_id):
            return binding(execution)

        self._owner_visible_capture_strategy = _IdentityCaptureStrategy(
            strategy.id, strategy.version, strategy_binding_id,
            strategy.health, strategy.acquire,
            current_capture_binding)
        from .routes import RouteRecipeRegistry
        self.routes = RouteRecipeRegistry(self.state_dir / "routes" / "recipes.json")
        from .site_modules import SiteModuleRegistry
        self.site_modules = SiteModuleRegistry(self.state_dir / "routes" / "site-modules.json")
        self._identity_browsers = {}
        self._global = asyncio.Semaphore(concurrency)
        self._per_domain_count = per_domain
        self._domain_sems, self._domain_next = {}, {}
        self._domain_delay = domain_delay
        self._blocked_domains = {}
        self._transport = transport
        self._http = None
        self._pw = self._local_browser = self._steel_browser = self._cdp_browser = None
        self._steel_session = None
        self._steel_page = None
        self._browser_lock = asyncio.Lock()
        self._steel_lock = asyncio.Lock()  # upstream OSS Steel is single-session
        # Session construction is deliberately last: invalid Runtime arguments
        # and core registry failures cannot strand factory-created executions.
        self.plugin_catalog = catalog
        self._plugin_session = catalog.open_session()
        self.providers = self._plugin_session.providers
        self.searches = self._plugin_session.searches
        self.adapters = self._plugin_session.adapters

    def inspect_plugins(self):
        """Return the immutable, secret-free startup catalog description."""
        return self.plugin_catalog.inspect()


    async def __aenter__(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "evidence").mkdir(exist_ok=True)
        (self.state_dir / "traces").mkdir(exist_ok=True)
        (self.state_dir / "cache").mkdir(exist_ok=True)
        from .bot_auth import sign_hop
        self._http = httpx.AsyncClient(follow_redirects=True, transport=self._transport,
                                       **({"proxy": self.proxy["url"]} if self.proxy and self._transport is None else {}),
                                       headers={"User-Agent": _user_agent()},
                                       event_hooks={"request": [sign_hop, _guard_hop]})
        try:
            await self._plugin_session.start()
        except BaseException:
            await self._http.aclose()
            self._http = None
            raise
        return self

    async def __aexit__(self, *args):
        plugin_error = None
        try:
            await self._plugin_session.close()
        except BaseException as error:
            plugin_error = error
        try:
            if self._local_browser:
                try: await self._local_browser.close()
                except Exception: pass
            # Never close the user's CDP browser. Stopping Playwright only disconnects it.
            if self._pw:
                try: await self._pw.stop()
                except Exception: pass
            if self._steel_session and self._http:
                try:
                    await self._http.post(self.steel_api_url.rstrip("/") + "/v1/sessions/" + self._steel_session + "/release", timeout=10)
                except httpx.HTTPError: pass
            for client in self._egress_http.values():
                try:
                    await client.aclose()
                except Exception:
                    pass
            self._egress_http.clear()
            if self._http:
                await self._http.aclose()
        finally:
            self._http = None
        if plugin_error is not None and (not args or args[0] is None):
            raise plugin_error

    def _save_bytes(self, data: bytes, extension: str) -> dict:
        digest = hashlib.sha256(data).hexdigest()
        from .repair import _write_private_bytes
        return _write_private_bytes(
            self.state_dir, ("evidence", digest + extension), data)

    def _prepare_bytes(self, data: bytes, extension: str):
        """Describe content-addressed evidence without writing it yet."""
        digest = hashlib.sha256(data).hexdigest()
        from .repair import _artifact_path
        descriptor = {
            "path": str(_artifact_path(
                self.state_dir, ("evidence", digest + extension))),
            "sha256": digest,
            "bytes": len(data),
        }
        expected = dict(descriptor)

        def commit():
            retained = self._save_bytes(data, extension)
            if retained != expected:
                raise OSError("Evidence descriptor changed")

        return descriptor, commit

    def _image_evidence(self, data: bytes) -> dict:
        """Retain an image now, or describe a semantic-attempt artifact."""
        if not _STAGE_IMAGE_EVIDENCE.get():
            return self._save_bytes(data, ".image")
        descriptor, commit = self._prepare_bytes(data, ".image")
        return {**descriptor, "_evidence_commit": commit}

    def _prepare_provider_failure_evidence(
            self, raw: bytes, content_type: str):
        """Describe evidence before returning its one-shot atomic writer."""
        media_type = content_type.partition(";")[0].strip().lower()
        extension = {
            "application/json": ".json",
            "text/html": ".html",
            "text/plain": ".txt",
        }.get(media_type, ".bin")
        digest = hashlib.sha256(raw).hexdigest()
        from .repair import _artifact_path
        descriptor = {
            "path": str(_artifact_path(
                self.state_dir, ("evidence", digest + extension))),
            "sha256": digest,
            "bytes": len(raw),
        }

        def commit():
            retained = self._save_bytes(raw, extension)
            if retained != descriptor:
                raise OSError("Provider evidence descriptor changed")

        return descriptor, commit

    def _cache_key(self, url, adapter, policy, resolved=None, *, route_scope=None,
                   route_seed_scope=None):
        adapter_version = None
        if adapter:
            adapter_version = {
                "version": self.adapters.require_enabled(adapter).version,
                "binding_id": self.adapters.binding_id(adapter)}
        provider_version = None
        if resolved is not None:
            provider_id = policy.provider or resolved.provider
            provider_version = {
                "id": provider_id,
                "version": self.providers.require_enabled(
                    provider_id, policy,
                    operation="extract" if adapter else "read").version,
                "binding_id": self.providers.binding_id(provider_id)}
        elif policy.provider:
            provider_version = {
                "version": self.providers.require_enabled(
                    policy.provider, policy,
                    operation="extract" if adapter else "read").version,
                "binding_id": self.providers.binding_id(policy.provider)}
        elif policy.provider_candidates is not None and resolved is None:
            manifests = {item["id"]: item for item in self.providers.inspect()}
            provider_version = [manifests.get(identifier) for identifier in policy.provider_candidates]
        elif resolved is None:
            manifests = {item["id"]: item for item in self.providers.inspect()}
            candidates = self.providers.candidates(policy,
                configured=("steel",) if self.steel_api_url else (),
                operation="extract" if adapter else "read")
            provider_version = [manifests.get(identifier) for identifier in candidates]
        key = [url, adapter, adapter_version, provider_version, asdict(policy)]
        if resolved is not None: key.append(resolved.cache_scope)
        if route_scope is not None: key.append({"operator_route_recipe": route_scope})
        if route_seed_scope is not None:
            key.append({"catalog_route_seeds": route_seed_scope})
        return hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()

    def _cache_lookup(self, key, policy):
        if policy.freshness == "now": return None
        file = self.state_dir / "cache" / (key + ".json")
        if not file.exists(): return None
        result = json.loads(file.read_text())
        status = result["receipt"].get("http_status")
        # Older releases could cache an unclassified 4xx as observed. Reacquire
        # instead of perpetuating that invalid success receipt.
        if isinstance(status, int) and _status_failure(status): return None
        observed = datetime.fromisoformat(result["receipt"]["observed_at"])
        age = (datetime.now(timezone.utc) - observed).total_seconds()
        ttl = {"hour": 3600, "day": 86400, "cached": float("inf")}[policy.freshness]
        if age > ttl: return None
        result["receipt"]["cache_hit"] = True
        result["receipt"]["freshness_seconds"] = age
        result["receipt"]["source_trace_id"] = result["receipt"]["trace_id"]
        result["receipt"]["trace_id"] = uuid.uuid4().hex
        return result

    async def _get_http(self, url, policy):
        assert self._http
        signing = None
        try:
            headers = ({"Accept": "text/markdown, text/html;q=0.9, */*;q=0.8"}
                       if policy.prefer_markdown else None)
            from .bot_auth import SIGNER, current_signer
            from .profiles import ACTIVE
            signing = SIGNER.set(current_signer() if policy.sign_requests else None)
            profile = ACTIVE.get() if policy.profile else None
            client = self._http
            from .egress import CURRENT as EGRESS
            if EGRESS.get() is not None and self._transport is None:
                client = self._egress_client(EGRESS.get())
            if profile is not None:
                from .bot_auth import sign_hop
                headers = dict(headers or {})
                cookie = profile.cookie_header(url)
                if cookie:
                    headers["Cookie"] = cookie
                if profile.fingerprint.get("user_agent"):
                    headers["User-Agent"] = profile.fingerprint["user_agent"]
                client = httpx.AsyncClient(follow_redirects=True, transport=self._transport,
                                           event_hooks={"request": [sign_hop]})
            async with _closing(client, client is not self._http and client not in self._egress_http.values()), client.stream(
                    "GET", url, timeout=policy.timeout_seconds, headers=headers) as response:
                final_url = str(response.url)
                _validate_url(final_url)
                failure = _status_failure(response.status_code)
                if failure:
                    refused = WebFailure(failure, "HTTP response requires stop", response.status_code,
                                         response_url=final_url)
                    refused.wall_vendor = wall_vendor(response.headers)
                    raise refused
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > policy.max_bytes: raise WebFailure("LIMIT_EXCEEDED", "Response byte limit exceeded", response_url=final_url)
                data = bytes(raw)
                if profile is not None:
                    _merge_response_cookies(profile, response)
                content = data.decode(response.encoding or "utf-8", errors="replace")
                if "html" in response.headers.get("content-type", "") and _challenge(content):
                    challenge = WebFailure("CAPTCHA", "Challenge page observed", response.status_code,
                                           response_url=final_url)
                    challenge.wall_vendor = wall_vendor(response.headers, content)
                    raise challenge
                return {"url": str(response.url), "content": content, "raw": data,
                        "content_type": response.headers.get("content-type", ""), "http_status": response.status_code,
                        "headers": {k: v for k, v in response.headers.items() if k in _SAFE_HEADERS}}
        except httpx.TimeoutException as exc:
            raise WebFailure("TIMEOUT", "HTTP request timed out") from exc
        except httpx.HTTPError as exc:
            raise WebFailure("PROVIDER_DOWN", "HTTP request failed") from exc
        finally:
            if signing is not None:
                SIGNER.reset(signing)

    async def _browser(self, provider, policy):
        async with self._browser_lock:
            if not self._pw:
                from playwright.async_api import async_playwright
                self._pw = await async_playwright().start()
            if provider == "local":
                if not policy.allow_local_browser: raise WebFailure("POLICY_DENIED", "Local browser disabled")
                if not self._local_browser:
                    self._local_browser = await self._pw.chromium.launch(
                        headless=True, **({"proxy": _browser_proxy(self.proxy)} if self.proxy else {}))
                return self._local_browser
            if provider == "local_cdp":
                raise WebFailure("IDENTITY_REQUIRED", "Local CDP requires an enrolled named identity")
            if not self.steel_api_url:
                raise WebFailure("PROVIDER_DOWN", "Self-hosted Steel endpoint is not configured")
            if not self._steel_browser:
                response = await self._http.post(self.steel_api_url.rstrip("/") + "/v1/sessions", json={"blockAds": True, **({"proxyUrl": self.proxy["url"]} if self.proxy else {})}, timeout=30)
                if response.status_code >= 400: raise WebFailure("PROVIDER_DOWN", "Steel session creation failed", response.status_code)
                session = response.json()
                self._steel_session = session["id"]
                endpoint = session.get("websocketUrl") or session.get("websocket_url")
                if not endpoint:
                    endpoint = self.steel_api_url.replace("http://", "ws://").replace("https://", "wss://").rstrip("/") + "/?sessionId=" + self._steel_session
                self._steel_browser = await self._pw.chromium.connect_over_cdp(endpoint)
            return self._steel_browser

    async def _browser_representation(self, page, response, policy):
        content_type = response.headers.get("content-type", "") if response else ""
        json_response = "json" in content_type.lower()
        raw = None
        # Shopify .js endpoints commonly send JSON with a JavaScript MIME type.
        # Validate bytes as JSON; never execute downloaded JavaScript.
        if response and (json_response or any(kind in content_type.lower() for kind in ("javascript","text/plain"))):
            raw = await response.body()
            if len(raw) > policy.max_bytes:
                raise WebFailure("LIMIT_EXCEEDED", "Browser response byte limit exceeded")
            candidate = raw.decode("utf-8-sig",errors="replace")
            if not json_response:
                try: json.loads(candidate)
                except (ValueError,TypeError): pass
                else:
                    json_response = True
                    content_type += "; representation=json"
        if json_response:
            if raw is None: raw = await response.body()
            content = raw.decode("utf-8-sig",errors="replace")
        else:
            content = await _rendered_html(page)
            raw = content.encode()
            content_type = "text/html; rendered=1"
        if len(raw) > policy.max_bytes:
            raise WebFailure("LIMIT_EXCEEDED", "Browser response byte limit exceeded")
        return content,raw,content_type

    async def _wait_content_readiness(
            self, page, policy, deadline, timeout_error):
        if policy.content_ready_selector is None:
            return None
        available = max(0.0, deadline - time.monotonic())
        capture_reserve = min(available / 2, 5.0)
        wait_timeout = min(policy.content_ready_timeout_seconds,
            max(0.0, available - capture_reserve))
        if wait_timeout <= 0:
            status = "timed_out"
        else:
            try:
                await page.locator(
                    policy.content_ready_selector).first.wait_for(
                        state=policy.wait_state, timeout=wait_timeout * 1000)
                status = "satisfied"
            except timeout_error:
                status = "timed_out"
        return {"status": status,
                "timeout_seconds": policy.content_ready_timeout_seconds}

    async def _get_browser(self, url, policy, provider):
        from playwright.async_api import TimeoutError as PWTimeout, Error as PWError
        page = context = None
        capture = capture_listener = None
        deadline = time.monotonic() + policy.timeout_seconds
        try:
            browser = await self._browser(provider, policy)
            from .profiles import ACTIVE
            profile = ACTIVE.get() if policy.profile else None
            if profile is not None:
                context = await browser.new_context(**profile.context_options())
                page = await context.new_page()
            elif provider == "steel":
                # Reuse one owned page. Upstream OSS instrumentation races on very fast
                # create/close targets (e.g. a 403) and can terminate its Node process.
                context = browser.contexts[0] if browser.contexts else await browser.new_context()
                if self._steel_page is None or self._steel_page.is_closed():
                    self._steel_page = await context.new_page()
                    await self._steel_page.wait_for_timeout(300)
                page = self._steel_page
            else:
                context = await browser.new_context()
                page = await context.new_page()
            if policy.capture_json_responses:
                capture = _JsonCapture(policy.capture_json_max_items, policy.capture_json_max_bytes)
                capture_listener = capture.listener()
                page.on("response", capture_listener)
            session_site = None
            if profile is None and provider == "local" and policy.site_session_hours and not policy.identity:
                session_site = urlparse(url).netloc
                cookies = self._site_session(session_site, policy.site_session_hours)
                if cookies:
                    try:
                        await context.add_cookies(cookies)
                    except PWError:
                        pass
            if policy.public_entry_url and provider == "local":
                from .public_entry import valid_entry
                if valid_entry(policy.public_entry_url, url):
                    await page.goto(policy.public_entry_url, wait_until="domcontentloaded",
                                    timeout=policy.timeout_seconds * 1000)
                    await page.wait_for_timeout(min(policy.settle_ms or 1500, 4000))
            response = await page.goto(url, wait_until="domcontentloaded", timeout=policy.timeout_seconds * 1000,
                                       **({"referer": policy.public_entry_url}
                                          if policy.public_entry_url and provider == "local" else {}))
            final_url = page.url
            _validate_url(final_url)
            status = response.status if response else None
            failure = _status_failure(status) if status else None
            if failure: raise WebFailure(failure, "Browser response requires stop", status, response_url=final_url)
            if policy.wait_selector:
                await page.locator(policy.wait_selector).first.wait_for(state=policy.wait_state, timeout=policy.timeout_seconds * 1000)
            if policy.settle_ms: await page.wait_for_timeout(policy.settle_ms)
            await _scroll_for_lazy(page, _scroll_screens(policy))
            performed = None
            if policy.interactions:
                from .interactions import run as interact
                performed = await interact(page, policy.interactions)
            content_readiness = await self._wait_content_readiness(
                page, policy, deadline, PWTimeout)
            content,raw,content_type = await self._browser_representation(page,response,policy)
            if _challenge(content): raise WebFailure("CAPTCHA", "Browser challenge page observed", status, response_url=page.url)
            if session_site is not None:
                try:
                    self._save_site_session(session_site, await context.cookies())
                except PWError:
                    pass
            if profile is not None and profile.merge(await context.storage_state()):
                profile.changed = True
            screenshot = self._save_bytes(await page.screenshot(full_page=False), ".png")
            captured = None
            if capture is not None:
                captured = await capture.settle(min(5.0, deadline - time.monotonic()))
            return {"url": page.url, "content": content, "raw": raw,
                    "content_type": content_type, "http_status": status,
                    "headers": {}, "screenshot": screenshot,
                    **({"captured_json": captured} if captured is not None else {}),
                    **({"interactions": performed} if performed is not None else {}),
                    **({"content_readiness": content_readiness}
                       if content_readiness is not None else {})}
        except WebFailure: raise
        except PWTimeout as exc: raise WebFailure("TIMEOUT", "Browser navigation or readiness timed out") from exc
        except PWError as exc:
            code = _navigation_failure(str(exc))
            if code:
                raise WebFailure(code, "Target refused the page", response_url=url) from exc
            raise WebFailure("PROVIDER_DOWN", "Browser connection or execution failed") from exc
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise WebFailure("PROVIDER_DOWN", "Browser connection or execution failed") from exc
        finally:
            if page and capture_listener is not None:
                try: page.remove_listener("response", capture_listener)
                except Exception: pass
            if page and (provider != "steel" or profile is not None):
                try: await page.close()
                except PWError: pass
            if context and (provider not in {"local_cdp", "steel"} or profile is not None):
                try: await context.close()
                except PWError: pass

    def _site_session_path(self, site):
        return self.state_dir / "site-sessions" / (hashlib.sha256(site.encode()).hexdigest() + ".json")

    def _site_session(self, site, hours):
        """Cookies the local browser kept for this site, if still fresh. Anonymous
        state only: never a profile's, never an identity's."""
        try:
            saved = json.loads(self._site_session_path(site).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if time.time() - saved.get("at", 0) > hours * 3600:
            return []
        now = time.time()
        return [cookie for cookie in saved.get("cookies") or []
                if isinstance(cookie, dict) and (cookie.get("expires", -1) in (-1, None) or cookie["expires"] > now)]

    def _save_site_session(self, site, cookies):
        host = site.split(":")[0].removeprefix("www.")
        kept = [cookie for cookie in cookies if str(cookie.get("domain", "")).lstrip(".").removeprefix("www.")
                in (host,) or host.endswith("." + str(cookie.get("domain", "")).lstrip("."))][:200]
        path = self._site_session_path(site)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".session-", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"site": site, "at": time.time(), "cookies": kept}, stream)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)

    def identity_status(self, identity_id: str | None = None) -> dict:
        return self.identities.status(identity_id)

    @staticmethod
    def _identity_path(value):
        if re.match(r"^[A-Za-z]:[\\/]", value):
            return ntpath.normcase(ntpath.normpath(value))
        return os.path.normcase(os.path.realpath(os.path.expanduser(value)))

    async def _identity_cleanup(self, operation):
        try:
            await asyncio.wait_for(operation,timeout=0.5)
        except Exception:
            pass

    async def _identity_step(self, operation, policy, *, probe=False):
        try:
            return await asyncio.wait_for(operation,timeout=policy.timeout_seconds)
        except TimeoutError as exc:
            raise WebFailure("IDENTITY_EXECUTOR_OFFLINE" if probe else "TIMEOUT",
                             "Identity execution did not respond within its time limit") from exc

    async def _identity_context(self, resolved, policy):
        from playwright.async_api import Error as PWError
        self.identities.recheck(resolved)
        async with self._browser_lock:
            if not self._pw:
                from playwright.async_api import async_playwright
                self._pw = await async_playwright().start()
            browser = self._identity_browsers.get(resolved.cache_scope)
            if browser is None or not browser.is_connected():
                try:
                    browser = await self._pw.chromium.connect_over_cdp(
                        resolved.endpoint, timeout=policy.timeout_seconds * 1000)
                except PWError as exc:
                    raise WebFailure("IDENTITY_EXECUTOR_OFFLINE", "Enrolled local browser is unreachable") from exc
                self._identity_browsers[resolved.cache_scope] = browser
            session = None
            try:
                session = await browser.new_browser_cdp_session()
                arguments = (await session.send("Browser.getBrowserCommandLine")).get("arguments", [])
                flags = {}
                for i, argument in enumerate(arguments):
                    if argument.startswith("--"):
                        key, sep, value = argument.partition("=")
                        if not sep and i + 1 < len(arguments) and not arguments[i+1].startswith("--"):
                            value = arguments[i+1]
                        flags[key] = value
                actual_dir = flags.get("--user-data-dir")
                actual_profile = flags.get("--profile-directory") or "Default"
                if not actual_dir or self._identity_path(actual_dir) != self._identity_path(resolved.user_data_dir) or actual_profile != resolved.profile_directory:
                    raise WebFailure("IDENTITY_PROFILE_MISMATCH", "Browser does not match the enrolled canonical profile")
                browser_contexts = (await session.send("Target.getBrowserContexts")).get("browserContextIds", [])
                if browser_contexts or len(browser.contexts) != 1:
                    raise WebFailure("IDENTITY_PROFILE_MISMATCH", "Canonical default browser context is ambiguous")
                context = browser.contexts[0]  # only after explicit profile + default-context attestation
                self.identities.recheck(resolved)
                return context
            except WebFailure:
                raise
            except PWError as exc:
                raise WebFailure("IDENTITY_PROFILE_MISMATCH", "Browser profile binding could not be verified") from exc
            finally:
                if session is not None:
                    await self._identity_cleanup(session.detach())

    def _identity_allows(self, resolved, url, *, image=False):
        try:
            return self.identities.permits_url(resolved,url,image=image)
        except IdentityFailure as exc:
            if exc.code == "IDENTITY_DOMAIN_DENIED": return False
            raise

    async def _identity_page(self, context, resolved, capture=None):
        # Logged-in reads behave like a normal browser tab: page scripts run and
        # the page loads whatever it needs. Core keeps two rules: the top-level
        # page stays inside the identity's domains, and Core itself never clicks
        # or types. CDP Fetch pauses top-level document requests, including
        # redirect hops that Playwright route() misses.
        from playwright.async_api import Error as PWError
        page = await context.new_page()
        denied = []
        try:
            session = await context.new_cdp_session(page)
            tree = await session.send("Page.getFrameTree")
            main_frame = tree["frameTree"]["frame"]["id"]
            async def scope_guard(event):
                request_id = event["requestId"]
                url = event.get("request", {}).get("url", "")
                top_level = (event.get("resourceType") == "Document"
                             and event.get("frameId", main_frame) == main_frame)
                try:
                    allowed = not top_level or self._identity_allows(resolved, url)
                except IdentityFailure as exc:
                    denied.append(exc.code)
                    allowed = False
                if not allowed and not denied:
                    denied.append("IDENTITY_DOMAIN_DENIED")
                try:
                    await session.send("Fetch.continueRequest" if allowed else "Fetch.failRequest",
                        {"requestId":request_id} if allowed else {"requestId":request_id,"errorReason":"BlockedByClient"})
                except PWError:
                    pass  # Owned tab may have closed while a request was paused.
            session.on("Fetch.requestPaused",scope_guard)
            # Popups opened by page scripts are closed so the owner profile is
            # left as it was found.
            page.on("popup", lambda popup: asyncio.ensure_future(
                self._identity_cleanup(popup.close())))
            if capture is not None:
                page.on("response", capture.listener())
            await session.send("Fetch.enable",{"patterns":[{"urlPattern":"*","resourceType":"Document","requestStage":"Request"}]})
            return page, denied
        except PWError as exc:
            await self._identity_cleanup(page.close())
            raise WebFailure("IDENTITY_PROFILE_MISMATCH", "Scoped browser execution could not be established") from exc

    def _identity_capture_strategy(self, resolved):
        return (self._owner_visible_capture_strategy
                if getattr(resolved, "snapshot_policy", None) else None)

    @staticmethod
    def _require_current_capture_strategy(strategy):
        try:
            current = strategy.current_binding_id()
        except (OSError, UnicodeError):
            current = None
        if current != strategy.binding_id:
            raise WebFailure("IDENTITY_CHANGED",
                "Core capture strategy changed during execution")

    async def _identity_health(self, context, resolved, policy, *,
                               capture_strategy=None,
                               allow_capture_strategy=True):
        if (capture_strategy is None
                and allow_capture_strategy
                and getattr(resolved, "snapshot_policy", None)):
            capture_strategy = self._identity_capture_strategy(resolved)
        if capture_strategy is not None:
            self._require_current_capture_strategy(capture_strategy)
            result = await capture_strategy.health(
                context, resolved, policy, self.identities)
            self._require_current_capture_strategy(capture_strategy)
            return result
        check = resolved.auth_check
        if not check: return "not_configured"
        from playwright.async_api import Error as PWError, TimeoutError as PWTimeout
        page = None
        try:
            self.identities.recheck(resolved)
            check_url = check["url"] if "url" in check else check["check_url"]
            if not self._identity_allows(resolved, check_url):
                raise WebFailure("IDENTITY_POLICY_DENIED", "Authentication probe is outside identity scope")
            page, denied = await self._identity_page(context, resolved)
            response = await page.goto(check_url, wait_until="domcontentloaded", timeout=policy.timeout_seconds*1000)
            if denied or not self._identity_allows(resolved, page.url):
                raise WebFailure(denied[-1] if denied else "IDENTITY_DOMAIN_DENIED", "Authentication probe redirected outside identity scope")
            status = response.status if response else None
            if status == 401:
                raise WebFailure("IDENTITY_REAUTH_REQUIRED", "Browser identity requires operator authentication")
            code = _status_failure(status) if status else None
            if code: raise WebFailure(code, "Authentication probe failed", status)
            if _challenge(await page.content()):
                raise WebFailure("CAPTCHA", "Authentication probe encountered a challenge")
            if check.get("login_selector") and await page.locator(check["login_selector"]).first.is_visible():
                raise WebFailure("IDENTITY_REAUTH_REQUIRED", "Browser identity requires operator authentication")
            selector = check.get("authenticated_selector")
            if selector:
                try: await page.locator(selector).first.wait_for(state="visible", timeout=policy.timeout_seconds*1000)
                except PWTimeout as exc:
                    raise WebFailure("IDENTITY_REAUTH_REQUIRED", "Expected authenticated state was not observed") from exc
            return "verified" if selector else "probe_observed"
        except WebFailure: raise
        except PWError as exc:
            if 'denied' in locals() and denied:
                raise WebFailure(denied[-1], "Identity authentication probe was denied by local policy") from exc
            raise WebFailure("IDENTITY_EXECUTOR_OFFLINE", "Authentication probe could not execute") from exc
        finally:
            if page:
                await self._identity_cleanup(page.close())

    async def _get_identity_browser(self, url, policy, resolved, context, *,
                                    capture_strategy=None):
        if (capture_strategy is None
                and getattr(resolved, "snapshot_policy", None)):
            capture_strategy = self._identity_capture_strategy(resolved)
        if capture_strategy is not None:
            self._require_current_capture_strategy(capture_strategy)
            response = await capture_strategy.acquire(
                url, policy, resolved, context, self.identities)
            self._require_current_capture_strategy(capture_strategy)
            claims = response.get("visible_snapshot")
            expected = {
                "scope": "owner_opened_visible_region",
                "navigation_performed": False,
                "source_refresh_performed": False,
                "source_freshness": "unknown",
                "gallery_coverage": "partial_unverified_candidates",
            }
            if type(claims) is not dict or claims != expected:
                raise WebFailure("IDENTITY_CHANGED",
                    "Core capture strategy returned invalid provenance")
            response["visible_snapshot"] = {
                "strategy": capture_strategy.id,
                "strategy_version": capture_strategy.version,
                "strategy_binding_id": capture_strategy.binding_id,
                **expected,
            }
            response["screenshot"] = self._save_bytes(response.pop("snapshot_screenshot_bytes"), ".png")
            return response
        from playwright.async_api import TimeoutError as PWTimeout, Error as PWError
        page = None
        denied = []
        deadline = time.monotonic() + policy.timeout_seconds
        try:
            self.identities.recheck(resolved)
            if not self._identity_allows(resolved, url):
                raise WebFailure("IDENTITY_POLICY_DENIED", "Page is outside identity domain scope")
            capture = (_JsonCapture(policy.capture_json_max_items, policy.capture_json_max_bytes)
                       if policy.capture_json_responses else None)
            page, denied = await self._identity_page(context, resolved, capture)
            response = await page.goto(url, wait_until="domcontentloaded", timeout=policy.timeout_seconds*1000)
            if denied or not self._identity_allows(resolved, page.url):
                raise WebFailure(denied[-1] if denied else "IDENTITY_DOMAIN_DENIED", "Page redirected outside identity domain scope")
            status = response.status if response else None
            failure = _status_failure(status) if status else None
            if failure: raise WebFailure("IDENTITY_REAUTH_REQUIRED" if failure == "AUTH_REQUIRED" else failure, "Identity page could not be retrieved", status, response_url=page.url)
            if policy.wait_selector:
                await page.locator(policy.wait_selector).first.wait_for(state=policy.wait_state, timeout=policy.timeout_seconds*1000)
            if policy.settle_ms: await page.wait_for_timeout(policy.settle_ms)
            await _scroll_for_lazy(page, _scroll_screens(policy))
            content_readiness = await self._wait_content_readiness(
                page, policy, deadline, PWTimeout)
            content,raw,content_type = await self._browser_representation(page,response,policy)
            if _challenge(content): raise WebFailure("CAPTCHA", "Identity browser challenge observed", status)
            self.identities.recheck(resolved)
            screenshot = self._save_bytes(await page.screenshot(full_page=False), ".png")
            captured = None
            if capture is not None:
                captured = await capture.settle(min(5.0, deadline - time.monotonic()))
            return {"url": page.url, "content": content, "raw": raw,
                    "content_type": content_type, "http_status": status,
                    "headers": {}, "screenshot": screenshot,
                    **({"captured_json": captured} if captured is not None else {}),
                    **({"content_readiness": content_readiness}
                       if content_readiness is not None else {})}
        except WebFailure: raise
        except PWTimeout as exc: raise WebFailure("TIMEOUT", "Identity browser navigation or readiness timed out") from exc
        except PWError as exc:
            if denied: raise WebFailure(denied[-1], "Identity page request was denied by local policy") from exc
            raise WebFailure("IDENTITY_EXECUTOR_OFFLINE", "Identity browser execution failed") from exc
        finally:
            if page:
                await self._identity_cleanup(page.close())

    async def _get_identity_image(self, url, policy, resolved, context, *,
                                  public_url=None):
        from playwright.async_api import Error as PWError, TimeoutError as PWTimeout
        response = None
        requested = url
        public_url = _output_url(
            public_url if public_url is not None else url,
            named_identity=True)
        try:
            _validate_url(url)
            for _ in range(6):
                self.identities.recheck(resolved)
                if not self._identity_allows(resolved, requested, image=True):
                    raise WebFailure("IDENTITY_DOMAIN_DENIED", "Image URL is outside identity image scope")
                response = await context.request.get(requested, timeout=policy.timeout_seconds*1000,
                                                     max_redirects=0, fail_on_status_code=False)
                status = response.status
                if status in {301,302,303,307,308}:
                    location = response.headers.get("location")
                    if not location: raise WebFailure("INVALID_IMAGE", "Image redirect lacks a destination")
                    destination = urljoin(requested, location)
                    _validate_url(destination)
                    if not self._identity_allows(resolved, destination, image=True):
                        raise WebFailure("IDENTITY_DOMAIN_DENIED", "Image redirect is outside identity scope")
                    await self._identity_cleanup(response.dispose()); response = None
                    requested = destination
                    continue
                failure = _status_failure(status)
                if failure: raise WebFailure("IDENTITY_REAUTH_REQUIRED" if failure == "AUTH_REQUIRED" else failure, "Identity image request failed", status)
                size = response.headers.get("content-length")
                if size and size.isdigit() and int(size) > policy.max_image_bytes:
                    raise WebFailure("LIMIT_EXCEEDED", "Image byte limit exceeded")
                raw = await response.body()
                if len(raw) > policy.max_image_bytes: raise WebFailure("LIMIT_EXCEEDED", "Image byte limit exceeded")
                with Image.open(io.BytesIO(raw)) as image:
                    image.load(); width,height,fmt = image.width,image.height,image.format
                self.identities.recheck(resolved)
                return {"url":public_url,"status":"decoded","content_type":response.headers.get("content-type", ""),
                        "width":width,"height":height,"format":fmt,**self._image_evidence(raw),
                        "observed_at":utcnow(),"failure":None,"identity":resolved.id,
                        "executor":resolved.executor_id,"authority_mode":resolved.authority_mode,"method":"local_cdp"}
            raise WebFailure("LIMIT_EXCEEDED", "Image redirect limit exceeded")
        except (WebFailure, IdentityFailure) as exc:
            return {"url":public_url,"status":"failed","failure":exc.code,"identity":resolved.id}
        except PWTimeout:
            return {"url":public_url,"status":"failed","failure":"TIMEOUT","identity":resolved.id}
        except PWError:
            return {"url":public_url,"status":"failed","failure":"IDENTITY_EXECUTOR_OFFLINE","identity":resolved.id}
        except (UnidentifiedImageError,OSError,Image.DecompressionBombError):
            return {"url":public_url,"status":"failed","failure":"INVALID_IMAGE","identity":resolved.id}
        finally:
            if response is not None:
                await self._identity_cleanup(response.dispose())

    async def _download_identity_images(self, urls, policy, resolved, context,
                                        *, private_sources=None):
        private_sources = private_sources or {}
        images = []
        for url in urls[:policy.max_images]:
            public_url = _output_url(url, named_identity=True)
            fetch_url = private_sources.get(public_url, url)
            async with self._global:
                try:
                    image = await self._identity_step(
                        self._get_identity_image(fetch_url, policy, resolved,
                            context, public_url=public_url), policy)
                    # Defense in depth: an executor result can never surface a
                    # signed/private fetch URL even if it reports one itself.
                    image["url"] = public_url
                    images.append(image)
                except WebFailure as exc:
                    images.append({"url":public_url,"status":"failed",
                        "failure":exc.code,"identity":resolved.id})
        return images

    async def _image(self, url, policy):
        attempts = []
        for index in range(policy.image_max_attempts):
            started = time.monotonic()
            result = await self._image_once(url, policy)
            attempts.append({"status": result["status"], "failure": result.get("failure"),
                             "http_status": result.get("http_status"),
                             "latency_ms": round((time.monotonic()-started)*1000)})
            if result["status"] == "decoded" or result.get("failure") not in policy.image_retry_failures or index+1 == policy.image_max_attempts:
                return {**result, "attempts": attempts}
            if policy.image_retry_delay_seconds:
                await asyncio.sleep(policy.image_retry_delay_seconds)

    async def _image_once(self, url, policy):
        try:
            _validate_url(url)
            response = await self._get_http(url, replace(policy, max_bytes=policy.max_image_bytes))
            with Image.open(io.BytesIO(response["raw"])) as image:
                image.load()
                width, height, fmt = image.width, image.height, image.format
            return {"url": url, "status": "decoded", "http_status": response["http_status"], "content_type": response["content_type"],
                    "width": width, "height": height, "format": fmt,
                    **self._image_evidence(response["raw"]), "observed_at": utcnow(), "failure": None}
        except WebFailure as exc:
            return {"url": url, "status": "failed", "failure": exc.code, "http_status": exc.http_status}
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
            return {"url": url, "status": "failed", "failure": "INVALID_IMAGE"}

    def _identity_provider_authority(self, resolved, policy, *,
                                     operation="read", expected=None):
        provider_id = resolved.provider
        if expected is not None:
            operation = expected.operation
        if policy.provider is not None and policy.provider != provider_id:
            raise WebFailure("IDENTITY_PROVIDER_DENIED",
                "Named identity is bound to a different provider")
        manifest = self.providers.require_enabled(
            provider_id, policy, operation=operation)
        binding_id = self.providers.binding_id(provider_id)
        if (not manifest.authentication or not manifest.rendering
                or not manifest.requires_local_browser
                or binding_id is None):
            raise WebFailure("IDENTITY_PROVIDER_DENIED",
                "Named identity requires its exact local authenticated provider binding")
        authority = _IdentityProviderAuthority(
            provider_id, manifest.version, binding_id, operation)
        if expected is not None and authority != expected:
            raise WebFailure("IDENTITY_PROVIDER_DENIED",
                "Named identity provider binding changed during execution")
        return authority

    def _action_provider_authority(self, provider_id, policy, action_class,
                                   contract, *, intent, expected=None):
        manifest = self.providers.require_action_enabled(
            provider_id, policy, action_class, contract, intent=intent)
        binding_id = self.providers.binding_id(provider_id)
        if (not manifest.authentication or not manifest.requires_local_browser
                or binding_id is None):
            raise WebFailure("IDENTITY_PROVIDER_DENIED",
                "Named identity action requires an exact local provider binding")
        authority = _IdentityProviderAuthority(
            provider_id, manifest.version, binding_id, "do")
        if expected is not None and authority != expected:
            raise WebFailure("IDENTITY_PROVIDER_DENIED",
                "Action provider binding changed during execution")
        return authority

    def _recheck_action_identity(self, resolved, url, required_action_classes,
                                 policy):
        """Recheck every independent identity grant in an action contract."""
        current = self.identities.recheck(resolved, url)
        for action_class in required_action_classes:
            granted = self.identities.resolve(
                resolved.id, url, provider=resolved.provider,
                action=action_class,
                allow_local_browser=policy.allow_local_browser)
            if granted.cache_scope != resolved.cache_scope:
                raise IdentityFailure(
                    "IDENTITY_CHANGED",
                    "Identity configuration changed during execution")
        return current

    def _action_provider_services(self, resolved, request, capability):
        from .providers import ProviderServices

        async def denied(*args, **kwargs):
            raise WebFailure("IDENTITY_POLICY_DENIED",
                "Authenticated actions cannot use public transports")

        async def authenticated_action(action_request, runner):
            from .browser_use_action_provider import LocalActionExecution
            if capability.state != "unused":
                capability.state = "violated"
                raise WebFailure("IDENTITY_POLICY_DENIED",
                    "Authenticated action capability is one-shot")
            capability.state = "active"
            if (action_request is not request or request.intent is not action_request.intent
                    or request.policy is not action_request.policy
                    or resolved.cache_scope != capability.identity_scope
                    or not callable(runner)):
                capability.state = "violated"
                raise WebFailure("IDENTITY_POLICY_DENIED",
                    "Authenticated action request exceeds its operation scope")
            try:
                self._recheck_action_identity(
                    resolved, request.intent.url,
                    capability.required_action_classes, request.policy)
                self._action_provider_authority(
                    capability.authority.provider_id, request.policy,
                    request.intent.action_class, request.intent.contract,
                    intent=request.intent,
                    expected=capability.authority)
                grant = LocalActionExecution(
                    resolved.endpoint, resolved.cache_scope,
                    resolved.profile_version, resolved.executor_id)
                packet = await runner(action_request, grant)
                encoded = json.dumps(packet, sort_keys=True,
                    separators=(",", ":"), ensure_ascii=False,
                    allow_nan=False).encode()
                if len(encoded) > request.policy.browser_do_packet_max_bytes:
                    raise WebFailure("LIMIT_EXCEEDED",
                        "Action provider packet exceeds WebPolicy")
                self._recheck_action_identity(
                    resolved, request.intent.url,
                    capability.required_action_classes, request.policy)
                self._action_provider_authority(
                    capability.authority.provider_id, request.policy,
                    request.intent.action_class, request.intent.contract,
                    intent=request.intent,
                    expected=capability.authority)
                if capability.state != "active":
                    raise WebFailure("IDENTITY_POLICY_DENIED",
                        "Authenticated action capability was re-entered")
                capability.response_fingerprint = hashlib.sha256(encoded).hexdigest()
                capability.state = "consumed"
                return packet
            except IdentityFailure as error:
                if capability.state == "active":
                    capability.state = "failed"
                raise WebFailure(error.code, error.message) from None
            except BaseException:
                if capability.state == "active":
                    capability.state = "failed"
                raise

        return ProviderServices(denied, denied, denied,
            authenticated_action=authenticated_action)

    def _retain_action_evidence(self, packet):
        import base64
        retained = []
        for phase in ("before", "after"):
            snapshot = packet.get(phase)
            if type(snapshot) is not dict:
                continue
            html_bytes = snapshot["html"].encode()
            try:
                screenshot = base64.b64decode(
                    snapshot["screenshot"], validate=True)
            except (TypeError, ValueError):
                raise WebFailure("PROVIDER_DOWN",
                    "Action evidence screenshot is invalid") from None
            retained.extend((
                {"phase": phase, "kind": "html",
                 **self._save_bytes(html_bytes, ".html")},
                {"phase": phase, "kind": "screenshot",
                 **self._save_bytes(screenshot, ".png")},
            ))
        return retained

    def _retain_action_evidence_checked(self, packet):
        """Map private persistence failures into the typed action boundary."""
        try:
            return self._retain_action_evidence(packet)
        except WebFailure:
            raise
        except Exception:
            raise WebFailure("PROVIDER_DOWN",
                "Action evidence could not be retained") from None

    async def do(self, intent, policy: WebPolicy | None = None,
                 provider: str | None = None) -> dict:
        """Execute one typed action intent through Local Core authority."""
        from .actions import require_action
        from .providers import ProviderActionRequest
        from .web_do import (ActionJournal, JournalFailure, WebIntent,
                             require_action_contract)

        if type(intent) is dict:
            intent = WebIntent.from_dict(intent)
        if not isinstance(intent, WebIntent):
            raise ValueError("intent must be a WebIntent or exact intent object")
        policy = policy or WebPolicy()
        if provider is not None:
            policy = replace(policy, provider=provider)
        contract = require_action_contract(intent.contract)
        trace = uuid.uuid4().hex
        started = time.monotonic()
        result = {"url": _output_url(intent.url, named_identity=True),
            "outcome": {"state": "failed", "certainty": "certain",
                        "reconciliation_required": False},
            "receipt": {"trace_id": trace, "status": "failed",
                "operation": "do", "action_class": intent.action_class,
                "action_contract": intent.contract,
                "required_action_classes": list(
                    contract.required_action_classes),
                "semantic_result": contract.semantic_result,
                "identity": policy.identity, "authority_mode": None,
                "executor": None, "profile_version": None,
                "provider": None, "provider_version": None,
                "provider_binding_id": None, "rung": "R9",
                "requested_url": _output_url(intent.url, named_identity=True),
                "final_url": None, "observed_at": None,
                "latency_ms": None, "cost_usd": 0,
                "cost_basis": "self-hosted software; excludes electricity/hardware",
                "evidence": [], "attempts": [], "failure": None,
                "idempotency_key": intent.idempotency_key,
                "idempotent_reuse": False,
                "plan": intent.public_plan()}}
        receipt = result["receipt"]
        journal = None
        journal_active = False
        request_fingerprint = None
        effect_started = None
        packet = None
        try:
            for required_action_class in contract.required_action_classes:
                require_action(policy, required_action_class)
            if intent.contract not in policy.browser_do_allowed_contracts:
                raise WebFailure("POLICY_DENIED",
                    "Action operation contract is outside WebPolicy")
            try:
                contract.validate(intent)
            except PermissionError as error:
                raise WebFailure("POLICY_DENIED", str(error)) from None
            if contract.explicit_provider_required and policy.provider is None:
                raise WebFailure("POLICY_DENIED",
                    "Action operation contract requires an explicit provider")
            if (contract.requires_explicit_origins
                    and policy.browser_do_allowed_origins is None):
                raise WebFailure("POLICY_DENIED",
                    "Action operation contract requires explicit origins")
            if not policy.identity:
                raise WebFailure("IDENTITY_REQUIRED",
                    "web.do requires an explicit named identity")
            try:
                intent.validate_policy(policy)
            except PermissionError:
                raise WebFailure("POLICY_DENIED",
                    "Intent tool is outside WebPolicy") from None
            except ValueError:
                raise WebFailure("POLICY_DENIED",
                    "Intent exceeds WebPolicy") from None
            if contract.validation == "raw_control":
                used_tools = tuple(dict.fromkeys(
                    action.tool for action in intent.actions))
                if (len(policy.browser_do_allowed_tools) != len(used_tools)
                        or set(policy.browser_do_allowed_tools)
                            != set(used_tools)):
                    raise WebFailure("POLICY_DENIED",
                        "Raw browser control requires the exact action tools")
            _validate_url(intent.url)
            from .browser_use_config import origin as browser_origin
            origins = policy.browser_do_allowed_origins or (
                browser_origin(intent.url),)
            if (contract.requires_explicit_origins
                    and browser_origin(intent.url) not in origins):
                raise WebFailure("POLICY_DENIED",
                    "Action target is outside the explicit origin scope")
            for allowed_origin in origins:
                try:
                    contract.validate_origin(allowed_origin)
                except PermissionError as error:
                    raise WebFailure("POLICY_DENIED", str(error)) from None
            resolved = self.identities.resolve(
                policy.identity, intent.url, action=intent.action_class,
                allow_local_browser=policy.allow_local_browser)
            self._recheck_action_identity(
                resolved, intent.url, contract.required_action_classes,
                policy)
            receipt.update(authority_mode=resolved.authority_mode,
                executor=resolved.executor_id,
                profile_version=resolved.profile_version)
            for allowed_origin in origins:
                self._recheck_action_identity(
                    resolved, allowed_origin + "/",
                    contract.required_action_classes, policy)

            candidates = ([policy.provider] if policy.provider else
                list(policy.provider_candidates)
                    if policy.provider_candidates is not None else
                self.providers.action_candidates(
                    policy, intent.action_class, intent.contract))
            authority = None
            planning_failures = []
            for candidate in candidates:
                try:
                    authority = self._action_provider_authority(
                        candidate, policy, intent.action_class,
                        intent.contract, intent=intent)
                    break
                except WebFailure as error:
                    planning_failures.append({"provider": candidate,
                        "status": "failed", "failure": error.code,
                        "latency_ms": 0})
            receipt["attempts"].extend(planning_failures)
            if authority is None:
                raise WebFailure("PROVIDER_UNAVAILABLE",
                    "No installed action provider satisfies this intent")
            receipt.update(provider=authority.provider_id,
                provider_version=authority.version,
                provider_binding_id=authority.binding_id)
            request = ProviderActionRequest(intent, policy)
            capability = _AuthenticatedActionCapability(
                authority=authority, identity_scope=resolved.cache_scope,
                required_action_classes=contract.required_action_classes,
                request=request)
            policy_fingerprint = {
                "identity_scope": resolved.cache_scope,
                "action_class": intent.action_class,
                "required_action_classes": contract.required_action_classes,
                "action_contract": intent.contract,
                "provider_id": authority.provider_id,
                "provider_version": authority.version,
                "provider_binding_id": authority.binding_id,
                "origins": origins,
                "tools": policy.browser_do_allowed_tools,
            }
            request_fingerprint = intent.fingerprint(policy_fingerprint)
            journal = ActionJournal(
                self.state_dir, policy.browser_do_journal_max_bytes)

            with self.identities.lease(resolved):
                self._recheck_action_identity(
                    resolved, intent.url, contract.required_action_classes,
                    policy)
                context = await self._identity_step(
                    self._identity_context(resolved, policy), policy,
                    probe=True)
                health = await self._identity_step(self._identity_health(
                    context, resolved, policy,
                    allow_capture_strategy=False), policy, probe=True)
                receipt["identity_health"] = health
                if (policy.browser_do_require_auth_check
                        and health != "verified"):
                    raise WebFailure("IDENTITY_REAUTH_REQUIRED",
                        "Authenticated action requires a positive identity health check")
                decision = journal.begin(intent.idempotency_key,
                    request_fingerprint, trace)
                if not decision["execute"]:
                    replayed = copy.deepcopy(decision["result"])
                    if type(replayed) is not dict or "receipt" not in replayed:
                        raise WebFailure("EXECUTION_OUTCOME_UNKNOWN",
                            "Retained action outcome cannot be reconciled")
                    replayed["receipt"]["idempotent_reuse"] = True
                    return replayed
                journal_active = True
                attempt_started = time.monotonic()
                services = self._action_provider_services(
                    resolved, request, capability)
                packet = await self.providers.perform(
                    authority.provider_id, request, services)
                encoded = json.dumps(packet, sort_keys=True,
                    separators=(",", ":"), ensure_ascii=False,
                    allow_nan=False).encode()
                if (capability.state != "consumed"
                        or capability.response_fingerprint
                            != hashlib.sha256(encoded).hexdigest()):
                    raise WebFailure("IDENTITY_PROVIDER_DENIED",
                        "Action provider did not consume the exact Core capability")
                effect_started = packet["effect_started"]
                receipt["attempts"].append({
                    "provider": authority.provider_id,
                    "provider_version": authority.version,
                    "provider_binding_id": authority.binding_id,
                    "status": ("completed" if packet["status"] == "completed"
                               else "failed"),
                    "failure": packet.get("failure"),
                    "latency_ms": round(
                        (time.monotonic() - attempt_started) * 1000)})
                if packet["status"] != "completed":
                    # A failed action packet can still contain the exact
                    # completed-step prefix and snapshots needed to reconcile
                    # an external effect. Retain that bounded, provider-
                    # validated evidence before mapping a post-effect failure
                    # to EXECUTION_OUTCOME_UNKNOWN.
                    receipt["verification"] = {
                        "status": "partial_steps_observed",
                        "completed_steps": len(packet["steps"]),
                        "steps": copy.deepcopy(packet["steps"]),
                    }
                    receipt["evidence"] = (
                        self._retain_action_evidence_checked(packet))
                    raise WebFailure(packet["failure"],
                        "Authenticated browser action did not complete")
                self._recheck_action_identity(
                    resolved, intent.url, contract.required_action_classes,
                    policy)

            receipt["evidence"] = self._retain_action_evidence_checked(packet)
            final_url = _output_url(packet["url"], named_identity=True)
            result["url"] = final_url
            if contract.semantic_result == "unknown":
                verification = {
                    "status": "steps_observed",
                    "completed_steps": len(packet["steps"]),
                    "steps": copy.deepcopy(packet["steps"]),
                }
                certainty = "steps_observed"
                reversible = None
            else:
                last_mutation = max(index for index, action in
                    enumerate(intent.actions) if action.mutating)
                dom_assertions_passed = any(
                    index > last_mutation
                    and action.tool in {"assert_text", "assert_value"}
                    for index, action in enumerate(intent.actions))
                verification_status = (
                    "dom_assertions_passed" if dom_assertions_passed
                    else "execution_observed")
                verification = {
                    "status": verification_status,
                    "completed_steps": len(packet["steps"]),
                }
                certainty = verification_status
                reversible = intent.action_class == "WRITE_REVERSIBLE"
            receipt.update(status="completed", final_url=final_url,
                observed_at=utcnow(),
                latency_ms=round((time.monotonic() - started) * 1000),
                failure=None,
                verification=verification)
            result["outcome"] = {"state": "completed",
                "certainty": certainty,
                "reversible": reversible,
                "reconciliation_required": False}
            journal.finish(intent.idempotency_key, request_fingerprint,
                trace, "completed", result=result)
            journal_active = False
        except (WebFailure, IdentityFailure, JournalFailure) as error:
            code = error.code
            if journal_active:
                state = ("failed_before_effect" if effect_started is False
                         else "uncertain")
                try:
                    journal.finish(intent.idempotency_key,
                        request_fingerprint, trace, state)
                    journal_active = False
                except JournalFailure:
                    state = "uncertain"
                if state == "uncertain":
                    receipt["underlying_failure"] = code
                    code = "EXECUTION_OUTCOME_UNKNOWN"
                    result["outcome"] = {"state": "unknown",
                        "certainty": "unknown",
                        "reconciliation_required": True}
            receipt.update(status="failed",
                observed_at=utcnow(),
                latency_ms=round((time.monotonic() - started) * 1000),
                failure={"code": code, "message": (
                    "Action outcome requires reconciliation before replay"
                    if code == "EXECUTION_OUTCOME_UNKNOWN" else error.message)})
        self._save_trace(result, record_observations=False)
        return result

    def _provider_services(self, *, identity_binding=None):
        from .providers import ProviderServices
        services = None
        if identity_binding is not None:
            resolved, identity_context, capability = identity_binding

            async def public_denied(*args, **kwargs):
                raise WebFailure("IDENTITY_POLICY_DENIED",
                    "Named identity providers cannot use public transport services")

            async def authenticated(url, policy):
                # The callback is an opaque, one-shot operation capability.
                # Plugins never receive the browser context, profile material,
                # identity registry, or Core-owned capture attestation.
                if capability.state != "unused":
                    capability.state = "violated"
                    raise WebFailure("IDENTITY_POLICY_DENIED",
                        "Authenticated provider capability is one-shot")
                capability.state = "active"
                if (url != capability.url or policy is not capability.policy
                        or resolved.cache_scope
                            != capability.identity_scope):
                    capability.state = "violated"
                    raise WebFailure("IDENTITY_POLICY_DENIED",
                        "Authenticated provider request exceeds its operation scope")
                try:
                    self._identity_provider_authority(
                        resolved, policy, expected=capability.authority)
                    self.identities.recheck(resolved, url)
                    acquired = await self._identity_step(
                        self._get_identity_browser(
                            url, policy, resolved, identity_context,
                            **({"capture_strategy": capability.capture_strategy}
                               if capability.capture_strategy is not None
                               else {})), policy)
                    acquired, capture, private_sources = (
                        _core_identity_capture(
                        acquired, policy.max_bytes,
                        capture_strategy=capability.capture_strategy)
                    )
                    for private_url in private_sources.values():
                        if not self._identity_allows(
                                resolved, private_url, image=True):
                            raise WebFailure("IDENTITY_DOMAIN_DENIED",
                                "Identity image source is outside its enrolled scope")
                    fingerprint = _identity_acquisition_fingerprint(
                        acquired, policy.max_bytes,
                        capture_metadata=capture)
                    self.identities.recheck(resolved, url)
                    self._identity_provider_authority(
                        resolved, policy, expected=capability.authority)
                    if capability.state != "active":
                        raise WebFailure("IDENTITY_POLICY_DENIED",
                            "Authenticated provider capability was re-entered")
                    capability.response_fingerprint = fingerprint
                    capability.capture_metadata = capture
                    capability.private_image_sources = private_sources
                    capability.state = "consumed"
                    return acquired
                except IdentityFailure as exc:
                    if capability.state == "active":
                        capability.state = "failed"
                    raise WebFailure(exc.code, exc.message) from None
                except BaseException:
                    if capability.state == "active":
                        capability.state = "failed"
                    raise

            http_service = browser_service = isolated_service = public_denied
        else:
            authenticated = None
            http_service = self._get_http

            async def browser_service(url,policy,provider):
                if provider == "steel":
                    async with self._steel_lock:return await self._get_browser(url,policy,provider)
                return await self._get_browser(url,policy,provider)

            async def isolated_service(url,policy,provider):
                from .experimental import read_public
                return await read_public(self,url,policy,provider)
        async def acquire_provider(identifier, request):
            return await self.providers.acquire(
                identifier, request, services,
                _evidence_preparer=self._prepare_provider_failure_evidence)
        def provider_manifest(identifier, policy):
            return self.providers.require_enabled(identifier, policy)
        services = ProviderServices(http_service,browser_service,
                                    isolated_service,acquire_provider,
                                    provider_manifest,authenticated)
        return services

    # Polite pacing applies to every Runtime. The test suite turns it off in
    # tests/conftest.py and enables it explicitly where pacing is under test.
    pacing_enabled = True
    # Structural completeness escalation (WebPolicy.completeness_*). Unit tests
    # with fake pages turn it off; tests/test_completeness.py turns it on.
    completeness_enabled = True

    def _pacing_path(self):
        return self.state_dir / "origin-pacing.json"

    def _pacing_load(self):
        try:
            data = json.loads(self._pacing_path().read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _pacing_save(self, data):
        now = time.time()
        data = {key: entry for key, entry in data.items()
                if isinstance(entry, dict) and max(entry.get("next_at", 0), entry.get("cooldown_until", 0)) > now}
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".origin-pacing-", dir=self.state_dir)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, sort_keys=True)
            os.replace(temporary, self._pacing_path())
        except OSError:
            pass

    def _hints_path(self):
        return self.state_dir / "origin-routes.json"

    def _hints_load(self):
        try:
            data = json.loads(self._hints_path().read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _hints_save(self, data):
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".origin-routes-", dir=self.state_dir)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, sort_keys=True)
            os.replace(temporary, self._hints_path())
        except OSError:
            pass

    def _origin_hint(self, url, policy):
        """The provider that last got through this public origin, if still fresh."""
        if policy.identity or not policy.origin_route_hint_ttl_seconds:
            return None
        try:
            key = self._pacing_key(url, policy)
        except (ValueError, TypeError):
            return None
        entry = self._hints_load().get(key)
        if (isinstance(entry, dict) and isinstance(entry.get("provider"), str)
                and time.time() - entry.get("at", 0) <= policy.origin_route_hint_ttl_seconds):
            return entry["provider"]
        return None

    def _record_origin_hint(self, url, policy, receipt):
        if (policy.identity or not policy.origin_route_hint_ttl_seconds
                or policy.provider or policy.provider_candidates is not None):
            return
        attempts = [item for item in receipt.get("attempts") or [] if isinstance(item, dict)]
        if not attempts:
            return
        try:
            key = self._pacing_key(url, policy)
        except (ValueError, TypeError):
            return
        if receipt.get("archived"):
            return
        data = self._hints_load()
        last = attempts[-1]
        if receipt.get("status") == "observed" and last.get("status") == "observed":
            if (any(item.get("status") == "failed" for item in attempts[:-1])
                    or (receipt.get("hedge") or {}).get("won")):
                data[key] = {"provider": last.get("provider"), "at": time.time()}
            elif data.get(key, {}).get("provider") != last.get("provider"):
                return
        elif data.get(key, {}).get("provider") in {item.get("provider") for item in attempts}:
            data.pop(key, None)
        else:
            return
        self._hints_save(data)

    @staticmethod
    def _pacing_key(url, policy):
        parsed = urlparse(url)
        return parsed.scheme + "://" + (parsed.hostname or "") + "|" + (policy.identity or "public")

    async def read(self, url: str, policy: WebPolicy | None = None, provider: str | None = None,
                   adapter: str | None = None, *, policy_overrides: dict | None = None,
                   workload_assertions: dict | None = None, retry_of: str | None = None,
                   module: str | bool | None = None, module_override: dict | None = None) -> dict:
        """Read a page. ``retry_of`` is the agent's "try harder": pass the trace_id
        of a read whose page was not what you needed, and this read skips every
        tool that one tried and starts from the strongest remaining tool.

        Site modules (see site_modules.py): by default a saved, enabled module
        matching the URL shapes the read. ``module`` names one (its origin must
        match), ``module=False`` turns modules off, and ``module_override`` runs
        an unsaved module for this read only. With a module, the result gains
        ``items`` and the receipt gains ``module``; raw page data is unchanged.
        """
        from .site_modules import ACTIVE as ACTIVE_MODULE, SiteModule, SiteModuleError
        if policy is None and (self.lightning_default or self.real_browser_default):
            # The owner's switches for every read (FRANKENSURF_LIGHTNING,
            # FRANKENSURF_REAL_BROWSER) are defaults: anything the call sets wins.
            policy_overrides = {**({"lightning": True} if self.lightning_default else {}),
                                **({"allow_real_browser": True} if self.real_browser_default else {}),
                                **(policy_overrides or {})}
        selected, source = None, None
        try:
            if module_override is not None:
                selected, source = SiteModule.from_record(module_override), "override"
            elif isinstance(module, str):
                selected, source = self.site_modules.get(module), "named"
            elif module is None and isinstance(url, str):
                selected, source = self.site_modules.match(url), "matched"
            elif module is not False:
                raise SiteModuleError("module must be a module ID, None or False")
            if selected is not None and not selected.same_origin(url):
                raise SiteModuleError(f"Site module {selected.id} is for {selected.match['origin']}")
        except SiteModuleError as error:
            if source == "matched":
                selected = None  # A broken store never stops ordinary reads.
            else:
                from .routes import request_policy
                effective, _ = request_policy(policy, policy_overrides)
                return await self._read(url, effective, adapter=adapter,
                                        _planning_failure=WebFailure("POLICY_DENIED", str(error)))
        if selected is None:
            result = await self._read_entry(url, policy, provider, adapter, policy_overrides,
                                            workload_assertions, retry_of)
            result = await self._rescue(url, result, policy, provider, adapter, policy_overrides)
            self._auto_items(url, result, policy, policy_overrides)
            return result
        if policy is None:
            # The module's operational defaults; anything the caller set wins.
            policy_overrides = {**selected.policy_overrides(), **(policy_overrides or {})}
        token = ACTIVE_MODULE.set(selected)
        try:
            result = await self._read_entry(url, policy, provider, adapter, policy_overrides,
                                            workload_assertions, retry_of)
        finally:
            ACTIVE_MODULE.reset(token)
        self._apply_module(selected, source, url, result, policy, policy_overrides)
        return result

    async def _rescue(self, url, result, policy, provider, adapter, policy_overrides):
        """The page the URL meant, when the URL is wrong: after NOT_FOUND, search the
        site itself for the words the URL asked for. Web conventions only (the
        site's own search from its home page); the results stand only when they
        are complete and mention those words."""
        receipt = result.get("receipt") or {}
        # Not found, or a search page whose results ignore what it asked for (the
        # site took no notice of the URL's query): either way the URL is wrong.
        after = ("NOT_FOUND" if (receipt.get("failure") or {}).get("code") == "NOT_FOUND"
                 else "off_query" if (receipt.get("status") == "observed"
                                      and "off_query" in ((receipt.get("quality") or {}).get("flags") or ()))
                 else None)
        if after is None or not isinstance(url, str) or provider or adapter:
            return result
        from .routes import request_policy
        try:
            effective, _ = request_policy(policy, policy_overrides)
        except ValueError:
            return result
        if (not effective.rescue_not_found or effective.provider or effective.provider_candidates is not None
                or effective.identity or effective.profile):
            return result
        from .completeness import _same_site, assess, url_words
        from .module_discovery import find_search
        words = url_words(url)
        parsed = urlparse(url)
        home = f"{parsed.scheme}://{parsed.netloc}/"
        if not words or url.split("#")[0].rstrip("/") == home.rstrip("/"):
            return result
        inner = {**(policy_overrides or {}), "rescue_not_found": False} if policy is None else None
        inner_policy = replace(policy, rescue_not_found=False) if policy is not None else None
        record = {"query": words, "found": False}
        # Every read the rescue makes stays on the bill, found or not.
        costs = [receipt.get("cost_usd", 0)]

        def give_up():
            receipt["cost_usd"] = _total_cost(costs)
            receipt["rescue"] = record
            return result
        home_result = await self.read(home, inner_policy, module=False, policy_overrides=inner)
        home_receipt = home_result.get("receipt") or {}
        costs.append(home_receipt.get("cost_usd", 0))
        record["home_trace_id"] = home_receipt.get("trace_id")
        if home_receipt.get("status") != "observed":
            return give_up()
        search = find_search(home_result, home)
        if search is not None and search.get("template"):
            from urllib.parse import quote, quote_plus
            encode = quote_plus if search["encoding"] == "query" else (lambda text: quote(text, safe=""))
            target, via = search["template"].replace("{query}", encode(words)), search["from"]
        else:
            target, via = await self._probe_search(home, words, effective), "search box"
        # The site's own search only: its host, a subdomain or its parent, never another site.
        target_host = urlparse(target or "").hostname
        landed = (receipt.get("final_url") or url).split("#")[0]
        if (not target or target.split("#")[0] in (url.split("#")[0], landed)
                or not (_same_site(target_host, parsed.hostname) or _same_site(parsed.hostname, target_host))):
            return give_up()
        found = await self.read(target, inner_policy, module=False, policy_overrides=inner)
        found_receipt = found.get("receipt") or {}
        costs.append(found_receipt.get("cost_usd", 0))
        verdict = assess(target, found, expect_terms=tuple(words.split()))
        record.update(via=via, search_url=target, trace_id=found_receipt.get("trace_id"))
        if found_receipt.get("status") != "observed" or not verdict.get("complete") or verdict.get("off_query"):
            return give_up()
        found_receipt["cost_usd"] = _total_cost(costs)
        found_receipt["rescued"] = {"from": url, "after": after, "query": words, "via": via,
                                    "search_url": target, "home_trace_id": record["home_trace_id"],
                                    "trace_id": receipt.get("trace_id")}
        found_receipt["next_step"] = {
            "reason": "rescued",
            "how": (("The URL asked for answered not found" if after == "NOT_FOUND"
                     else "The URL asked for showed results that ignored its search")
                    + f", so these are the site's own search results for '{words}'. "
                    "Pick the page you meant from them, or read it directly.")}
        self._annotate_trace(found_receipt, "rescued")
        if adapter is None:
            self._grade(target, found, effective)
        return found

    def _apply_module(self, selected, source, url, result, policy, policy_overrides):
        from .completeness import query_terms
        from .repair import evaluate_workload_assertions
        from .site_modules import receipt_record
        receipt = result.get("receipt") or {}
        if receipt.get("status") != "observed":
            invalid = "site module" in str((receipt.get("failure") or {}).get("message") or "")
            receipt["module"] = receipt_record(selected, source, None, None, invalid=invalid)
            self._annotate_trace(receipt, "module")
            return
        from .routes import request_policy
        effective, _ = request_policy(policy, policy_overrides)
        output = selected.extract(result, url, query_terms=query_terms(url, effective.expect_terms))
        validation = (evaluate_workload_assertions(output, selected.assertions)
                      if selected.assertions is not None else None)
        result["items"] = output["items"]
        if output.get("next_url"):
            result["next_url"] = output["next_url"]
        receipt["module"] = receipt_record(selected, source, output, validation)
        from .module_discovery import discover, verify_items
        check = verify_items(output["items"], result, url) if selected.items else None
        if check is not None:
            receipt["module"]["items_quality"] = check
            if check["grade"] == "poor":
                # The site changed under the module: draft a replacement from this
                # same page (no extra read). The owner accepts it with site_modules put.
                found = discover(result, url, module_id=selected.id, limit=1)
                if found["drafts"] and found["drafts"][0]["items_quality"]["grade"] != "poor":
                    repair = dict(found["drafts"][0]["module"])
                    repair["version"] = f"{selected.version}.repair"
                    for key in ("templates", "pagination", "invalid", "assertions"):
                        if getattr(selected, key):
                            repair[key] = selected.record()[key]
                    receipt["module"]["repair_draft"] = {
                        "module": repair, "items_quality": found["drafts"][0]["items_quality"],
                        "how": "site_modules put this module to accept it; nothing was saved"}
        self._annotate_trace(receipt, "module")

    def _auto_items(self, url, result, policy, policy_overrides):
        """With auto_items and no saved module: items from the page's own data,
        and the drafted module that found them (result.auto_module) to save."""
        from .routes import request_policy
        try:
            effective, _ = request_policy(policy, policy_overrides)
        except Exception:
            return
        receipt = result.get("receipt") or {}
        if not effective.auto_items or receipt.get("status") != "observed" or not isinstance(url, str):
            return
        from .module_discovery import discover
        from .site_modules import SiteModule
        found = discover(result, url, limit=1)
        if not found["drafts"]:
            receipt["auto_items"] = {"found": False}
            return
        record = found["drafts"][0]["module"]
        output = SiteModule.from_record(record).extract(result, url)
        result["items"] = output["items"]
        result["auto_module"] = record
        receipt["auto_items"] = {"found": True, "count": output["count"],
                                 "quality": found["drafts"][0].get("items_quality"),
                                 "source": record["sources"]["listing"]["kind"],
                                 "save": "site_modules put with result.auto_module to reuse it"}

    def _module_satisfied(self, result, url, terms):
        """True when the active module's assertions pass on this page."""
        from .site_modules import ACTIVE as ACTIVE_MODULE
        selected = ACTIVE_MODULE.get()
        if selected is None or selected.assertions is None or not selected.items:
            return False
        from .completeness import query_terms
        from .repair import evaluate_workload_assertions
        output = selected.extract(result, url, query_terms=query_terms(url, terms))
        return evaluate_workload_assertions(output, selected.assertions)["status"] == "passed"

    async def read_template(self, module_id: str, template: str, params: dict | None = None,
                            **kwargs) -> dict:
        """Read a URL built from a saved module's template, shaped by that module."""
        built = self.site_modules.get(module_id).build_url(template, params)
        return await self.read(built, module=module_id, **kwargs)

    async def batch_template(self, module_id: str, template: str, params_list: list[dict],
                             policy: WebPolicy | None = None, **kwargs) -> list[dict]:
        """One saved template, many parameter sets (for example many searches),
        read as one paced batch and shaped by the module. Results keep the
        order of params_list; each carries the params it was built from."""
        module = self.site_modules.get(module_id)
        urls = [module.build_url(template, params) for params in params_list]
        results = await self.batch(urls, policy, module=module_id, **kwargs)
        for params, result in zip(params_list, results):
            result["params"] = dict(params or {})
        return results

    async def discover_module(self, url: str, *, module_id: str | None = None, save: bool = False,
                              query: str | None = None, policy_overrides: dict | None = None) -> dict:
        """Draft a site module from the page's own JSON (JSON-LD, script JSON,
        or the JSON it fetched while rendering). Reads the page once; when that
        page carries no feed, reads it again in a browser that records the JSON
        responses. save=True saves the best draft.

        With query and a page that is not itself a search (a home page), the
        site's search is found first (schema.org SearchAction, else its search
        form), the query is searched, and the module is drafted from that."""
        from .module_discovery import discover, find_search, search_template
        overrides = dict(policy_overrides or {})
        result = await self.read(url, module=False, policy_overrides=overrides)
        reads = [(result.get("receipt") or {}).get("trace_id")]
        search = None
        if query and not search_template(url) and (result.get("receipt") or {}).get("status") == "observed":
            search = find_search(result, url)
            if search is None:
                # A search box run by script: type the query once and see where it lands.
                from .routes import request_policy
                effective, _ = request_policy(None, overrides)
                landed = await self._probe_search(url, query, effective)
                landed_template = search_template(landed) if landed else None
                if landed_template:
                    search = {"template": landed_template[0], "encoding": landed_template[1],
                              "path_pattern": landed_template[2], "from": "search box"}
                elif landed:
                    search = {"template": None, "encoding": None, "path_pattern": None,
                              "from": "search box", "landed": landed}
            if search is None:
                return {"url": url, "drafts": [], "found": False, "reads": [trace for trace in reads if trace],
                        "hint": "No search found on this page (no SearchAction, no search form, no search"
                                " box that navigates); pass a search results URL instead."}
            from urllib.parse import quote, quote_plus
            encode = quote_plus if search["encoding"] == "query" else (lambda text: quote(text, safe=""))
            url = (search["template"].replace("{query}", encode(query)) if search["template"]
                   else search["landed"])
            result = await self.read(url, module=False, policy_overrides=overrides)
            reads.append((result.get("receipt") or {}).get("trace_id"))
        template = ((search["template"], search["encoding"], search["path_pattern"])
                    if search and search["template"] else None)
        found = discover(result, url, module_id=module_id, search=template)
        if not found["drafts"]:
            rendered = await self.read(url, module=False, policy_overrides={
                **overrides, "capture_json_responses": True,
                "provider_candidates": overrides.get("provider_candidates") or ["local", "scrapling"]})
            reads.append((rendered.get("receipt") or {}).get("trace_id"))
            if (rendered.get("receipt") or {}).get("status") == "observed":
                found = discover(rendered, url, module_id=module_id, search=template)
        found["reads"] = [trace for trace in reads if trace]
        if search:
            found["search"] = search
        if save and found["drafts"]:
            found["saved"] = self.site_modules.put(found["drafts"][0]["module"])
        return found

    async def _probe_search(self, url, query, policy):
        """Type query into the page's visible search box and press Enter, in an
        anonymous local browser; the URL it lands on, or None. One search and
        nothing else: no other field is touched and nothing is submitted but
        the search. Boxes are found by web conventions (type=search, role,
        label), not by site."""
        if not policy.allow_local_browser:
            return None
        _validate_url(url)
        try:
            browser = await self._browser("local", policy)
            context = await browser.new_context()
        except Exception:
            return None
        try:
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=policy.timeout_seconds * 1000)
            await page.wait_for_timeout(1500)
            # A consent banner over the search box takes the click: reject it first.
            from .interactions import dismiss_consent
            await dismiss_consent(page, [])
            box = None
            for selector in ('input[type="search"]', '[role="searchbox"]', '[role="search"] input[type="text"]',
                             'input[name="q"]', 'input[aria-label*="search" i]',
                             'input[placeholder*="search" i]', 'input[type="text"][name*="search" i]'):
                found = page.locator(selector)
                for index in range(min(await found.count(), 6)):
                    if await found.nth(index).is_visible():
                        box = found.nth(index)
                        break
                if box is not None:
                    break
            if box is None:
                return None
            try:
                # Some boxes open on click; an overlay (a consent banner) may take the
                # click, and nothing else is clicked to clear it.
                await box.click(timeout=3000)
            except Exception:
                pass
            await box.fill(query, timeout=5000)
            try:
                async with page.expect_navigation(timeout=15000):
                    await box.press("Enter")
            except Exception:
                pass
            await page.wait_for_timeout(1000)
            landed = page.url.split("#")[0]
            _validate_url(landed)
            same = urlparse(landed).netloc.split(":")[0].endswith(
                ".".join((urlparse(url).hostname or "").split(".")[-2:]))
            return landed if same and landed != url.split("#")[0] else None
        except Exception:
            return None
        finally:
            await context.close()

    async def _read_entry(self, url, policy, provider, adapter, policy_overrides,
                          workload_assertions, retry_of):
        from .routes import request_policy
        if retry_of is not None:
            policy, policy_overrides = self._retry_policy(retry_of, policy, policy_overrides)
        effective, _ = request_policy(policy, policy_overrides)
        if effective.exclude_providers and not (provider or effective.provider or effective.identity):
            remaining = [i for i in self.providers.candidates(
                             effective, configured=("steel",) if self.steel_api_url else (),
                             operation="extract" if adapter else "read")
                         if i not in effective.exclude_providers]
            remaining += [i for i in effective.completeness_ladder
                          if i not in effective.exclude_providers and self.providers.is_available(i)
                          and _allowed(self.providers, i, effective)]
            # Route-scoped tools the call allows still count.
            remaining += [i for i, allowed in (("real_browser", effective.allow_real_browser
                                                 and effective.allow_local_browser),
                                                ("handoff", effective.allow_handoff))
                          if allowed and i not in effective.exclude_providers and self.providers.is_available(i)]
            if not remaining:
                return await self._read(url, effective, adapter=adapter, _planning_failure=WebFailure(
                    "PROVIDER_UNAVAILABLE", "Every allowed tool was already tried; allow paid tools,"
                    " your real browser (allow_real_browser=True), use a profile, or ask for handoff"
                    " (allow_handoff=True)"))
        if effective.profile:
            return await self._read_with_profile(url, policy, provider, adapter, policy_overrides,
                                                 workload_assertions, effective)
        return await self._read_paced(url, policy, provider, adapter, policy_overrides,
                                      workload_assertions, effective)

    def _offer_try_harder(self, result, policy):
        """Tell the agent how to ask for more when an observed page isn't right."""
        receipt = result.get("receipt") or {}
        if receipt.get("status") != "observed" or policy.identity or not receipt.get("trace_id"):
            return
        tried = {attempt.get("provider") for attempt in receipt.get("attempts") or ()}
        tried |= {step.get("provider") for step in (receipt.get("completeness") or {}).get("escalations") or ()}
        tried |= set((receipt.get("hedge") or {}).get("tried") or ()) | set((receipt.get("hedge") or {}).get("walled") or ())
        tried |= set(policy.exclude_providers)
        untried = [i for i in policy.completeness_ladder
                   if i not in tried and self.providers.is_available(i) and _allowed(self.providers, i, policy)]
        receipt["if_not_right"] = {
            "retry_of": receipt["trace_id"], "untried": untried,
            "how": ("If this page isn't what you needed, read it again with retry_of=<this trace_id>"
                    " (MCP: try_harder_than): FrankenSurf skips every tool tried here and starts"
                    " from the strongest remaining one." if untried else
                    "Every strong tool allowed here was tried. Allow paid tools, use a profile,"
                    " or ask for handoff (allow_handoff=True).")}

    def _tried_providers(self, trace_id: str) -> list[str]:
        """Every provider a recorded read used: attempts, escalations, second opinion."""
        try:
            receipt = self.trace(trace_id)["receipt"]
        except (OSError, ValueError, KeyError):
            raise ValueError("Unknown trace_id for retry_of") from None
        tried = [attempt.get("provider") for attempt in receipt.get("attempts") or ()]
        tried += [step.get("provider") for step in (receipt.get("completeness") or {}).get("escalations") or ()]
        tried += list((receipt.get("hedge") or {}).get("tried") or ()) + list((receipt.get("hedge") or {}).get("walled") or ())
        tried.append((receipt.get("second_opinion") or {}).get("other"))
        # A retry of a retry also skips what the earlier rounds skipped.
        tried += (receipt.get("try_harder") or {}).get("excluded") or []
        return list(dict.fromkeys(item for item in tried if isinstance(item, str)))

    def _retry_policy(self, trace_id, policy, policy_overrides):
        """A policy that skips what the earlier read tried and climbs strongest-first."""
        tried = self._tried_providers(trace_id)
        if policy is not None:
            excluded = tuple(dict.fromkeys((*policy.exclude_providers, *tried)))
            return replace(policy, exclude_providers=excluded, try_harder=True, freshness="now"), None
        overrides = dict(policy_overrides or {})
        excluded = list(dict.fromkeys([*(overrides.get("exclude_providers") or []), *tried]))
        overrides.update(exclude_providers=excluded, try_harder=True, freshness="now",
                         retry_of_trace=trace_id)
        return None, overrides

    async def _read_with_profile(self, url, policy, provider, adapter, policy_overrides,
                                 workload_assertions, effective):
        """Run one read carrying a stored profile, then save its refreshed session."""
        from .profiles import ACTIVE, ProfileError, ProfileStore
        store = ProfileStore()
        try:
            profile = store.load(effective.profile)
        except ProfileError as error:
            return await self._read(url, effective, adapter=adapter,
                                    _planning_failure=WebFailure("POLICY_DENIED", str(error)))
        if not isinstance(url, str) or not profile.covers(url):
            return await self._read(url, effective, adapter=adapter,
                _planning_failure=WebFailure("POLICY_DENIED", "Profile does not cover this site"))
        explicit = provider or effective.provider
        if explicit and explicit not in profile.carriers():
            return await self._read(url, effective, adapter=adapter,
                _planning_failure=WebFailure("POLICY_DENIED",
                    "Provider cannot carry this profile at its sharing level"))
        profile.changed = False
        token = ACTIVE.set(profile)
        try:
            result = await self._read_paced(url, policy, provider, adapter, policy_overrides,
                                            workload_assertions, effective)
        finally:
            ACTIVE.reset(token)
        receipt = result.get("receipt") or {}
        receipt["profile"] = {"name": profile.name, "version": profile.version,
                              "sharing": profile.sharing, "carried_by": receipt.get("method")}
        if receipt.get("status") == "observed" and getattr(profile, "changed", False):
            profile.version += 1
            store.save(profile)
            receipt["profile"]["version"] = profile.version
        if (receipt.get("failure") or {}).get("code") in ("AUTH_REQUIRED", "AUTH_EXPIRED"):
            receipt["next_step"] = {"profile_login": profile.name, "reason": receipt["failure"]["code"],
                "how": f"sign in again with: frankensurf profile-login {profile.name} --site "
                       + profile.sites[0]}
        return result

    async def _read_paced(self, url, policy, provider, adapter, policy_overrides,
                          workload_assertions, effective):
        key = self._pacing_key(url, effective) if self.pacing_enabled and isinstance(url, str) else None
        cooling = None  # The cool-down failure, when only the site's known connection may try.
        if key is not None and (effective.origin_min_interval_seconds or effective.origin_cooldown_seconds):
            entry = self._pacing_load().get(key) or {}
            now = time.time()
            from .hosted_providers import UNBLOCKERS
            hinted = self._origin_hint(url, effective)
            # Unblockers reach the site through their own network, and a
            # handoff is a person clearing the wall, so a cool-down holds neither.
            unblocker = ((provider or effective.provider) in UNBLOCKERS | {"handoff", "real_browser"}
                         or (hinted in UNBLOCKERS and effective.allow_paid_fallbacks
                             and not effective.provider
                             and self.providers.is_available(hinted)))
            if (entry.get("cooldown_until", 0) > now and not unblocker and not (provider or effective.provider)
                    and entry.get("code") in ("BLOCKED", "CAPTCHA")
                    and effective.allow_real_browser and effective.allow_local_browser
                    and "real_browser" not in effective.exclude_providers
                    and effective.provider_candidates is None and not effective.identity and not effective.profile
                    and self.providers.is_available("real_browser")):
                # The site walled the other tools minutes ago; the owner's real
                # browser, which walls rarely tell from a person, still gets a try.
                provider, unblocker = "real_browser", True
            if entry.get("cooldown_until", 0) > now and not unblocker:
                code = entry.get("code") if entry.get("code") in effective.origin_cooldown_failures else "RATE_LIMITED"
                failure = WebFailure(code, "Origin is cooling down after %s; retry after %d seconds"
                                     % (code, int(entry["cooldown_until"] - now) + 1))
                if not (self._egress_usable(url, provider, effective) and self._egress_hint(url, effective)):
                    return await self._read(url, replace(effective, provider=provider) if provider else effective,
                                            adapter=adapter, _planning_failure=failure)
                # The pause protects this address; the connection that got through
                # last time is another one, so it alone still gets a try.
                cooling = failure
            delay = entry.get("next_at", 0) - now
            if delay > 0:
                await asyncio.sleep(min(delay, effective.origin_min_interval_seconds))
        egress_state = {}
        result = await self._egress_first(url, policy, provider, adapter, policy_overrides,
                                          workload_assertions, effective, egress_state)
        if result is None and cooling is not None:
            return await self._read(url, replace(effective, provider=provider) if provider else effective,
                                    adapter=adapter, _planning_failure=cooling)
        if result is None:
            result = await self._hedged(url, policy, provider, adapter, policy_overrides,
                                        workload_assertions, effective)
        result = await self._warm_up_retry(url, result, policy, provider, adapter, policy_overrides,
                                           workload_assertions, effective)
        if not effective.identity and not effective.profile:
            # Learned from what this machine's own address met, before another connection.
            try:
                self._learn_wall(result.get("receipt") or {})
            except OSError:
                pass
        result = await self._egress_retry(url, result, policy, provider, adapter, policy_overrides,
                                          workload_assertions, effective, egress_state)
        egress = self._egress_named((result.get("receipt") or {}).get("egress"))
        if egress is None and _wants_second_opinion(result, effective, provider):
            result = await self._second_opinion(url, result, policy, adapter, policy_overrides,
                                                workload_assertions, effective)
        if (self.completeness_enabled and effective.completeness_escalation
                and _automatic_observed(result, effective, provider)):
            if egress is None:
                result = await self._ensure_complete(url, result, policy, adapter, policy_overrides,
                                                     workload_assertions, effective)
            else:
                # The page came through another connection: so does any climb from it,
                # with the tools that can carry that connection.
                from .egress import CURRENT as EGRESS
                scoped_policy, scoped_overrides, scoped = self._egress_scoped(policy, policy_overrides, effective)
                token = EGRESS.set(egress)
                try:
                    result = await self._ensure_complete(url, result, scoped_policy, adapter, scoped_overrides,
                                                         workload_assertions, scoped)
                finally:
                    EGRESS.reset(token)
        self._offer_try_harder(result, effective)
        if adapter is None:
            self._grade(url, result, effective)
        if effective.main_content and (result.get("receipt") or {}).get("status") == "observed":
            from .main_content import main_content
            found = main_content(result.get("content") or "", result.get("content_type") or "",
                                 result.get("url") or url)
            if found:
                result["main_text"] = found["text"]
                result["receipt"]["main_content"] = {
                    "method": found["method"], "chars": found["chars"],
                    "of_chars": len(result.get("text") or ""),
                    **({"cookie_notice": True} if found.get("cookie_notice") else {})}
        if isinstance(url, str) and not effective.profile:
            self._record_origin_hint(url, effective, result.get("receipt") or {})
            _suggest_handoff(result.get("receipt") or {})
        if key is not None and (effective.origin_min_interval_seconds or effective.origin_cooldown_seconds):
            data = self._pacing_load()
            entry = dict(data.get(key) or {})
            entry["next_at"] = time.time() + effective.origin_min_interval_seconds
            code = ((result.get("receipt") or {}).get("failure") or {}).get("code")
            if code in effective.origin_cooldown_failures and effective.origin_cooldown_seconds:
                entry["cooldown_until"] = time.time() + effective.origin_cooldown_seconds
                entry["code"] = code
            data[key] = entry
            self._pacing_save(data)
        return result

    def _egress_client(self, egress):
        """A plain-HTTP client through one configured connection (kept for the Runtime)."""
        client = self._egress_http.get(egress["name"])
        if client is None:
            from .bot_auth import sign_hop
            try:
                client = httpx.AsyncClient(follow_redirects=True, proxy=egress["url"],
                                           headers={"User-Agent": _user_agent()},
                                           event_hooks={"request": [sign_hop, _guard_hop]})
            except ImportError:
                raise WebFailure("PROVIDER_UNAVAILABLE",
                                 "A SOCKS connection needs httpx's socks extra: pip install 'frankensurf[socks]'") from None
            self._egress_http[egress["name"]] = client
        return client

    def _egress_usable(self, url, provider, effective):
        return bool(self.egress_pool and effective.use_egress_pool and isinstance(url, str)
                    and not (provider or effective.provider or effective.provider_candidates is not None
                             or effective.identity or effective.profile))

    def _egress_named(self, record):
        """The configured connection a receipt's egress record names, or None."""
        name = (record or {}).get("used") if isinstance(record, dict) else None
        return next((egress for egress in self.egress_pool if egress["name"] == name), None)

    def _egress_scoped(self, policy, policy_overrides, effective):
        """The read's policy without tools that would ignore the connection."""
        direct = tuple(sorted(_PROXIED - _EGRESS_CAPABLE))
        excluded = tuple(dict.fromkeys(tuple(effective.exclude_providers) + direct))
        scoped = replace(effective, exclude_providers=excluded)
        if policy is not None:
            return replace(policy, exclude_providers=excluded), None, scoped
        return None, {**(policy_overrides or {}), "exclude_providers": list(excluded)}, scoped

    def _egress_tools(self, vendor, effective, egress):
        """The tools a pass through `egress` uses: egress_tools that can carry a
        connection, the ones that got past this wall vendor more often than not first."""
        tools = [tool for tool in effective.egress_tools
                 if tool in _EGRESS_CAPABLE and tool not in effective.exclude_providers
                 and not (tool == "http" and self._transport is not None)
                 # nodriver's browser flags cannot carry a username and password.
                 and not (tool == "nodriver" and egress.get("username"))
                 and self.providers.is_available(tool) and _allowed(self.providers, tool, effective)]
        if vendor:
            stats, _ = self._vendor_stats(vendor, effective)
            rate = {tool: (stats[tool][0] + 1) / (stats[tool][1] + 2) for tool in tools if tool in stats}
            ahead = sorted((tool for tool in tools if rate.get(tool, 0) >= 0.5), key=lambda tool: -rate[tool])
            tools = ahead + [tool for tool in tools if tool not in ahead]
        return tools

    def _egress_hints_path(self):
        return self.state_dir / "egress-routes.json"

    def _egress_hints(self):
        try:
            data = json.loads(self._egress_hints_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _egress_hint(self, url, effective):
        """The connection that last got through this origin, if still fresh and still configured."""
        if not effective.origin_route_hint_ttl_seconds:
            return None
        try:
            entry = self._egress_hints().get(self._pacing_key(url, effective))
        except (ValueError, TypeError, AttributeError):
            return None
        names = {egress["name"] for egress in self.egress_pool}
        if (isinstance(entry, dict) and entry.get("egress") in names
                and isinstance(entry.get("at"), (int, float))
                and time.time() - entry["at"] <= effective.origin_route_hint_ttl_seconds):
            return entry["egress"]
        return None

    def _record_egress_hint(self, url, effective, name):
        try:
            data = self._egress_hints()
            key = self._pacing_key(url, effective)
            if name is None:
                data.pop(key, None)
            else:
                data[key] = {"egress": name, "at": time.time()}
            fd, temporary = tempfile.mkstemp(prefix=".egress-routes-", dir=self.state_dir)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, sort_keys=True)
            os.replace(temporary, self._egress_hints_path())
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    async def _egress_pass(self, url, policy, adapter, policy_overrides, workload_assertions, egress, tools):
        """One read pinned to `tools`, through one configured connection."""
        from .egress import CURRENT as EGRESS
        token = EGRESS.set(egress)
        try:
            if policy is not None:
                return await self._read_unpaced(url, replace(policy, provider_candidates=tuple(tools)), None,
                                                adapter, workload_assertions=workload_assertions)
            return await self._read_unpaced(url, None, None, adapter, policy_overrides={
                **(policy_overrides or {}), "provider_candidates": list(tools)},
                workload_assertions=workload_assertions)
        finally:
            EGRESS.reset(token)

    def _egress_record(self, kept, record, replaced=None):
        """Put the pool's record on the kept receipt (and its trace); a direct read it
        replaced is linked, and its cost stays on the bill."""
        receipt = kept["receipt"]
        if replaced is not None:
            record["direct_trace_id"] = replaced.get("trace_id")
            costs = [receipt.get("cost_usd"), replaced.get("cost_usd")]
            receipt["cost_usd"] = None if None in costs else _total_cost(costs)
        receipt["egress"] = record
        self._annotate_trace(receipt, "egress")
        return kept

    async def _egress_first(self, url, policy, provider, adapter, policy_overrides,
                            workload_assertions, effective, state):
        """A site whose hint names a connection is read through it first."""
        if not self._egress_usable(url, provider, effective):
            return None
        name = self._egress_hint(url, effective)
        egress = next((item for item in self.egress_pool if item["name"] == name), None)
        tools = self._egress_tools(None, effective, egress) if egress is not None else []
        if egress is None or not tools:
            return None
        result = await self._egress_pass(url, policy, adapter, policy_overrides, workload_assertions,
                                         egress, tools)
        state["tried"] = [name]
        if (result.get("receipt") or {}).get("status") == "observed":
            return self._egress_record(result, {"used": name, "from_hint": True})
        self._record_egress_hint(url, effective, None)  # It stopped working; read as usual.
        return None

    async def _egress_retry(self, url, result, policy, provider, adapter, policy_overrides,
                            workload_assertions, effective, state):
        """A read the direct connection ended at a wall (or a rate limit) gets one
        pass through each other connection; the first that gets through wins and
        becomes the site's hint."""
        receipt = result.get("receipt") or {}
        code = (receipt.get("failure") or {}).get("code")
        if code not in effective.egress_failures or not self._egress_usable(url, provider, effective):
            return result
        tried = list(state.get("tried") or ())
        for egress in self.egress_pool:
            if egress["name"] in tried:
                continue  # The site's hint already tried it in this read.
            tools = self._egress_tools(receipt.get("wall_vendor"), effective, egress)
            if not tools:
                continue
            second = await self._egress_pass(url, policy, adapter, policy_overrides, workload_assertions,
                                             egress, tools)
            tried.append(egress["name"])
            if (second.get("receipt") or {}).get("status") == "observed":
                self._record_egress_hint(url, effective, egress["name"])
                return self._egress_record(second, {"used": egress["name"], "tried": tried, "after": code},
                                           replaced=receipt)
        if tried:
            self._egress_record(result, {"used": None, "tried": tried, "after": code})
        return result

    async def _warm_up_retry(self, url, result, policy, provider, adapter, policy_overrides,
                             workload_assertions, effective):
        """One more try at a walled page, entering through the site's home page."""
        receipt = result.get("receipt") or {}
        code = (receipt.get("failure") or {}).get("code")
        if (code not in ("BLOCKED", "CAPTCHA") or not effective.warm_up_on_wall or not isinstance(url, str)
                or provider or effective.provider or effective.provider_candidates is not None
                or effective.identity or effective.profile or not effective.allow_local_browser):
            return result
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.path in ("", "/"):
            return result  # The home page itself was walled; entering through it changes nothing.
        tools = [tool for tool in ("camoufox", "patchright", "local")
                 if self.providers.is_available(tool) and tool not in effective.exclude_providers]
        if not tools:
            return result
        entry = f"https://{parsed.netloc}/"
        settle = max(effective.settle_ms, 2500)
        if policy is not None:
            second = await self._read_unpaced(url, replace(policy, provider_candidates=tuple(tools),
                                                           public_entry_url=entry, settle_ms=settle),
                                              None, adapter, workload_assertions=workload_assertions)
        else:
            second = await self._read_unpaced(url, None, None, adapter, policy_overrides={
                **(policy_overrides or {}), "provider_candidates": tools, "public_entry_url": entry,
                "settle_ms": settle}, workload_assertions=workload_assertions)
        second_receipt = second.get("receipt") or {}
        record = {"entry": entry, "tools": tools, "status": second_receipt.get("status"),
                  "trace_id": second_receipt.get("trace_id"),
                  "failure": (second_receipt.get("failure") or {}).get("code")}
        if second_receipt.get("status") == "observed":
            second_receipt["warm_up"] = {**record, "after": code}
            return second
        receipt["warm_up"] = record
        return result

    async def _rendered_opinion(self, url, first, policy, adapter, policy_overrides,
                                workload_assertions, effective, terms):
        """When a page that didn't run its script looks like the wrong results,
        read it once in a browser: many sites apply the query client-side. The
        rendered page wins only if it is complete and on the query."""
        from .completeness import assess
        method = (first.get("receipt") or {}).get("method")
        manifest = next((item for item in self.providers.inspect() if item["id"] == method), None)
        if manifest is None or manifest.get("rendering") or not effective.allow_local_browser:
            return None
        tools = [tool for tool in ("local", "camoufox", "scrapling")
                 if self.providers.is_available(tool) and tool not in effective.exclude_providers]
        settle = max(effective.settle_ms, effective.completeness_settle_ms)
        second, tool = None, None
        # A browser that reads the page decides; one that meets a wall hands over to a stealthier one.
        for tool in tools[:2]:
            if policy is not None:
                second = await self._read_unpaced(url, replace(policy, provider_candidates=(tool,), settle_ms=settle),
                                                  None, adapter, workload_assertions=workload_assertions)
            else:
                second = await self._read_unpaced(url, None, None, adapter, policy_overrides={
                    **(policy_overrides or {}), "provider_candidates": [tool], "settle_ms": settle},
                    workload_assertions=workload_assertions)
            if (second.get("receipt") or {}).get("status") == "observed":
                break
        if second is None:
            return None
        receipt = second.get("receipt") or {}
        if receipt.get("status") != "observed":
            return None
        verdict = assess(url, second, expect_terms=terms)
        if not verdict["complete"] or verdict.get("off_query"):
            return None
        receipt["completeness"] = {**{key: verdict.get(key) for key in ("kind", "complete", "item_links", "prices",
                                                                       "text_chars")},
                                   "rendered_after_off_query": method, "kept": tool,
                                   "escalations": [{"provider": tool, "status": "observed", "complete": True,
                                                    "trace_id": receipt.get("trace_id")}]}
        return second

    def _lightning_tools(self, url, effective, adapter, count=2):
        """Lightning's racers: the first free, available tools on the completeness
        ladder (a reader that fetches from elsewhere and a stealth browser, by
        default), leaving out the tool this site's hint already starts with."""
        paid = {item["id"] for item in self.providers.inspect() if item["paid"]}
        hinted = self._origin_hint(url, effective)
        tools = []
        for identifier in effective.completeness_ladder:
            if (identifier in paid or identifier == hinted or identifier in effective.exclude_providers
                    or not self.providers.is_available(identifier)):
                continue
            try:
                self.providers.require_enabled(identifier, effective,
                                               operation="extract" if adapter else "read")
            except WebFailure:
                continue
            tools.append(identifier)
            if len(tools) == count:
                break
        return tools

    async def _lightning(self, url, policy, provider, adapter, policy_overrides,
                         workload_assertions, effective, racers):
        """Lightning mode: the read and its racers start together, half a second
        apart, and the main read leaves the racers' tools to them."""
        if policy is not None:
            main_read = self._read_unpaced(url, replace(policy, exclude_providers=tuple(
                dict.fromkeys(tuple(policy.exclude_providers) + tuple(racers)))), provider, adapter,
                workload_assertions=workload_assertions)
        else:
            excluded = list(dict.fromkeys(list((policy_overrides or {}).get("exclude_providers") or ())
                                          + list(racers)))
            main_read = self._read_unpaced(url, None, provider, adapter, policy_overrides={
                **(policy_overrides or {}), "exclude_providers": excluded},
                workload_assertions=workload_assertions)
        return await self._race(url, policy, adapter, policy_overrides, workload_assertions, effective,
                                asyncio.ensure_future(main_read), racers, lightning=True)

    async def _race(self, url, policy, adapter, policy_overrides, workload_assertions, effective,
                    main, racers, *, lightning, after_seconds=0):
        """The main read (already running) against its racers, which start now,
        half a second apart. The first complete page wins and the rest are
        cancelled. When none is complete, the more complete of the main read's
        page and the racers' best stands; when the main read fails, the racers'
        best does."""
        from .completeness import assess

        async def pinned(tool, delay):
            await asyncio.sleep(delay)
            if policy is not None:
                return await self._read_unpaced(url, replace(policy, provider_candidates=(tool,)), None,
                                                 adapter, workload_assertions=workload_assertions)
            return await self._read_unpaced(url, None, None, adapter, policy_overrides={
                **(policy_overrides or {}), "provider_candidates": [tool]},
                workload_assertions=workload_assertions)
        tasks = {asyncio.ensure_future(pinned(tool, 0.5 * (index + (1 if lightning else 0)))): tool
                 for index, tool in enumerate(racers)}
        record = {"provider": racers[0], "providers": list(racers), "after_seconds": after_seconds,
                  "won": False, "tried": []}
        if lightning:
            record["lightning"] = True
        pending = {main, *tasks}
        main_result, main_score, best, best_score, best_tool = None, None, None, None, None

        def won(result, tool, **extra):
            receipt = result["receipt"]
            record.update(won=True, provider=tool, status="observed", trace_id=receipt.get("trace_id"), **extra)
            if main_result is not None:
                # The main read finished (and may have paid): its cost stays on the bill.
                costs = [receipt.get("cost_usd"), (main_result.get("receipt") or {}).get("cost_usd")]
                receipt["cost_usd"] = None if None in costs else _total_cost(costs)
            receipt["hedge"] = record
            return result
        try:
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    try:
                        result = task.result()
                    except Exception:
                        continue
                    if task is main:
                        main_result = result
                    else:
                        record["tried"].append(tasks[task])
                    receipt = result.get("receipt") or {}
                    if receipt.get("status") != "observed":
                        continue
                    verdict = assess(url, result, expect_terms=effective.expect_terms)
                    if verdict.get("complete"):
                        if task is main:
                            receipt["hedge"] = record
                            return result
                        return won(result, tasks[task])
                    if task is main:
                        main_score = verdict["score"]
                    elif best_score is None or verdict["score"] > best_score:
                        best, best_score, best_tool = result, verdict["score"], tasks[task]
            failed = (main_result or {}).get("receipt") or {}
            if failed.get("status") != "observed" and best is not None:
                return won(best, best_tool, after_failure=(failed.get("failure") or {}).get("code"),
                           walled=sorted({item["provider"] for item in failed.get("attempts") or ()
                                          if isinstance(item, dict) and item.get("provider")
                                          and item.get("failure") in effective.escalation_failures}))
            if best is not None and main_score is not None and best_score > main_score:
                # Neither is complete; the racer's page is the more complete one.
                return won(best, best_tool)
            if main_result is None:
                return await main
            failed["hedge"] = record
            return main_result
        finally:
            for task in (main, *tasks):
                if not task.done():
                    task.cancel()
            await asyncio.gather(main, *tasks, return_exceptions=True)

    async def _hedged(self, url, policy, provider, adapter, policy_overrides,
                      workload_assertions, effective):
        """The read, raced when it is slow (smart lightning).

        A blocked or script-heavy site can take several tools in turn. Once the
        read has run hedge_after_seconds without a page, hedge_racers reads
        pinned to the first free ladder tools start beside it, and the first
        complete page wins. Easy pages are done before then and never race.
        Free tools only, so racing never costs money. With lightning, the
        racers start at once."""
        automatic = (isinstance(url, str) and self.completeness_enabled
                     and not (provider or effective.provider or effective.provider_candidates is not None
                              or effective.identity or effective.profile))
        from .site_modules import ACTIVE as ACTIVE_MODULE
        plain = automatic and adapter is None and ACTIVE_MODULE.get() is None
        if plain and effective.lightning:
            racers = self._lightning_tools(url, effective, adapter)
            if racers:
                return await self._lightning(url, policy, provider, adapter, policy_overrides,
                                             workload_assertions, effective, racers)
        claimed = set()
        token = _RACE_CLAIMED.set((url, claimed))
        try:
            main = asyncio.ensure_future(self._read_unpaced(
                url, policy, provider, adapter, policy_overrides=policy_overrides,
                workload_assertions=workload_assertions))
        finally:
            _RACE_CLAIMED.reset(token)
        racers = (self._lightning_tools(url, effective, adapter, count=effective.hedge_racers)
                  if effective.hedge_after_seconds and automatic else [])
        if not racers:
            return await main
        done, _ = await asyncio.wait({main}, timeout=effective.hedge_after_seconds)
        if done:
            return main.result()
        claimed.update(racers)
        return await self._race(url, policy, adapter, policy_overrides, workload_assertions, effective,
                                main, racers, lightning=False, after_seconds=effective.hedge_after_seconds)

    def _grade(self, url, result, effective):
        """receipt.quality on every observed document read: good, partial or poor,
        and why. Agents (and fallbacks to paid tools) can route on it."""
        receipt = result.get("receipt") or {}
        if receipt.get("status") != "observed" or not isinstance(url, str):
            return
        content_type = str(result.get("content_type") or "")
        if "html" not in content_type and "markdown" not in content_type:
            return
        from .completeness import assess, quality
        try:
            verdict = assess(result.get("url") or url, result, expect_terms=effective.expect_terms)
        except Exception:
            return
        record = receipt.get("completeness") or {}
        # The escalation's own verdict wins where it has one (it saw every step).
        for key in ("off_query", "placeholder", "needs_interaction"):
            if record.get(key):
                verdict[key] = record[key]
        receipt["quality"] = quality(verdict, receipt)

    async def _second_opinion(self, url, first, policy, adapter, policy_overrides,
                              workload_assertions, effective):
        """Ask a rendering provider too, and keep the page with clearly more content.

        A plain HTTP page that loads its results with JavaScript still carries
        the site's header and footer text, so no fixed threshold separates it
        from a small real page. The rendered read decides; when it fails or
        adds little, the HTTP page stands.
        """
        await asyncio.sleep(min(effective.origin_min_interval_seconds, 2.0))
        if policy is not None:
            second = await self._read_unpaced(url, replace(policy, render=True), None, adapter,
                                              workload_assertions=workload_assertions)
        else:
            second = await self._read_unpaced(url, None, None, adapter,
                                              policy_overrides={**(policy_overrides or {}), "render": True},
                                              workload_assertions=workload_assertions)
        second_receipt = second.get("receipt") or {}
        first_chars = len((first.get("text") or "").strip())
        second_chars = len((second.get("text") or "").strip())
        rendered_wins = (second_receipt.get("status") == "observed"
                         and second_chars >= max(first_chars * 1.5, first_chars + 1000))
        chosen, other = (second, first) if rendered_wins else (first, second)
        other_receipt = other.get("receipt") or {}
        receipt = chosen["receipt"]
        if rendered_wins and (first.get("receipt") or {}).get("hedge") and "hedge" not in receipt:
            receipt["hedge"] = first["receipt"]["hedge"]
        receipt["second_opinion"] = {
            "kept": receipt.get("method"), "other": other_receipt.get("method"),
            "other_status": other_receipt.get("status"),
            "other_trace_id": other_receipt.get("trace_id"),
            "text_chars": {"http": first_chars, "rendered": second_chars}}
        costs = [receipt.get("cost_usd"), other_receipt.get("cost_usd")]
        receipt["cost_usd"] = None if None in costs else _total_cost(costs)
        return chosen

    async def _ensure_complete(self, url, first, policy, adapter, policy_overrides,
                               workload_assertions, effective):
        """Escalate along completeness_ladder until the page looks complete.

        Each step is an ordinary read pinned to one provider, so Core's content
        checks, receipts and evidence apply. The most complete page wins; when
        no step helps, the first page stands.
        """
        from .completeness import assess
        terms = effective.expect_terms
        verdict = assess(url, first, expect_terms=terms)
        record = {key: verdict.get(key) for key in ("kind", "complete", "item_links", "prices",
                                                    "text_chars", "reason")}
        if verdict.get("query"):
            record["query"] = verdict["query"]
        for key in ("placeholder", "link_text_share", "needs_interaction", "article"):
            if verdict.get(key):
                record[key] = verdict[key]
        first["receipt"]["completeness"] = record
        if verdict.get("off_query") and effective.completeness_escalation:
            rendered = await self._rendered_opinion(url, first, policy, adapter, policy_overrides,
                                                    workload_assertions, effective, terms)
            if rendered is not None:
                return rendered
        if verdict.get("off_query"):
            # Another tool would read the same wrong page: hand it back, named.
            record["off_query"] = True
            first["receipt"]["next_step"] = {
                "reason": "off_query",
                "how": ("These results don't mention the query, so this is probably the site's"
                        " default feed: the search URL's query parameter may be wrong or ignored."
                        " Check the site's own search URL, or save it in a site module.")}
            return first
        if not verdict["complete"] and self._module_satisfied(first, url, terms):
            # The site module knows where this site keeps its results.
            record.update(complete=True, reason=None, module="passed")
            return first
        borderline = (verdict["complete"] and verdict.get("kind") == "search"
                      and verdict.get("item_links", 0) < effective.completeness_borderline_items
                      and verdict.get("prices", 0) < 2 * 4)
        one_action_away = (effective.interact_on_escalation and effective.allow_local_browser
                           and self.providers.is_available("local")
                           # Only pages that name the action they need: a thin search page is
                           # better served by a stronger tool than by pressing buttons.
                           and (verdict.get("needs_interaction")
                                or "consent" in str(verdict.get("reason") or "")
                                or (not verdict["complete"] and _consent_leads(first.get("text")))))
        if verdict["complete"] and not borderline and not one_action_away:
            return first
        receipt = first["receipt"]
        tried = {attempt.get("provider") for attempt in receipt.get("attempts") or ()}
        tried.add((receipt.get("second_opinion") or {}).get("other"))
        tried.update((receipt.get("hedge") or {}).get("walled") or ())
        tried.update((receipt.get("hedge") or {}).get("tried") or ())
        ladder = []
        from .profiles import ACTIVE
        carriers = ACTIVE.get().carriers() if effective.profile and ACTIVE.get() else None
        for identifier in effective.completeness_ladder:
            if (identifier in tried or identifier in effective.exclude_providers
                    or not self.providers.is_available(identifier)):
                continue
            if carriers is not None and identifier not in carriers:
                continue
            try:
                self.providers.require_enabled(identifier, effective,
                                               operation="extract" if adapter else "read")
            except WebFailure:
                continue
            ladder.append(identifier)
        if one_action_away:
            ladder = ["interact"] + [item for item in ladder if item != "local"]
            if verdict["complete"]:
                # A complete page whose prices wait for a choice: only the action can add them.
                ladder = ["interact"]
        best, best_score, steps = first, verdict["score"], []
        if borderline:
            # A narrow pass gets one opinion from the strongest allowed tool:
            # a paid one when the call allows them.
            paid = {item["id"] for item in self.providers.inspect() if item["paid"]}
            ladder = ([item for item in ladder if item in paid] or ladder)[:1]
            record["borderline"] = True
        costs = [receipt.get("cost_usd")]
        deadline = time.monotonic() + effective.completeness_deadline_seconds
        settle = max(effective.settle_ms, effective.completeness_settle_ms)
        paid = {item["id"] for item in self.providers.inspect() if item["paid"]}
        queue = list(ladder[:effective.completeness_max_extra_reads])

        async def run_step(identifier, delay):
            await asyncio.sleep(delay)
            # "interact": the local browser with the safe actions (interactions.py).
            pinned = "local" if identifier == "interact" else identifier
            extra = {"interactions": ("dismiss_consent", "reveal", "load_more")} if identifier == "interact" else {}
            if policy is not None:
                return identifier, await self._read_unpaced(
                    url, replace(policy, provider_candidates=(pinned,), settle_ms=settle, **extra),
                    None, adapter, workload_assertions=workload_assertions)
            return identifier, await self._read_unpaced(url, None, None, adapter, policy_overrides={
                **(policy_overrides or {}), "provider_candidates": [pinned], "settle_ms": settle,
                **{key: list(value) for key, value in extra.items()}},
                workload_assertions=workload_assertions)

        agreements = {"count": 0}

        def same_page(step_verdict):
            """A step that read what the first read did: same item links, similar text."""
            if not step_verdict:
                return False
            first_chars, chars = verdict.get("text_chars") or 0, step_verdict.get("text_chars") or 0
            return (abs((step_verdict.get("item_links") or 0) - (verdict.get("item_links") or 0)) <= 1
                    and abs(chars - first_chars) <= 0.2 * max(chars, first_chars, 1))

        def judge(identifier, step):
            """Record one step; True when it is a complete page."""
            nonlocal best, best_score, record
            step_receipt = step.get("receipt") or {}
            costs.append(step_receipt.get("cost_usd"))
            observed = step_receipt.get("status") == "observed"
            step_verdict = assess(url, step, expect_terms=terms) if observed else None
            if (step_verdict and not step_verdict["complete"]
                    and self._module_satisfied(step, url, terms)):
                step_verdict = {**step_verdict, "complete": True, "reason": None,
                                "score": max(step_verdict["score"], best_score + 1)}
            steps.append({"provider": identifier, "status": step_receipt.get("status"),
                          "failure": (step_receipt.get("failure") or {}).get("code"),
                          "complete": step_verdict["complete"] if step_verdict else False,
                          "trace_id": step_receipt.get("trace_id")})
            # Only browsers count: a plain fetcher agreeing says little about what a
            # stronger browser would see.
            renders = next((item.get("rendering") for item in self.providers.inspect() if item["id"] == identifier),
                           False) or identifier == "interact"
            if renders and step_verdict and not step_verdict["complete"] and same_page(step_verdict):
                agreements["count"] += 1
            if step_verdict and step_verdict["score"] > best_score:
                best, best_score = step, step_verdict["score"]
                record = {**record, "complete": step_verdict["complete"],
                          "item_links": step_verdict.get("item_links"),
                          "prices": step_verdict.get("prices"),
                          "text_chars": step_verdict.get("text_chars"),
                          "reason": step_verdict.get("reason")}
            return bool(step_verdict and step_verdict["complete"])

        # Free tools race in small groups and the first complete page wins; the
        # rest are cancelled. A paid tool always runs alone, so racing never
        # spends twice. Starts are staggered so a site sees at most a couple of
        # reads at once.
        width = 4 if effective.lightning else effective.completeness_parallel
        pause = min(effective.origin_min_interval_seconds, 1.0)
        finished = False
        while queue and not finished and time.monotonic() <= deadline:
            group = [queue.pop(0)]
            if group[0] not in paid:
                while queue and queue[0] not in paid and len(group) < width:
                    group.append(queue.pop(0))
            tasks = [asyncio.create_task(run_step(identifier, pause + index * 0.5))
                     for index, identifier in enumerate(group)]
            # A step is cut off at its own limit or the climb's deadline, whichever
            # comes first; the best page so far stands.
            limit = min(deadline - time.monotonic(),
                        effective.completeness_step_timeout_seconds + pause + 0.5 * len(group))
            try:
                for next_done in asyncio.as_completed(tasks, timeout=max(limit, 0.1)):
                    try:
                        identifier, step = await next_done
                    except asyncio.TimeoutError:
                        for identifier, task in zip(group, tasks):
                            if not task.done():
                                steps.append({"provider": identifier, "status": "failed", "failure": "TIMEOUT",
                                              "complete": False, "cut_off": True})
                        record["cut_off"] = True
                        break
                    if judge(identifier, step):
                        finished = True
                        break
                    if agreements["count"] >= 2 and effective.completeness_stop_on_agreement:
                        # Two more tools read the page the first one did: it is this
                        # page, not an unfinished one. Stop climbing.
                        record["agreed"] = True
                        finished = True
                        break
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        if len(steps) > 1:
            record["raced"] = width > 1
        record["escalations"] = steps
        record["kept"] = best["receipt"].get("method")
        best["receipt"]["completeness"] = record
        best["receipt"]["cost_usd"] = None if None in costs else _total_cost(costs)
        return best

    async def watch(self, url: str, *, link_pattern: str | None = None, policy: WebPolicy | None = None,
                    policy_overrides: dict | None = None, state_key: str | None = None,
                    max_seen: int = 5000) -> dict:
        """One poll of a listing page: return links not seen on earlier polls.

        Generic and site-agnostic. The caller schedules polls and decides what a
        link means; FrankenSurf reads the page (paced, with the usual receipts),
        collects same-site links, optionally filtered by ``link_pattern`` (a
        regular expression matched against the absolute URL), and diffs them
        against the persisted set for this watch. The first poll reports every
        current link as new with ``first_poll`` set, so the caller can choose to
        treat it as a baseline.
        """
        if link_pattern is not None:
            pattern = re.compile(link_pattern)
        else:
            pattern = None
        if type(max_seen) is not int or max_seen < 1:
            raise ValueError("max_seen must be a positive integer")
        key = state_key or url
        path = self.state_dir / "watches" / (hashlib.sha256(key.encode()).hexdigest() + ".json")
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            seen = list(state.get("seen") or [])
        except (OSError, ValueError):
            state, seen = None, []
        result = await self.read(url, policy, policy_overrides=policy_overrides)
        receipt = result.get("receipt") or {}
        if receipt.get("status") != "observed":
            return {"url": url, "status": "failed", "failure": receipt.get("failure"),
                    "new": [], "first_poll": state is None, "receipt": receipt}
        base = result.get("url") or url
        host = (urlparse(base).hostname or "").removeprefix("www.")
        links, order = {}, []
        content = result.get("content") or ""
        if "html" in (result.get("content_type") or "html"):
            soup = BeautifulSoup(content, "html.parser")
            for anchor in soup.select("a[href]"):
                href = urljoin(base, anchor["href"]).split("#", 1)[0]
                parsed = urlparse(href)
                if parsed.scheme not in {"http", "https"} or (parsed.hostname or "").removeprefix("www.") != host:
                    continue
                if pattern is not None and not pattern.search(href):
                    continue
                text = anchor.get_text(" ", strip=True)[:300]
                if href not in links:
                    links[href] = text
                    order.append(href)
                elif text and not links[href]:
                    links[href] = text
        seen_set = set(seen)
        new = [{"url": href, "text": links[href]} for href in order if href not in seen_set]
        merged = seen + [item["url"] for item in new]
        merged = merged[-max_seen:]
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".watch-", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"url": url, "link_pattern": link_pattern, "seen": merged,
                       "last_polled_at": receipt.get("observed_at")}, stream)
        os.replace(temporary, path)
        return {"url": url, "status": "observed", "new": new, "first_poll": state is None,
                "links_on_page": len(order), "seen_count": len(merged), "receipt": receipt}

    async def watch_sites(self, sites: list[str], *, since: str | None = None, read_new: bool = False,
                          max_new_per_site: int = 50, max_reads: int = 100,
                          policy_overrides: dict | None = None) -> dict:
        """New pages on many sites, from what each site publishes about itself.

        For each site (a home page or any URL on it): the feeds it advertises
        (<link rel="alternate">), the sitemaps robots.txt names, and, when it
        advertises no feed, a few conventional feed paths. These are found once
        and remembered for a week. Every poll fetches them with conditional
        requests (ETag / Last-Modified), so an unchanged feed is one small 304.
        Pages already reported are remembered per site, and the same story under
        two URLs (syndication) is reported once.

        since (ISO 8601) limits the first poll; later polls report what is new
        since the last one. read_new=True also reads up to max_reads new pages
        with main_content as one paced batch.
        """
        from .site_feeds import (CONVENTIONAL_FEEDS, find_feeds, newer_than, page_key, parse_feed,
                                 parse_sitemap, pick_child_sitemaps, robots_sitemaps, story_key)
        from .search import parse_date
        if not isinstance(sites, list) or not 1 <= len(sites) <= 500:
            raise ValueError("sites is a list of 1 to 500 URLs")
        floor = parse_date(since) if since else None
        if since and floor is None:
            raise ValueError("since must be an ISO 8601 date or time")
        overrides = dict(policy_overrides or {})
        folder = self.state_dir / "site-watches"
        folder.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        stories, everything, reports = set(), [], []
        counters = {"requests": 0, "unchanged": 0}

        async def fetch(url, validators):
            """(status, text) with a conditional GET; falls back to a full read when refused."""
            _validate_url(url)
            if overrides.get("block_private_network") or self.block_private_network:
                await _refuse_private(url)
            headers = {}
            known = validators.get(url) or {}
            if known.get("etag"):
                headers["If-None-Match"] = known["etag"]
            if known.get("last_modified"):
                headers["If-Modified-Since"] = known["last_modified"]
            host = urlparse(url).netloc
            delay = self._domain_next.get(host, 0) - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._domain_next[host] = time.monotonic() + max(self._domain_delay, 0.5)
            counters["requests"] += 1
            try:
                response = await self._http.get(url, headers=headers, timeout=20)
            except httpx.HTTPError:
                response = None
            if response is not None and response.status_code == 304:
                counters["unchanged"] += 1
                return 304, None
            if response is not None and response.status_code == 200 and len(response.content) <= 20_000_000:
                validators[url] = {key: value for key, value in (
                    ("etag", response.headers.get("etag")),
                    ("last_modified", response.headers.get("last-modified"))) if value}
                return 200, response.text
            if response is not None and response.status_code in (404, 410):
                return response.status_code, None
            # Refused or walled: the read ladder can still get through (no validators then).
            read = await self.read(url, adapter="rss" if url.endswith(".xml") else None,
                                   policy_overrides={**overrides, "completeness_escalation": False})
            receipt = read.get("receipt") or {}
            if receipt.get("status") == "observed":
                return 200, read.get("content") or ""
            return (receipt.get("http_status") or 0), None

        async def poll(site):
            parsed = urlparse(site)
            origin = f"{parsed.scheme}://{parsed.netloc}"
            path = folder / (hashlib.sha256(origin.encode()).hexdigest() + ".json")
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                state = {}
            first = not state
            validators = state.get("validators") or {}
            seen = list(state.get("seen") or [])
            seen_set = set(seen)
            discovered_at = parse_date(state.get("discovered_at"))
            report = {"site": origin, "first_poll": first}
            if discovered_at is None or (now - discovered_at).days >= 7:
                feeds, sitemaps = [], []
                home = await self.read(site, policy_overrides={**overrides, "completeness_escalation": False})
                counters["requests"] += 1
                if (home.get("receipt") or {}).get("status") == "observed":
                    feeds = find_feeds(home.get("content") or "", home.get("url") or site)
                status, robots = await fetch(origin + "/robots.txt", {})
                if status == 200 and robots:
                    sitemaps = robots_sitemaps(robots, origin)
                if not feeds:
                    for candidate in CONVENTIONAL_FEEDS:
                        status, body = await fetch(origin + candidate, validators)
                        if status == 200 and body and parse_feed(body):
                            feeds.append(origin + candidate)
                            break
                if not sitemaps:
                    sitemaps = [origin + "/sitemap.xml"]
                state.update(feeds=feeds, sitemaps=sitemaps, discovered_at=now.isoformat())
            report.update(feeds=state.get("feeds") or [], sitemaps=state.get("sitemaps") or [])
            entries = []
            for feed in state.get("feeds") or []:
                status, body = await fetch(feed, validators)
                if status == 200 and body:
                    for entry in parse_feed(body) or []:
                        entries.append({**entry, "source": "feed"})
            # Sitemaps when feeds are absent or thin: they list everything with dates.
            since_here = floor if first else parse_date(state.get("last_polled_at"))
            if first and since_here is None:
                since_here = now - timedelta(days=2)
            if len(entries) < 5:
                # The first poll walks the index to its newest children. Later polls
                # fetch the index conditionally (304 when unchanged), any child that
                # is new since last time, and the "hot" children that held recent
                # pages: a few small requests, not a crawl.
                known = set(state.get("known_children") or [])
                hot = list(state.get("hot_sitemaps") or [])
                queue = list(state.get("sitemaps") or [])[:3]
                visited, new_hot = 0, []
                while queue and visited < 8:
                    sitemap = queue.pop(0)
                    visited += 1
                    status, body = await fetch(sitemap, validators)
                    parsed_map = parse_sitemap(body) if status == 200 and body else None
                    if not parsed_map:
                        continue
                    children = [child for child in parsed_map["sitemaps"] if child.get("url")]
                    if first or not hot:
                        queue += pick_child_sitemaps(children)
                    else:
                        queue += [child["url"] for child in children if child["url"] not in known][:3]
                    known.update(child["url"] for child in children)
                    pages = [{**page, "source": "sitemap"} for page in parsed_map["pages"]]
                    if any(page.get("published") and newer_than(page, since_here) for page in pages):
                        new_hot.append(sitemap)
                    entries += pages
                    if not queue and hot:
                        queue, hot = [url for url in hot if url != sitemap], []
                state.update(known_children=sorted(known)[-500:],
                             hot_sitemaps=new_hot[:3] or list(state.get("hot_sitemaps") or [])[:3])
            new, more = [], 0
            entries.sort(key=lambda entry: entry.get("published") or "", reverse=True)
            for entry in entries:
                url = entry.get("url") or ""
                try:
                    _validate_url(url)
                except WebFailure:
                    continue
                if urlparse(url).netloc.removeprefix("www.") != parsed.netloc.removeprefix("www.") \
                        and entry["source"] == "sitemap":
                    continue
                key = page_key(url)
                if key in seen_set or not newer_than(entry, since_here):
                    continue
                seen_set.add(key)
                seen.append(key)
                # Undated pages are the baseline, not news: on the first poll from
                # anywhere, and always from sitemaps (a sitemap lists every page it
                # has, so an undated page there is old until shown otherwise).
                if not entry.get("published") and (first or entry["source"] == "sitemap"):
                    continue
                story = story_key(entry.get("title"))
                if story and story in stories:
                    continue
                if story:
                    stories.add(story)
                if len(new) >= max_new_per_site:
                    more += 1  # Remembered as seen, counted, not listed.
                    continue
                new.append({**entry, "site": origin})
            state.update(validators=validators, seen=seen[-50000:], last_polled_at=now.isoformat())
            fd, temporary = tempfile.mkstemp(prefix=".watch-", dir=folder)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(state, stream)
            os.replace(temporary, path)
            report.update(new=len(new), more=more, entries_seen=len(entries))
            return report, new

        results = await asyncio.gather(*(poll(site) for site in sites), return_exceptions=True)
        for site, outcome in zip(sites, results):
            if isinstance(outcome, BaseException):
                reports.append({"site": site, "error": type(outcome).__name__, "new": 0})
                continue
            reports.append(outcome[0])
            everything += outcome[1]
        everything.sort(key=lambda entry: entry.get("published") or "", reverse=True)
        answer = {"new": everything, "sites": reports, "polled_at": now.isoformat(),
                  "requests": counters["requests"], "unchanged_feeds": counters["unchanged"]}
        if read_new and everything:
            pages = await self.batch([entry["url"] for entry in everything[:max_reads]],
                                     policy_overrides={**overrides, "main_content": True})
            fingerprints, kept = set(), []
            for entry, page in zip(everything, pages):
                text = page.get("main_text") or page.get("text") or ""
                fingerprint = hashlib.sha256(" ".join(text.split())[:3000].lower().encode()).hexdigest()
                duplicate = bool(text) and fingerprint in fingerprints
                fingerprints.add(fingerprint)
                kept.append({**entry, "status": (page.get("receipt") or {}).get("status"),
                             "quality": ((page.get("receipt") or {}).get("quality") or {}).get("grade"),
                             "main_text": None if duplicate else page.get("main_text"),
                             "duplicate_of_earlier": duplicate,
                             "trace_id": (page.get("receipt") or {}).get("trace_id")})
            answer["pages"] = kept
        return answer

    async def _read_unpaced(self, url: str, policy: WebPolicy | None = None, provider: str | None = None,
                   adapter: str | None = None, *, policy_overrides: dict | None = None,
                   workload_assertions: dict | None = None) -> dict:
        from .routes import request_policy, RouteRecipeError
        effective, protected = request_policy(policy, policy_overrides)
        from .repair import normalize_workload_assertions
        assertions = normalize_workload_assertions(
            workload_assertions,
            max_count=effective.workload_assertion_max_count,
            max_bytes=effective.workload_assertion_max_bytes)
        if assertions is not None and adapter is None:
            raise ValueError("workload assertions require extraction adapter")
        if provider is not None:
            effective = replace(effective, provider=provider)
        # Identity and explicit route decisions have authority over operator recipes.
        if effective.identity or effective.provider is not None or effective.provider_candidates is not None:
            return await self._read(url, effective, adapter=adapter,
                _workload_assertions=assertions)
        started = time.monotonic()
        try:
            _validate_url(url)
            version = None
            if adapter:
                version = self.adapters.require_enabled(adapter).version
            planning = self.routes.plan(url, "extract" if adapter else "read", adapter,
                version, effective, protected, providers=self.providers,
                configured=("steel",) if self.steel_api_url else ())
        except (WebFailure, RouteRecipeError) as exc:
            error = exc if isinstance(exc, WebFailure) else WebFailure(exc.code, str(exc))
            return await self._read(url, effective, adapter=adapter,
                                    _planning_failure=error,
                                    _workload_assertions=assertions)
        seed_policy = None
        seed_providers = ()
        seed_metadata = ()
        if planning.seeds:
            seed_policy = planning.seeds[0].policy
            if any(plan.policy != seed_policy for plan in planning.seeds[1:]):
                error = WebFailure("ROUTE_CONFIG_INVALID",
                    "Matching compatibility route seeds disagree on policy defaults")
                return await self._read(url, effective, adapter=adapter,
                                        _planning_failure=error,
                                        _workload_assertions=assertions)
            seed_providers = tuple(dict.fromkeys(
                plan.provider for plan in planning.seeds))
            seed_metadata = tuple(
                plan.recipe.metadata() for plan in planning.seeds)
        history = []
        budget_cap = effective.max_cost_usd
        for plan in planning.plans:
            if budget_cap is None:
                budget_cap = plan.policy.max_cost_usd
            caps = [cap for cap in (budget_cap, plan.policy.max_cost_usd) if cap is not None]
            plan_policy = replace(plan.policy, max_cost_usd=min(caps) if caps else None)
            result = await self._read(url, plan_policy, provider=plan.provider, adapter=plan.adapter,
                                      _route_scope=plan.recipe.fingerprint,
                                      _recipe=plan.recipe.metadata(),
                                      _prior_costs=[item["receipt"].get("cost_usd") for item in history],
                                      _workload_assertions=assertions)
            history.append(result)
            failure = (result["receipt"].get("failure") or {}).get("code")
            if result["receipt"]["status"] == "observed" or failure in effective.terminal_failures or failure in effective.context_stop_failures:
                break
        else:
            fallback = seed_policy or effective
            caps = [cap for cap in (budget_cap, fallback.max_cost_usd)
                    if cap is not None]
            fallback = replace(fallback,
                               max_cost_usd=min(caps) if caps else None)
            result = await self._read(url, fallback, adapter=adapter,
                _route_seeds=seed_metadata, _seed_providers=seed_providers,
                _prior_costs=[item["receipt"].get("cost_usd") for item in history],
                _workload_assertions=assertions)
        if not history and not planning.skipped:
            return result
        prior = history[:-1] if history and result is history[-1] else history
        receipt = result["receipt"]
        receipt["attempts"] = [attempt for item in prior for attempt in item["receipt"]["attempts"]] + receipt["attempts"]
        receipt["routing"] = {**receipt.get("routing", {}),
            "operator_recipes": {"skipped": list(planning.skipped),
                "prior_trace_ids": [item["receipt"]["trace_id"] for item in prior],
                "automatic_promotion": False}}
        receipt["latency_ms"] = round((time.monotonic() - started) * 1000)
        costs = [item["receipt"].get("cost_usd") for item in prior] + [receipt.get("cost_usd")]
        receipt["cost_usd"] = _total_cost([_measured_cost(cost) for cost in costs])
        if budget_cap is not None:
            receipt.update(max_cost_usd=budget_cap,
                cost_budget_satisfied=receipt["cost_usd"] is not None and receipt["cost_usd"] <= budget_cap)
        # Individual acquisitions already recorded their own routing observations.
        self._save_trace(result, record_observations=False)
        return result

    def _plan_route(self, url, policy, adapter, _route_scope, _route_seeds, _seed_providers):
        """Acquisition context and the public provider order (route memory, hints, seeds).

        Returns (policy, route_context, public_route); public_route is None for a
        caller-chosen route.
        """
        from .route_memory import acquisition_context
        caller_route = bool(policy.identity or policy.provider or policy.provider_candidates is not None)
        public_route = None
        route_context = acquisition_context(policy)
        if _route_scope is not None:
            if route_context is not None:
                # Recipe observations cannot train another recipe or ordinary
                # route when the resulting policy values happen to be equal.
                route_context = hashlib.sha256(json.dumps([route_context,
                    {"operator_route_recipe": _route_scope}],
                    sort_keys=True).encode()).hexdigest()
        elif _route_seeds:
            if route_context is not None:
                route_context = hashlib.sha256(json.dumps([route_context,
                    {"catalog_route_seeds": [seed["sha256"]
                        for seed in _route_seeds]}],
                    sort_keys=True).encode()).hexdigest()
        if not caller_route and _route_scope is None and policy.navigation_page == 1:
            candidates = (list(policy.provider_candidates) if policy.provider_candidates else
                          self.providers.candidates(policy,
                              configured=("steel",) if self.steel_api_url else (),
                              operation="extract" if adapter else "read"))
            # A validated exact compatibility seed can admit an installed
            # route-scoped provider without hiding ordinary candidates.
            seeded = list(dict.fromkeys(_seed_providers))
            candidates = seeded + [
                identifier for identifier in candidates
                if identifier not in seeded]
            real_browser = (policy.allow_real_browser and policy.allow_local_browser
                            and "real_browser" not in candidates
                            and "real_browser" not in policy.exclude_providers
                            and self.providers.is_available("real_browser"))
            if real_browser:
                # Opt-in: tried after the rest (but before a stored copy), or
                # sooner once walls move it ahead.
                archive = {item["id"] for item in self.providers.inspect() if item.get("archive")}
                at = next((index for index, item in enumerate(candidates) if item in archive), len(candidates))
                candidates.insert(at, "real_browser")
            if (policy.allow_handoff and "handoff" not in candidates
                    and self.providers.is_available("handoff")):
                candidates.append("handoff")
            if policy.profile:
                from .profiles import ACTIVE
                carriers = ACTIVE.get().carriers() if ACTIVE.get() else ()
                candidates = [identifier for identifier in candidates if identifier in carriers]
            if policy.exclude_providers:
                candidates = [i for i in candidates if i not in policy.exclude_providers]
            if policy.try_harder:
                # The agent judged a cheaper read wrong: strongest tools first,
                # including paid ones the call allows that plain routing keeps
                # until after the free ones.
                strong = [i for i in policy.completeness_ladder
                          if i not in policy.exclude_providers and self.providers.is_available(i)
                          and _allowed(self.providers, i, policy)]
                candidates = strong + [i for i in candidates if i not in strong]
            manifests = [item for item in self.providers.inspect()
                         if item["id"] in candidates]
            version = None
            if adapter:
                version = next((item["version"] for item in self.adapters.inspect()
                                if item["id"] == adapter and item["enabled"]), None)
            operation = "extract" if adapter else "read"
            if policy.use_route_memory:
                from .route_memory import provider_plan
                learned = provider_plan(self.state_dir, url, operation, adapter, version,
                    manifests, candidates, policy.route_memory_ttl_seconds,
                    policy.route_memory_min_samples, route_context,
                    adapter_binding_id=self.adapters.binding_id(adapter)
                    if adapter else None,
                    # An observed attempt already passed Core's content checks
                    # (app shell, empty render, challenge and sign-in pages), so
                    # recent observations on this exact path are the evidence.
                    require_independent_verification=False)
            else:
                learned = {"ordered": candidates, "preferred": None,
                    "execution": "sequential", "basis": "route memory disabled by policy",
                    "evidence": []}
            hinted = self._origin_hint(url, policy)
            if (learned["preferred"] is None and hinted is not None
                    and hinted in learned["ordered"] and learned["ordered"][0] != hinted):
                learned = {**learned, "ordered": [hinted] + [item for item in learned["ordered"]
                                                            if item != hinted],
                           "origin_hint": hinted}
            planned = {**learned, "reliability": None, "scope": {"operation": operation,
                "adapter": adapter, "adapter_version": version,
                "adapter_binding_id": self.adapters.binding_id(adapter)
                    if adapter else None,
                "identity_class": "public", "acquisition_context": route_context}}
            if candidates:
                policy = replace(policy, provider=None,
                                 provider_candidates=tuple(learned["ordered"]))
            public_route = {"provider_plan": planned}
            if _route_seeds:
                public_route["catalog_seeds"] = list(_route_seeds)
                public_route["provider_plan"]["baseline"] = (
                    "typed provisional compatibility seed order")
            if learned["preferred"] is not None:
                # Compatibility aliases retain their meaning while the complete
                # evidence-bearing plan remains the authoritative receipt field.
                public_route.update(memory_provider=learned["preferred"],
                                    memory_basis=learned["basis"])
        return policy, route_context, public_route

    def _parse_acquired(self, response, adapter, url, policy, receipt, resolved,
                        identity_capture, _workload_assertions):
        """PDF text, adapter projection, workload assertions and repair overlay."""
        repair_overlay = None
        if ("pdf" in (response.get("content_type") or "").lower()
                and isinstance(response.get("raw"), (bytes, bytearray))
                and response["raw"][:5] == b"%PDF-"):
            # Raw PDF bytes are not text; stitch in pypdf for that.
            response["content"] = _pdf_text(bytes(response["raw"]), policy.pdf_max_pages)
        try:
            parsed = parse_content(
                response["content"],
                response["content_type"],
                response["url"], adapter, policy=policy,
                requested_url=url,
                acquisition_attestation=copy.deepcopy(
                    identity_capture),
                adapter_registry=self.adapters,
                **({"navigation_data":
                    response["navigation_data"]}
                   if response.get("navigation_data")
                   is not None else {}),
                **({"navigation": response["navigation"]}
                   if response.get("navigation")
                   is not None else {}))
            if _workload_assertions is not None:
                from .repair import enforce_workload_assertions
                assertion_validation = (
                    enforce_workload_assertions(
                        parsed.get("structured"),
                        _workload_assertions))
        except WebFailure as extraction_failure:
            if (extraction_failure.code != "SCHEMA_CHANGED"
                    or adapter is None):
                raise
            from .repair import active_overlay_projection
            overlay_result = active_overlay_projection(
                self.state_dir, adapter,
                receipt.get("adapter_version"),
                receipt.get("adapter_binding_id"),
                url, response["content"],
                _workload_assertions,
                identity_class=("named" if resolved
                                else "public"),
                max_registry_bytes=(
                    policy.repair_overlay_registry_max_bytes))
            if overlay_result is None:
                if hasattr(extraction_failure,
                           "workload_assertions"):
                    receipt["workload_assertions"] = (
                        extraction_failure.workload_assertions)
                raise
            parsed, repair_overlay, assertion_validation = (
                overlay_result)
        if _workload_assertions is not None:
            receipt["workload_assertions"] = (
                assertion_validation)
        if repair_overlay is not None:
            receipt["repair_overlay"] = repair_overlay
        return parsed

    def _record_observation(self, result, receipt, response, parsed, policy, identity_capture):
        """Copy an accepted response into the result; return the evidence to commit."""
        result.update({k:v for k,v in response.items()
            if k not in {"raw", "screenshot", "http_status",
                         "url", "navigation_data",
                         "identity_private_image_sources",
                         "visible_snapshot"}})
        result["url"] = _output_url(response["url"], named_identity=bool(policy.identity))
        result.update(parsed)
        evidence = self._save_bytes(response["raw"], ".json" if
            "json" in response["content_type"] else ".html")
        pending_receipt_evidence = [*receipt["evidence"], evidence]
        if identity_capture is not None:
            receipt["visible_snapshot"] = identity_capture
            receipt.update(freshness_seconds=None,capture_freshness_seconds=0,
                source_freshness="unknown",source_refresh_performed=False,
                requested_freshness_satisfied=False,semantic_freshness="partial_owner_visible_evidence")
        if response.get("content_readiness"):
            receipt["content_readiness"] = response["content_readiness"]
        if response.get("interactions") is not None:
            receipt["interactions"] = response["interactions"]
        if response.get("archived"):
            # A stored copy: say so wherever the agent looks.
            receipt["archived"] = response["archived"]
            receipt["source_freshness"] = "archived"
        if response.get("navigation"):
            receipt["navigation"] = response["navigation"]
        if response.get("navigation_data"):
            data = response["navigation_data"]
            receipt["navigation_data_source"] = {"url":data["url"],"http_status":data["http_status"]}
            if data.get("method"): receipt["navigation_data_source"]["method"] = data["method"]
            pending_receipt_evidence.append(
                self._save_bytes(
                    data["content"].encode(), ".json"))
        if response.get("screenshot"):
            pending_receipt_evidence.append(
                response["screenshot"])
        pending_receipt_evidence.extend(
            response.get("acquisition_evidence", []))
        result["field_status"]["content"] = "observed"
        return pending_receipt_evidence

    def _record_failed_attempt(self, exc, response, attempt_final_url, adapter, policy,
                               candidate, candidate_version, candidate_binding_id,
                               attempt_started, attempt_cost, cost_reported, receipt,
                               attempts, attempt_costs, _recipe, resolved):
        """Append the failed attempt with its cost, children and retained evidence."""
        provider_acquisition = isinstance(response, dict)
        if isinstance(exc, WebFailure):
            if provider_acquisition:
                exc.response_url = attempt_final_url
                exc.http_status = response.get("http_status")
                if adapter is not None:
                    exc.failure_stage = None
                    exc._public_failure_evidence = []
            elif (exc.response_url is None and attempt_final_url
                  and not exc.code.startswith("IDENTITY_")):
                exc.response_url = attempt_final_url
        from .providers import _validated_provider_attempt_tree
        safe_children = None
        malformed_children = False
        if provider_acquisition:
            raw_children = response.get("provider_attempts")
        else:
            raw_children = (exc.__dict__.get(
                "provider_attempts")
                if isinstance(exc, WebFailure) else None)
        if raw_children is not None:
            try:
                safe_children = _validated_provider_attempt_tree(
                    raw_children,
                    max_depth=policy.provider_composition_max_depth,
                    max_nodes=policy.provider_composition_max_attempts,
                    max_bytes=policy.max_bytes)
            except (ValueError, TypeError, OverflowError,
                    RecursionError):
                malformed_children = True
        if malformed_children and isinstance(exc, WebFailure):
            exc.code = "PROVIDER_DOWN"
            exc.message = (
                "Invalid composite provider attempt envelope")
        attempt = {"provider":candidate,
            "provider_version":candidate_version,
            **({"provider_binding_id": candidate_binding_id}
               if candidate_binding_id is not None else {}),
            "status":"failed","failure":exc.code,
            "latency_ms":round((time.monotonic()-attempt_started)*1000)}
        if (not provider_acquisition
                and hasattr(exc, "cost_usd")):
            attempt_cost, cost_reported = _measured_cost(
                exc.cost_usd), True
            receipt["cost_basis"] = (
                "provider reported; excludes hardware")
        if cost_reported:
            attempt["cost_usd"] = attempt_cost
        attempt_costs.append(attempt_cost)
        receipt["cost_usd"] = _total_cost(attempt_costs)
        if _recipe: attempt["route_recipe"] = _recipe
        if getattr(exc, "failure_stage", None):
            attempt["failure_stage"] = exc.failure_stage
        if safe_children is not None:
            attempt["children"] = safe_children
        if getattr(exc, "response_url", None) and not exc.code.startswith("IDENTITY_"):
            attempt["final_url"] = _output_url(exc.response_url, named_identity=bool(policy.identity))
        if (policy.retain_public_failure_evidence and not resolved
                and not exc.code.startswith("IDENTITY_") and response is not None
                and attempt_final_url is not None):
            # Acquisition is evidence, even when semantic extraction fails.
            # Keep it local and attached to this failed attempt, never cached.
            artifact = self._save_bytes(
                response["raw"], ".json" if "json" in
                response["content_type"] else ".html")
            attempt["evidence"] = [artifact]
        if (policy.retain_public_failure_evidence
                and not provider_acquisition and not resolved
                and not exc.code.startswith("IDENTITY_")):
            retained = getattr(exc, "_public_failure_evidence", [])
            if retained:
                attempt.setdefault("evidence", []).extend(retained)
        attempts.append(attempt)

    def _after_failed_attempt(self, exc, policy, candidate, candidate_record, retry_index,
                              plan_state, order, resolved, receipt):
        """Decide what follows a failed attempt: "retry", "next" or "stop".

        Paid providers and cost-capped reads are never retried, since a retry is
        a second charge. After enough failed tries, free tools that fetch from
        their own network move ahead; after enough walls, allowed paid providers
        move ahead of those.
        """
        if exc.code in policy.provider_retry_failures:
            plan_state.setdefault("failed_alike", set()).add(candidate)
        if (exc.code in policy.provider_retry_failures
                and exc.code not in policy.terminal_failures
                and not (candidate_record or {}).get("paid")
                and policy.max_cost_usd is None
                # Retries are for a flaky tool. Once two different tools on an
                # automatic route failed the same way here, the site is the
                # likelier cause: each remaining tool still gets one try, not two.
                and not (plan_state.get("automatic") and len(plan_state["failed_alike"]) >= 2)
                and retry_index + 1 < policy.provider_max_attempts_per_candidate):
            return "retry"
        # Anonymous reads meet fake walls: sites answer bots with a sign-in
        # redirect or a 404 that a real browser never sees. So a public read
        # confirms them: a 404 from a plain fetch, or after another wall, gets
        # one more provider, and sign-in walls stop the climb only after
        # policy.auth_wall_confirmations of them in a row.
        anonymous = not resolved and not policy.profile
        terminal = exc.code in policy.terminal_failures
        suspect = False
        if exc.code == "RATE_LIMITED":
            # A bot checkpoint often answers 429 (Vercel, some CDNs); a 404 after it
            # is as suspect as one after a wall.
            plan_state["refused"] = True
        if exc.code == "NOT_FOUND" and anonymous:
            plan_state["not_found"] = plan_state.get("not_found", 0) + 1
            if plan_state["walls"] or (plan_state.get("refused") and plan_state["not_found"] == 1) or (
                    plan_state["not_found"] == 1 and candidate in ("http", "scrapling_http")):
                terminal, suspect = False, True
        if exc.code in ("AUTH_REQUIRED", "AUTH_EXPIRED") and anonymous:
            plan_state["auth_walls"] = plan_state.get("auth_walls", 0) + 1
            plan_state["walls"] += 1
            terminal = plan_state["auth_walls"] >= policy.auth_wall_confirmations
            suspect = not terminal
        if suspect and not plan_state.get("reordered"):
            # The same kind of fetcher would see the same fake wall, so the
            # confirming reads come from fetchers that look different.
            remaining = order[plan_state["position"] + 1:]
            first = [item for item in policy.fake_wall_confirmers if item in remaining]
            order[plan_state["position"] + 1:] = first + [i for i in remaining if i not in first]
            plan_state["reordered"] = True
        if exc.code in policy.escalation_failures:
            plan_state["walls"] += 1
        plan_state["misses"] = plan_state.get("misses", 0) + 1
        if policy.escalate_after_walls and not resolved:
            manifests = {item["id"]: item for item in self.providers.inspect()}
            hopeless = plan_state.get("hopeless") or set()

            def move_ahead(ahead):
                remaining = order[plan_state["position"] + 1:]
                order[plan_state["position"] + 1:] = ahead + [
                    item for item in remaining if item not in ahead]
                receipt.setdefault("routing", {})["escalated_to"] = ahead[0]
            if (not plan_state.get("elsewhere")
                    and plan_state["misses"] >= policy.escalate_after_walls):
                # This many tries on this machine got no page (a wall that blocks
                # by address stops them all alike, and some pages none of them
                # render): a free tool that fetches from elsewhere is the next chance.
                elsewhere = [item for item in order[plan_state["position"] + 1:]
                             if item not in hopeless and manifests.get(item, {}).get("remote")
                             and not manifests[item].get("paid") and not manifests[item].get("archive")]
                if elsewhere:
                    move_ahead(elsewhere)
                    plan_state["elsewhere"] = True
            remaining_paid = [item for item in order[plan_state["position"] + 1:]
                              if manifests.get(item, {}).get("paid") and item not in hopeless]
            paid_threshold = ((policy.paid_after_walls + (1 if plan_state.get("free_favourite") else 0))
                              if remaining_paid and policy.paid_after_walls else policy.escalate_after_walls)
            if (not plan_state["escalated"] and paid_threshold
                    and plan_state["walls"] >= paid_threshold):
                # The owner's real browser first (free, and walls rarely tell it
                # from a person), then allowed paid tools, cheapest likely success first.
                ahead = [item for item in order[plan_state["position"] + 1:]
                         if item == "real_browser" and item not in hopeless]
                ahead += self._rank_paid(remaining_paid, plan_state.get("vendor"), policy)
                if ahead:
                    move_ahead(ahead)
                    plan_state["escalated"] = True
        return "stop" if (resolved or terminal or candidate == order[-1]) else "next"

    def _rank_paid(self, tools, vendor, policy):
        """Paid tools, cheapest likely success first: measured cost per read over the
        chance it gets past this wall vendor (or any wall, when none is named).
        Tools with no known cost follow those with one."""
        stats, _ = self._vendor_stats(vendor, policy) if vendor else ({}, {})
        facts = _paid_priors() if policy.use_wall_priors else {}

        def key(index_tool):
            index, tool = index_tool
            wins, tries = stats.get(tool) or (facts.get(tool) or {}).get("overall") or [0, 0]
            rate = (wins + 1) / (tries + 2)
            cost = (facts.get(tool) or {}).get("cost_usd")
            return (cost is None, (cost / rate) if cost is not None else -rate, index)
        return [tool for _index, tool in sorted(enumerate(tools), key=key)]

    def _wall_stats_path(self):
        return self.state_dir / "wall-stats.json"

    def _wall_stats(self):
        try:
            return json.loads(self._wall_stats_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _vendor_stats(self, vendor, policy, fade=8):
        """What got past this wall vendor: this machine's own reads, plus the bundled
        priors when the policy uses them. A prior counts less with every local try
        and nothing after `fade` of them, so local evidence replaces it."""
        local = self._wall_stats().get(vendor) or {}
        priors = (_wall_priors().get(vendor) or {}) if policy.use_wall_priors else {}
        stats = {}
        for tool in set(local) | set(priors):
            wins, tries = local.get(tool, [0, 0])
            prior_wins, prior_tries = priors.get(tool, [0, 0])
            weight = max(0, fade - tries) / fade
            stats[tool] = [wins + prior_wins * weight, tries + prior_tries * weight]
        return stats, local

    def _route_by_vendor(self, vendor, order, plan_state, receipt, policy, minimum=3, hopeless=8):
        """Move the free tools that got past this wall vendor most often to the front
        of what is left, and the tools that never have in `hopeless` tries to the
        back. Paid tools are placed by the paid escalation (paid_after_walls,
        cheapest likely success first), never here. Learned from this machine's own
        reads and measured priors, never from site names."""
        stats, local = self._vendor_stats(vendor, policy)
        remaining = order[plan_state["position"] + 1:]
        paid = {item["id"] for item in self.providers.inspect() if item["paid"]}

        def plan(counts):
            rated = sorted(((tool, (counts[tool][0] + 1) / (counts[tool][1] + 2)) for tool in remaining
                            if tool in counts and counts[tool][1] >= minimum and tool not in paid),
                           key=lambda pair: -pair[1])
            return ([tool for tool, rate in rated if rate >= 0.5],
                    [tool for tool in remaining if tool in counts and counts[tool][0] == 0
                     and counts[tool][1] >= hopeless])
        ahead, last = plan(stats)
        plan_state["hopeless"] = set(last)
        # A free tool that usually beats this vendor gets its try before money is spent.
        plan_state["free_favourite"] = bool(ahead)
        if ahead or last:
            order[plan_state["position"] + 1:] = ahead + [
                tool for tool in remaining if tool not in ahead and tool not in last] + last
            learned = {"vendor": vendor}
            if ahead:
                learned["first"] = ahead[0]
            if last:
                learned["last"] = last
            if (ahead, last) != plan(local):
                learned["priors"] = True  # The bundled priors changed this order.
            receipt.setdefault("routing", {})["learned_for"] = learned

    def _learn_wall(self, receipt):
        """After a read that met a named wall: which tools tried, which got through."""
        vendor = receipt.get("wall_vendor")
        if vendor not in WALL_VENDORS:
            return
        attempts = [item for item in receipt.get("attempts") or [] if isinstance(item, dict)]
        winner = receipt.get("method") if receipt.get("status") == "observed" else None
        stats = self._wall_stats()
        tools = stats.setdefault(vendor, {})
        for tool in dict.fromkeys(item.get("provider") for item in attempts[1:] if item.get("provider")):
            wins, tries = tools.get(tool, [0, 0])
            tools[tool] = [wins + (1 if tool == winner else 0), tries + 1]
        path = self._wall_stats_path()
        fd, temporary = tempfile.mkstemp(prefix=".walls-", dir=self.state_dir)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(stats, stream)
        os.replace(temporary, path)

    def _store_cache(self, result, receipt, url, adapter, policy, resolved, key,
                     _route_scope, route_seed_scope, identity_capture, identity_authority):
        """Cache an observed public (or verified identity) result for later freshness levels."""
        if (receipt["status"] == "observed"
                and not (resolved and (identity_capture is not None
                    or getattr(resolved, "snapshot_policy", None)))):
            if resolved:
                self._identity_provider_authority(
                    resolved, policy, expected=identity_authority)
            if self._cache_key(url, adapter, policy, resolved,
                    route_scope=_route_scope,
                    route_seed_scope=route_seed_scope) != key:
                raise WebFailure(
                    "IDENTITY_PROVIDER_DENIED" if resolved
                        else "PROVIDER_UNAVAILABLE",
                    "Acquisition configuration changed before cache storage")
            for freshness in ("hour","day","cached"):
                cache_policy = replace(policy,freshness=freshness)
                cache_path = self.state_dir/"cache"/(self._cache_key(
                    url, adapter, cache_policy, resolved,
                    route_scope=_route_scope,
                    route_seed_scope=route_seed_scope)+".json")
                cache_path.write_text(json.dumps(result,ensure_ascii=False)); cache_path.chmod(0o600)

    def _bind_identity(self, resolved, policy, adapter, receipt):
        """Pin the identity's provider authority and the adapter version for this read.

        Returns (identity_provider, identity_authority, identity_capture_strategy).
        """
        identity_provider = None
        identity_authority = None
        identity_capture_strategy = None
        if resolved:
            identity_provider = resolved.provider
            identity_capture_strategy = self._identity_capture_strategy(
                resolved)
            if (policy.provider_candidates is not None
                    and policy.provider_candidates != (identity_provider,)):
                raise WebFailure("IDENTITY_PROVIDER_DENIED",
                    "Named identity provider candidates exceed its executor binding")
            # Pin the exact startup provider definition before connecting
            # to the owned browser. Authentication capability alone never
            # grants authority to an alternate catalog entry.
            identity_authority = self._identity_provider_authority(
                resolved, policy,
                operation="extract" if adapter else "read")
            receipt.update(
                provider_version=identity_authority.version,
                provider_binding_id=identity_authority.binding_id)
        if adapter:
            receipt["adapter_version"] = self.adapters.require_enabled(
                adapter).version
            adapter_binding_id = self.adapters.binding_id(adapter)
            if adapter_binding_id is not None:
                receipt["adapter_binding_id"] = adapter_binding_id
        return identity_provider, identity_authority, identity_capture_strategy

    def _read_candidates(self, url, policy, adapter, resolved, identity_provider):
        """Providers to try, in order, after context-stop and navigation checks."""
        block_key = (urlparse(url).netloc,resolved.cache_scope) if resolved else urlparse(url).netloc
        if self._blocked_domains.get(block_key) in policy.context_stop_failures:
            raise WebFailure(self._blocked_domains[block_key], "Context stopped after an earlier access or rate block")
        if policy.navigation_page > 1:
            navigation_provider = (identity_provider if resolved
                                   else policy.provider)
            if (not navigation_provider
                    or not self.providers.require_enabled(
                        navigation_provider,policy).navigation):
                raise WebFailure("POLICY_DENIED", "Numbered navigation requires an explicitly selected navigation-capable provider")
        if resolved: candidates = [identity_provider]
        elif policy.provider: candidates = [policy.provider]
        elif policy.provider_candidates is not None: candidates = list(policy.provider_candidates)
        else:
            candidates = self.providers.candidates(policy,
                configured=("steel",) if self.steel_api_url else (),
                operation="extract" if adapter else "read")
        if not candidates: raise WebFailure("POLICY_DENIED", "No permitted provider")
        return candidates

    def _check_acquisition_binding(self, url, adapter, policy, attempt_policy, resolved, receipt,
                                   key, _route_scope, route_seed_scope, candidate,
                                   candidate_version, candidate_binding_id,
                                   identity_authority, attempt_cost):
        """Refuse a result if the adapter, provider, identity or cache scope changed
        while it was being acquired, or if its cost breaks the remaining cap."""
        if adapter and self.adapters.require_enabled(adapter).version != receipt["adapter_version"]:
            raise WebFailure("SCHEMA_CHANGED", "Adapter configuration changed during acquisition")
        if resolved:
            self._identity_provider_authority(
                resolved, attempt_policy,
                expected=identity_authority)
            if self._cache_key(url, adapter, policy, resolved,
                    route_scope=_route_scope,
                    route_seed_scope=route_seed_scope) != key:
                raise WebFailure("IDENTITY_PROVIDER_DENIED",
                    "Named identity acquisition binding changed during execution")
        elif (self.providers.require_enabled(
                candidate, attempt_policy).version
                != candidate_version
                or self.providers.binding_id(candidate)
                    != candidate_binding_id
                or (adapter is not None
                    and self.adapters.binding_id(adapter)
                        != receipt.get(
                            "adapter_binding_id"))
                or self._cache_key(url, adapter, policy,
                    resolved, route_scope=_route_scope,
                    route_seed_scope=route_seed_scope) != key):
            raise WebFailure("PROVIDER_UNAVAILABLE", "Acquisition configuration changed; retry with its current version")
        if attempt_policy.max_cost_usd is not None and (
                attempt_cost is None or attempt_cost > attempt_policy.max_cost_usd):
            raise WebFailure("BUDGET_EXHAUSTED", "Acquisition cost cannot satisfy the remaining aggregate cap")

    def _observed_attempt(self, response, policy, candidate, candidate_version,
                          candidate_binding_id, attempt_started, _recipe,
                          attempt_cost, cost_reported):
        """The receipt row for a successful attempt, with any composite children."""
        attempt = {"provider":candidate,"provider_version":candidate_version,
            **({"provider_binding_id": candidate_binding_id}
               if candidate_binding_id is not None else {}),
            "status":"observed","latency_ms":round((time.monotonic()-attempt_started)*1000),
            **({"route_recipe": _recipe} if _recipe else {}),
            **({"cost_usd": attempt_cost} if cost_reported else {})}
        if response.get("provider_attempts"):
            # Child costs are included in this one outer cost;
            # keep the tree descriptive instead of flattening it.
            from .providers import _validated_provider_attempt_tree
            try:
                attempt["children"] = (
                    _validated_provider_attempt_tree(
                        response["provider_attempts"],
                        max_depth=policy.provider_composition_max_depth,
                        max_nodes=policy.provider_composition_max_attempts,
                        max_bytes=policy.max_bytes))
            except (ValueError, TypeError, OverflowError,
                    RecursionError):
                raise WebFailure("PROVIDER_DOWN",
                    "Invalid composite provider attempt envelope") from None
        return attempt

    def _prepare_attempt(self, url, policy, adapter, candidate, resolved, context,
                         identity_authority, identity_capture_strategy, prior_costs):
        """Provider manifest, the attempt's policy (longer deadline for unblockers,
        remaining cost cap) and, for an identity read, its one-shot capability.

        Returns (manifest, attempt_policy, identity_capability, identity_binding).
        """
        manifest = self.providers.require_enabled(candidate, policy,
            operation="extract" if adapter else "read")
        from .hosted_providers import AGENT_PROVIDERS, UNBLOCKERS
        if candidate in UNBLOCKERS:
            policy_for_candidate = replace(policy, timeout_seconds=max(
                policy.timeout_seconds, policy.unblocker_timeout_seconds))
        elif candidate == "handoff":
            policy_for_candidate = replace(policy, timeout_seconds=max(
                policy.timeout_seconds, policy.handoff_timeout_seconds))
        elif candidate == "real_browser":
            policy_for_candidate = replace(policy, timeout_seconds=max(
                policy.timeout_seconds, policy.real_browser_wait_seconds + 30))
        elif candidate in AGENT_PROVIDERS:
            policy_for_candidate = replace(policy, timeout_seconds=max(
                policy.timeout_seconds, policy.agent_provider_timeout_seconds))
        else:
            policy_for_candidate = policy
        attempt_policy = _remaining_cost_policy(policy_for_candidate,
            prior_costs, paid=manifest.paid)
        if not resolved:
            return manifest, attempt_policy, None, None
        self.identities.recheck(resolved)
        self._identity_provider_authority(
            resolved, attempt_policy, expected=identity_authority)
        identity_capability = _AuthenticatedProviderCapability(
            identity_authority, resolved.cache_scope, url, attempt_policy,
            capture_strategy=identity_capture_strategy)
        return manifest, attempt_policy, identity_capability, (resolved, context, identity_capability)

    def _verify_identity_response(self, response, resolved, attempt_policy,
                                  identity_capability, identity_authority,
                                  identity_capture_strategy):
        """Check an identity provider used its exact one-shot capability and
        returned Core's own acquisition. Returns (capture, private image sources).

        The two failures that mean the response is not Core's are marked
        `_discard_response` so the caller drops the response before reporting.
        """
        if (identity_capability.state != "consumed"
                or identity_capability.response_fingerprint is None):
            failure = WebFailure("IDENTITY_PROVIDER_DENIED",
                "Authenticated provider did not consume its exact one-shot capability")
            failure._discard_response = True
            raise failure
        self._identity_provider_authority(
            resolved, attempt_policy, expected=identity_authority)
        if identity_capture_strategy is not None:
            self._require_current_capture_strategy(identity_capture_strategy)
        if (_identity_acquisition_fingerprint(
                response, attempt_policy.max_bytes,
                capture_metadata=identity_capability.capture_metadata)
                != identity_capability.response_fingerprint):
            failure = WebFailure("IDENTITY_PROVIDER_DENIED",
                "Authenticated provider replaced the Core acquisition result")
            failure._discard_response = True
            raise failure
        return (copy.deepcopy(identity_capability.capture_metadata),
                copy.deepcopy(identity_capability.private_image_sources or {}))

    async def _read(self, url: str, policy: WebPolicy | None = None, provider: str | None = None,
                    adapter: str | None = None, *, _route_scope=None, _recipe=None,
                    _route_seeds=(), _seed_providers=(), _planning_failure=None,
                    _prior_costs=(),
                    _workload_assertions=None) -> dict:
        policy = policy or WebPolicy()
        if provider is not None: policy = replace(policy, provider=provider)
        policy, route_context, public_route = self._plan_route(
            url, policy, adapter, _route_scope, _route_seeds, _seed_providers)
        trace, started, attempts = uuid.uuid4().hex, time.monotonic(), []
        attempt_costs = []
        result = {"url": None, "title": None, "text": "", "content": "", "content_type": "",
                  "headers": {}, "structured": None, "image_urls": [], "images": [],
                  "field_status": {"availability": "unknown", "transaction_price": "unknown"}}
        from .actions import required_read_action
        action_class = required_read_action(policy.identity)
        receipt = {"trace_id":trace,"status":"failed","operation":"extract" if adapter else "read",
                   "action_class":action_class,
                   "acquisition_context": route_context,
                   "adapter":adapter,"observed_at":None,"freshness_seconds":None,"cache_hit":False,
                   "method":None,"identity":policy.identity,"http_status":None,"failure":None,
                   "requested_url":None,"final_url":None,
                   "confidence":None,"cost_usd":0,"cost_basis":"self-hosted software; excludes electricity/hardware",
                   "evidence":[],"attempts":attempts}
        if public_route: receipt["routing"] = public_route
        if policy.retry_of_trace or policy.exclude_providers:
            receipt["try_harder"] = {"retry_of": policy.retry_of_trace,
                                     "excluded": list(policy.exclude_providers)}
        if _recipe: receipt["route_recipe"] = _recipe
        result["receipt"] = receipt
        try:
            if _planning_failure is not None:
                raise _planning_failure
            from .actions import require_action
            require_action(policy, action_class)
            _validate_url(url)
            if policy.block_private_network or self.block_private_network:
                await _refuse_private(url)
                PRIVATE_GUARD.set(True)
            result["url"] = _output_url(url, named_identity=bool(policy.identity))
            receipt["requested_url"] = result["url"]
            resolved = self.identities.resolve(policy.identity,url,provider=policy.provider,
                allow_local_browser=policy.allow_local_browser) if policy.identity else None
            identity_capture = None
            identity_private_image_sources = {}
            identity_provider, identity_authority, identity_capture_strategy = (
                self._bind_identity(resolved, policy, adapter, receipt))
            with self.identities.lease(resolved) if resolved else nullcontext():
                context = None
                if resolved:
                    context = await self._identity_step(self._identity_context(resolved,policy),policy,probe=True)
                    auth_state = await self._identity_step(
                        self._identity_health(
                            context, resolved, policy,
                            **({"capture_strategy": identity_capture_strategy}
                               if identity_capture_strategy is not None
                               else {})), policy)
                    receipt.update(identity=resolved.id,authority_mode=resolved.authority_mode,
                        executor=resolved.executor_id,profile_version=resolved.profile_version,
                        identity_generation=resolved.generation,executor_generation=resolved.executor_generation,
                        network_context=resolved.network_context,geography=resolved.geography,
                        authentication=auth_state,browser_execution="owner_visible_snapshot" if getattr(resolved, "snapshot_policy", None) else "passive_read")
                    self.identities.recheck(resolved)
                route_seed_scope = ([seed["sha256"] for seed in _route_seeds]
                                    if _route_seeds else None)
                key = self._cache_key(url, adapter, policy, resolved,
                    route_scope=_route_scope,
                    route_seed_scope=route_seed_scope)
                cached = self._cache_lookup(key,policy) if not resolved or (auth_state == "verified" and not getattr(resolved, "snapshot_policy", None)) else None
                if cached:
                    if resolved:
                        self.identities.recheck(resolved)
                        self._identity_provider_authority(
                            resolved, policy, expected=identity_authority)
                    self._save_trace(cached)
                    return cached
                candidates = self._read_candidates(url, policy, adapter, resolved, identity_provider)
                order = list(candidates)
                plan_state = {"position": 0, "walls": 0, "escalated": False,
                              "automatic": public_route is not None}

                def _ordered_plan():
                    # Reads `order` lazily so escalation can reorder what is left.
                    while plan_state["position"] < len(order):
                        for retry_index in range(
                                policy.provider_max_attempts_per_candidate):
                            yield order[plan_state["position"]], retry_index
                        plan_state["position"] += 1

                provider_plan = _ordered_plan()
                exhausted_candidates = set()
                for candidate, retry_index in provider_plan:
                    if candidate in exhausted_candidates:
                        continue
                    claim = _RACE_CLAIMED.get()
                    if (claim is not None and claim[0] == url and candidate in claim[1]
                            and any(item not in claim[1] for item in order[plan_state["position"] + 1:])):
                        # A racer is reading this page with this tool: leave it to the racer.
                        left = receipt.setdefault("routing", {}).setdefault("left_to_racers", [])
                        if candidate not in left:
                            left.append(candidate)
                        exhausted_candidates.add(candidate)
                        continue
                    attempt_started = time.monotonic()
                    attempt_final_url = None
                    candidate_record = next((item for item in
                        self.providers.inspect() if item["id"] == candidate), None)
                    candidate_version = (identity_authority.version if resolved
                        else (candidate_record.get("version")
                              if candidate_record else None))
                    candidate_binding_id = (identity_authority.binding_id
                        if resolved else self.providers.binding_id(candidate))
                    if candidate_version is not None:
                        receipt["provider_version"] = candidate_version
                    if candidate_binding_id is not None:
                        receipt["provider_binding_id"] = candidate_binding_id
                    response = None
                    attempt_cost = 0
                    cost_reported = False
                    try:
                        from .providers import ProviderRequest
                        (manifest, attempt_policy, identity_capability, identity_binding) = (
                            self._prepare_attempt(
                                url, policy, adapter, candidate, resolved, context,
                                identity_authority, identity_capture_strategy,
                                [*_prior_costs, *attempt_costs]))
                        candidate_version = manifest.version
                        receipt["provider_version"] = candidate_version
                        if manifest.paid:
                            # Once a paid provider begins, absent reporting is
                            # unknown even when its executor is local.
                            attempt_cost, cost_reported = None, True
                            receipt["cost_basis"] = "provider reported; excludes hardware"
                        response=await self.providers.acquire(
                            candidate,
                            ProviderRequest(
                                url, attempt_policy,
                                "extract" if adapter else "read",
                                plan_state.get("pdf_bytes")),
                            self._provider_services(
                                identity_binding=identity_binding),
                            _evidence_preparer=(
                                self._prepare_provider_failure_evidence))
                        if resolved:
                            try:
                                identity_capture, identity_private_image_sources = (
                                    self._verify_identity_response(
                                        response, resolved, attempt_policy,
                                        identity_capability, identity_authority,
                                        identity_capture_strategy))
                            except WebFailure as failure:
                                if getattr(failure, "_discard_response", False):
                                    response = None
                                raise
                        if manifest.paid or "cost_usd" in response:
                            attempt_cost = _measured_cost(response.get("cost_usd"))
                            cost_reported = True
                            receipt["cost_basis"]="provider reported; excludes hardware"
                        content_readiness = _validated_content_readiness(
                            response.get("content_readiness"), attempt_policy)
                        if content_readiness is not None:
                            response["content_readiness"] = content_readiness
                        self._check_acquisition_binding(
                            url, adapter, policy, attempt_policy, resolved, receipt,
                            key, _route_scope, route_seed_scope, candidate,
                            candidate_version, candidate_binding_id,
                            identity_authority, attempt_cost)
                        _validate_url(response["url"])
                        if PRIVATE_GUARD.get():
                            # A browser that followed a redirect inside.
                            await _refuse_private(response["url"])
                        attempt_final_url = response["url"]
                        parsed = self._parse_acquired(
                            response, adapter, url, policy, receipt, resolved,
                            identity_capture, _workload_assertions)
                        if resolved:
                            parsed["image_urls"] = list(dict.fromkeys(
                                _output_url(image_url,
                                    named_identity=True)
                                for image_url in parsed["image_urls"]
                                if _output_url(image_url,
                                    named_identity=True) is not None))
                        _reject_unusable_page(candidate, candidate_record, policy, resolved,
                                              adapter, response, parsed, url)
                        pending_receipt_evidence = self._record_observation(
                            result, receipt, response, parsed, policy, identity_capture)
                        if policy.card_images and "html" in (response.get("content_type") or ""):
                            from .completeness import cards
                            found = cards(response.get("content") or "", response.get("url") or url,
                                          structured=parsed.get("structured"))
                            if resolved:
                                # Signed-in pages: strip private tokens like other output URLs.
                                found = [{**card,
                                          "url": _output_url(card["url"], named_identity=True),
                                          "image": card["image"] and _output_url(card["image"],
                                                                                 named_identity=True)}
                                         for card in found]
                                found = [card for card in found if card["url"]]
                            result["cards"] = found
                        if policy.include_images:
                            if resolved:
                                result["images"] = await self._download_identity_images(
                                    parsed["image_urls"], policy, resolved,
                                    context,
                                    private_sources=(
                                        identity_private_image_sources))
                            else:
                                result["images"] = await self.download_images(
                                    parsed["image_urls"], policy)
                        if resolved:
                            self.identities.recheck(resolved)
                            self._identity_provider_authority(
                                resolved, attempt_policy,
                                expected=identity_authority)
                            if identity_capture_strategy is not None:
                                self._require_current_capture_strategy(
                                    identity_capture_strategy)
                        attempt = self._observed_attempt(
                            response, policy, candidate, candidate_version,
                            candidate_binding_id, attempt_started, _recipe,
                            attempt_cost, cost_reported)
                        from .egress import CURRENT as EGRESS
                        if self.proxy and candidate in _PROXIED and EGRESS.get() is None:
                            receipt["via_proxy"] = True
                        receipt.update(
                            status="observed", observed_at=utcnow(),
                            **({"freshness_seconds": 0}
                               if identity_capture is None else {}),
                            method=candidate,
                            http_status=response["http_status"],
                            final_url=_output_url(
                                attempt_final_url,
                                named_identity=bool(policy.identity)),
                            evidence=pending_receipt_evidence)
                        attempts.append(attempt)
                        attempt_costs.append(attempt_cost)
                        receipt["cost_usd"] = _total_cost(attempt_costs)
                        break
                    except (WebFailure,IdentityFailure) as exc:
                        receipt["method"] = candidate
                        # Any completed acquisition response is authoritative
                        # at the provider boundary, including named-identity
                        # responses. An adapter may classify content but cannot
                        # forge acquisition provenance, stage, evidence, or
                        # accounting. Core parser stages remain useful recovery
                        # diagnostics when no adapter plugin was invoked.
                        self._record_failed_attempt(
                            exc, response, attempt_final_url, adapter, policy,
                            candidate, candidate_version, candidate_binding_id,
                            attempt_started, attempt_cost, cost_reported, receipt,
                            attempts, attempt_costs, _recipe, resolved)
                        if isinstance(response, dict) and "pdf" in (
                                response.get("content_type") or "").lower():
                            # Paid tools bill PDFs per page; remember how big this one is.
                            plan_state["pdf_bytes"] = len(response.get("raw") or b"")
                        vendor = getattr(exc, "wall_vendor", None)
                        if vendor and not plan_state.get("vendor") and not resolved:
                            plan_state["vendor"] = vendor
                            receipt["wall_vendor"] = vendor
                            self._route_by_vendor(vendor, order, plan_state, receipt, policy)
                        step = self._after_failed_attempt(
                            exc, policy, candidate, candidate_record, retry_index,
                            plan_state, order, resolved, receipt)
                        if exc.code not in _TOOL_OUTAGES:
                            plan_state["telling"] = exc
                        if step == "retry":
                            if policy.provider_retry_delay_seconds:
                                await asyncio.sleep(
                                    policy.provider_retry_delay_seconds)
                            continue
                        exhausted_candidates.add(candidate)
                        if step == "stop":
                            if exc.code in _TOOL_OUTAGES and plan_state.get("telling") is not None:
                                # The last tool being down says nothing about the page;
                                # what the last working tool met (a wall, a 404) does.
                                raise plan_state["telling"]
                            raise
                receipt["latency_ms"] = round((time.monotonic()-started)*1000)
                self._store_cache(result, receipt, url, adapter, policy, resolved, key,
                                  _route_scope, route_seed_scope, identity_capture,
                                  identity_authority)
        except (WebFailure,IdentityFailure) as exc:
            # Stopping the whole site protects a signed-in account; a public
            # read's sign-in wall may be a bot redirect, so it stops nothing.
            if exc.code in policy.context_stop_failures and (
                    exc.code not in ("AUTH_REQUIRED", "AUTH_EXPIRED")
                    or ('resolved' in locals() and resolved) or policy.profile):
                block_key = (urlparse(url).netloc,resolved.cache_scope) if 'resolved' in locals() and resolved else urlparse(url).netloc
                self._blocked_domains[block_key] = exc.code
            # A policy/generation failure after acquisition must not return private payload.
            if exc.code.startswith("IDENTITY_"):
                for field,value in {"title":None,"text":"","content":"","structured":None,"images":[],"image_urls":[]}.items(): result[field]=value
                result.pop("cards", None)
                receipt["evidence"] = []
            if getattr(exc, "response_url", None) and not exc.code.startswith("IDENTITY_"):
                result["url"] = _output_url(exc.response_url, named_identity=bool(policy.identity))
                receipt["final_url"] = result["url"]
            if getattr(exc, "failure_stage", None):
                receipt["failure_stage"] = exc.failure_stage
            receipt.update(status="failed",failure={"code":exc.code,"message":exc.message},
                http_status=getattr(exc,"http_status",None),observed_at=utcnow(),
                freshness_seconds=0,
                latency_ms=round((time.monotonic()-started)*1000))
        if policy.max_cost_usd is not None:
            aggregate_cost = _total_cost([*_prior_costs, receipt["cost_usd"]])
            receipt.update(max_cost_usd=policy.max_cost_usd,
                cost_budget_satisfied=aggregate_cost is not None and aggregate_cost <= policy.max_cost_usd)
        if receipt["status"] == "failed":
            result["_repair_input"] = {
                "policy": asdict(policy),
                "workload_assertions": copy.deepcopy(
                    _workload_assertions),
            }
        self._save_trace(result)
        return result

    def _annotate_trace(self, receipt, key):
        """Copy one receipt field into the read's saved trace (it was added after saving)."""
        trace_id = receipt.get("trace_id")
        if not isinstance(trace_id, str) or re.fullmatch(r"[0-9a-f]{32}", trace_id) is None:
            return
        from .repair import _read_private_bytes, _write_private_bytes
        try:
            trace = json.loads(_read_private_bytes(self.state_dir, ("traces", trace_id + ".json"),
                                                   64 * 1024 * 1024))
            trace["receipt"][key] = copy.deepcopy(receipt[key])
            _write_private_bytes(self.state_dir, ("traces", trace_id + ".json"),
                                 json.dumps(trace, ensure_ascii=False, indent=2).encode())
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def _save_trace(self, result, *, record_observations=True):
        # Raw content remains in private evidence files; trace output keeps normalized metadata.
        repair_context = result.pop("_repair_input", None)
        receipt = result["receipt"]
        trace_id = receipt.get("trace_id")
        if (not isinstance(trace_id, str)
                or re.fullmatch(r"[0-9a-f]{32}", trace_id) is None):
            trace_id = uuid.uuid4().hex
            receipt["trace_id"] = trace_id
        _redact_result_urls(result)  # Also covers cached records from earlier versions.
        if result["receipt"].get("status") == "failed":
            from .recovery import recovery_handoff
            repair_input = None
            if repair_context is not None:
                from .repair import store_repair_input
                repair_input = store_repair_input(
                    self.state_dir, trace_id, repair_context, receipt)
            elif isinstance(receipt.get("recovery"), dict):
                repair_input = receipt["recovery"].get("repair_input")
            result["receipt"]["recovery"] = recovery_handoff(
                result["receipt"], repair_input=repair_input)
        if record_observations:
            from .route_memory import record_observations as record_routing
            record_routing(self.state_dir, result)
        trace = copy.deepcopy(result)
        trace.pop("content", None)
        trace.pop("text", None)
        if result["receipt"].get("identity"):
            trace.pop("structured", None)
        from .repair import _write_private_bytes
        _write_private_bytes(
            self.state_dir, ("traces", trace_id + ".json"),
            json.dumps(trace, ensure_ascii=False, indent=2).encode())

    def _search_stats_path(self):
        return self.state_dir / "search-stats.json"

    def _search_stats(self):
        """{query type: {source: [passed the checks, tried, cost so far]}}."""
        try:
            return json.loads(self._search_stats_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _learn_search(self, qtype, tried):
        """After a search: which sources were tried for this kind of query, and which of
        them passed the checks on their own. tried is [(source, passed, cost_usd)]."""
        try:
            stats = self._search_stats()
            sources = stats.setdefault(qtype, {})
            for name, passed, cost in tried:
                wins, tries, spent = (list(sources.get(name) or []) + [0, 0, 0])[:3]
                sources[name] = [wins + (1 if passed else 0), tries + 1,
                                 round(spent + (cost if isinstance(cost, (int, float)) else 0), 6)]
            fd, temporary = tempfile.mkstemp(prefix=".search-stats-", dir=self.state_dir)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(stats, stream)
            os.replace(temporary, self._search_stats_path())
        except (OSError, ValueError, TypeError):
            pass  # A hint only: losing it costs one slower search.

    def _search_order(self, candidates, qtype, policy, source, minimum=2, hopeless=6):
        """The ladder's order: free sources first, the one that most often passed the
        checks for this kind of query leading and those that never did last; paid
        sources (cheapest likely success first) once search_paid_after_weak free ones
        are behind. An explicit source or candidate route keeps its own order.
        Learned from this machine's own searches, never from site names."""
        if source is not None or policy.search_source_candidates is not None:
            return list(candidates), {}
        paid_ids = {item["id"] for item in self.searches.inspect() if item["paid"]}
        free = [item for item in candidates if item not in paid_ids]
        paid = [item for item in candidates if item in paid_ids]
        stats = self._search_stats().get(qtype) or {}
        notes = {}
        if policy.search_learned_routing and len(free) > 1:
            pinned = [item for item in free if item in policy.search_source_prefer]
            rest = [item for item in free if item not in pinned]

            def counts(name):
                return (list(stats.get(name) or []) + [0, 0])[:2]
            rated = sorted(((name, (counts(name)[0] + 1) / (counts(name)[1] + 2)) for name in rest
                            if counts(name)[1] >= minimum), key=lambda pair: -pair[1])
            ahead = [name for name, rate in rated if rate >= 0.5]
            last = [name for name in rest if counts(name)[0] == 0 and counts(name)[1] >= hopeless]
            free = pinned + ahead + [name for name in rest if name not in ahead and name not in last] + last
            if ahead:
                notes["learned_first"] = ahead[0]
            if last:
                notes["learned_last"] = last
        if paid:
            paid = self._rank_search_paid(paid, stats, policy)
            notes["paid_order"] = paid
        after = policy.search_paid_after_weak or len(free)
        return free[:after] + paid + free[after:], notes

    def _rank_search_paid(self, sources, stats, policy):
        """Paid search sources, cheapest likely success first: the bundled or measured
        cost per search over the chance it passes the checks for this kind of query.
        Sources with no known cost follow those with one."""
        facts = _paid_priors() if policy.use_wall_priors else {}
        manifests = {item["id"]: item for item in self.searches.inspect()}

        def key(index_source):
            index, name = index_source
            wins, tries, *spent = (list(stats.get(name) or []) + [0, 0])
            rate = (wins + 1) / (tries + 2)
            fact = next((facts[item] for item in (name, manifests.get(name, {}).get("transport_provider"),
                                                  name.removesuffix("_search")) if item in facts), {})
            cost = fact.get("cost_usd")
            if cost is None and tries and spent and spent[0]:
                cost = spent[0] / tries
            return (cost is None, (cost / rate) if cost is not None else -rate, index)
        return [name for _index, name in sorted(enumerate(sources), key=key)]

    async def search(self, query: str, source: str | None = None, limit: int = 10,
                     engine_config: dict | None = None, policy: WebPolicy | None = None, *,
                     site: str | None = None, exclude_domains: list[str] | None = None,
                     recency: str | None = None, region: str | None = None,
                     vertical: str = "web", mode: str = "fallback") -> dict:
        """Search through installed sources; an explicit source never falls back.

        site, exclude_domains, recency (day, week, month, year) and region
        (en-AU) go to every source, natively where it can, and every result
        list is filtered afterwards. vertical picks the kind of source for
        automatic fallback: web, news, reference, discussions, qa, code,
        papers or books.

        mode="merge" asks every eligible source in the vertical at once (up
        to search_merge_sources) and fuses their lists by reciprocal rank:
        better recall, and one blocked engine costs nothing."""
        if mode not in ("fallback", "merge"):
            raise ValueError("mode is fallback or merge")
        if mode == "merge" and source is None:
            return await self._search_merged(query, limit, engine_config, policy, site=site,
                                             exclude_domains=exclude_domains, recency=recency,
                                             region=region, vertical=vertical)
        from .search import MAX_EXCLUDE_OPERATORS, VERTICALS, filter_results, normalize_options
        from .search_checks import accepted, annotate, check_results, merge_results
        from .search_plugins import SearchRequest, SearchServices
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise ValueError("Invalid search query")
        if not 1 <= limit <= 100:
            raise ValueError("Search limit must be 1..100")
        if vertical not in VERTICALS:
            raise ValueError("vertical must be one of: " + ", ".join(VERTICALS))
        options = normalize_options(site, exclude_domains, recency, region)
        if source is not None and not self.searches.contains(source):
            raise ValueError("Unknown search source")
        policy = policy or WebPolicy()
        # Ask for a few more when results will be filtered, then trim; a long exclude
        # list is mostly enforced on the results, so ask for more still.
        fetch_limit = min(100, limit + 10) if options else limit
        if len(options.get("exclude_domains", ())) > policy.search_exclude_operators:
            fetch_limit = min(100, 2 * limit + 10)
        started = time.monotonic()
        from .actions import require_action
        try:
            require_action(policy, "READ_PUBLIC")
            candidates = self.searches.candidates(policy, explicit=source, vertical=vertical)
        except WebFailure as exc:
            candidates, planning_failure = [], exc
        else:
            planning_failure = None
        if not candidates and planning_failure is None:
            planning_failure = WebFailure("POLICY_DENIED", "No permitted search source")

        def acquisition_can_charge(candidate_policy):
            manifests = {item["id"]: item for item in self.providers.inspect()}
            if candidate_policy.provider:
                return bool(manifests.get(candidate_policy.provider, {}).get("paid"))
            if candidate_policy.provider_candidates is not None:
                return any(manifests.get(item, {}).get("paid")
                           for item in candidate_policy.provider_candidates)
            configured = ("steel",) if self.steel_api_url else ()
            return any(manifests[item]["paid"] for item in self.providers.candidates(
                candidate_policy, configured=configured, operation="read"))

        search_attempts, provider_attempts, costs = [], [], []
        last_bundle = None
        last_bundle_binding_id = None
        services = SearchServices(read=self.read)
        # The ladder climbs only on an automatic search; an explicit source is asked once.
        ladder = source is None and policy.search_ladder
        qtype = _search_query_type(vertical, options)
        order, order_notes = self._search_order(candidates, qtype, policy, source)
        paid_ids = {item["id"] for item in self.searches.inspect() if item["paid"]}
        now = datetime.now(timezone.utc)
        engine_extra = ({"max_exclude_operators": policy.search_exclude_operators}
                        if options.get("exclude_domains")
                        and policy.search_exclude_operators != MAX_EXCLUDE_OPERATORS else {})

        async def run_rung(identifier):
            """One search source asked once: its bundle, or the failure that ended it."""
            source_started = time.monotonic()
            manifest = self.searches.require_enabled(identifier, policy)
            search_binding_id = self.searches.binding_id(identifier)
            outcome = {"id": identifier, "manifest": manifest, "binding": search_binding_id}
            try:
                source_policy = policy
                if manifest.transport_provider is not None:
                    # A source's transport is not a routing choice: always read it through
                    # its own, whatever read routing (provider_candidates, --paid) says.
                    source_policy = replace(policy, provider=manifest.transport_provider,
                                            provider_candidates=None)
                attempt_policy = _remaining_cost_policy(source_policy, costs,
                    paid=manifest.paid or acquisition_can_charge(source_policy))
                if identifier != order[-1]:
                    # Another search source is left: fall back instead of retrying.
                    attempt_policy = replace(attempt_policy,
                        provider_max_attempts_per_candidate=1)
                if policy.search_source_timeout_seconds is not None:
                    source_timeout = min(attempt_policy.timeout_seconds,
                                         policy.search_source_timeout_seconds)
                    attempt_policy = replace(attempt_policy,
                        timeout_seconds=source_timeout,
                        content_ready_timeout_seconds=min(
                            attempt_policy.content_ready_timeout_seconds,
                            source_timeout))
                config = {**((engine_config or {}) if source is not None or identifier == "searxng" else {}),
                          **options, **engine_extra} or None
                bundle, manifest = await self.searches.search(identifier,
                    SearchRequest(query, fetch_limit, attempt_policy, config), services)
            except ValueError:
                raise
            except WebFailure as exc:
                outcome.update(manifest=manifest, failure=exc)
            else:
                outcome.update(manifest=manifest, bundle=bundle)
            outcome["latency_ms"] = round((time.monotonic() - source_started) * 1000)
            return outcome

        rungs = {}              # source -> {"report", "kept", "dropped"}, for sources that answered
        bundles, bindings = {}, {}
        winner = stopped = None
        hedged, best, best_count = [], None, -1
        queue = list(order[:policy.search_max_sources] if ladder else order)
        deadline = started + policy.search_max_seconds
        running, leftover = {}, []

        def launch():
            identifier = queue.pop(0)
            running[asyncio.ensure_future(run_rung(identifier))] = identifier

        try:
            while queue or running:
                if not running:
                    launch()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    stopped = "time_cap"
                    break
                # Smart lightning for search: a free source with no answer yet gets the
                # next free one started beside it. Paid sources only ever run alone.
                hedge = bool(ladder and policy.search_hedge_after_seconds > 0 and queue
                             and len(running) < policy.search_hedge_racers and queue[0] not in paid_ids
                             and policy.search_hedge_after_seconds < remaining)
                finished, _ = await asyncio.wait(set(running), return_when=asyncio.FIRST_COMPLETED,
                    timeout=policy.search_hedge_after_seconds if hedge else remaining)
                if not finished:
                    if not hedge:
                        stopped = "time_cap"
                        break
                    hedged.append(queue[0])
                    launch()
                    continue
                finish = False
                for task in [item for item in running if item in finished]:
                    identifier = running.pop(task)
                    outcome = task.result()
                    manifest = outcome["manifest"]
                    base = {"source": identifier, "source_version": manifest.version,
                            **({"search_binding_id": outcome["binding"]} if outcome["binding"] is not None else {})}
                    if outcome.get("failure") is not None:
                        exc = outcome["failure"]
                        cost = getattr(exc, "cost_usd", None if manifest.paid else 0)
                        costs.append(_measured_cost(cost))
                        search_attempts.append({**base, "status": "failed", "failure": exc.code,
                            "cost_usd": _measured_cost(cost), "latency_ms": outcome["latency_ms"],
                            **({"raced": True} if identifier in hedged else {})})
                        planning_failure = exc
                        if source is not None or exc.code in policy.search_terminal_failures:
                            finish, stopped = True, stopped or "terminal"
                        continue
                    bundle = outcome["bundle"]
                    response, acquisition = bundle["response"], bundle["acquisition"]
                    receipt = acquisition["receipt"]
                    last_bundle, last_bundle_binding_id = bundle, outcome["binding"]
                    bundles[identifier], bindings[identifier] = bundle, outcome["binding"]
                    cost = _measured_cost(receipt.get("cost_usd"))
                    costs.append(cost)
                    provider_attempts.extend({**attempt, "search_source": identifier,
                                              "search_source_version": manifest.version}
                                             for attempt in receipt.get("attempts", []))
                    failure = (receipt.get("failure") or {}).get("code")
                    attempt = {**base, "status": receipt.get("status"),
                        **({"failure": failure} if failure else {}),
                        "cost_usd": cost, "latency_ms": outcome["latency_ms"],
                        "trace_id": receipt.get("trace_id"),
                        "evidence": copy.deepcopy(receipt.get("evidence", [])),
                        "upstream_failures": copy.deepcopy(response.get("upstream_failures", []))}
                    if identifier in hedged:
                        attempt["raced"] = True
                    search_attempts.append(attempt)
                    self._save_trace(acquisition, record_observations=False)
                    if receipt.get("status") == "observed" and winner is None:
                        # Options first (excluded domains, other sites, too old), then the
                        # checks: on the query, in the window, real pages, no repeats.
                        items, dropped = (filter_results(response["results"], options, now)
                                          if options else (response["results"], {}))
                        report = check_results(query, items, limit=limit, recency=recency, now=now,
                                               min_good=policy.search_good_results)
                        kept = accepted(items, report) if ladder else annotate(items, report)
                        rungs[identifier] = {"report": report, "kept": kept, "dropped": dropped}
                        attempt.update(count=len(response["results"]), passed=report["passed"],
                                       check=report["verdict"])
                        if len(kept) > best_count:
                            best, best_count = identifier, len(kept)
                        if not ladder:
                            winner, stopped, finish = identifier, "first_answer", True
                        elif report["verdict"] == "good":
                            winner, stopped, finish = identifier, "good", True
                        elif len(rungs) > 1 and len(merge_results(
                                [(name, rung["kept"]) for name, rung in rungs.items()])) >= report["need"]:
                            # No source is good alone; together their passing results are.
                            winner, stopped, finish = "merged", "good", True
                    if source is not None or failure in policy.search_terminal_failures:
                        finish, stopped = True, stopped or "terminal"
                if finish:
                    break
        finally:
            leftover = list(running.items())
            for task, _ in leftover:
                task.cancel()
            if leftover:
                await asyncio.gather(*(task for task, _ in leftover), return_exceptions=True)
        for _task, identifier in leftover:
            # Still running when another source won or the time cap hit: no result, and
            # for a paid source no way to know what it cost.
            paid = identifier in paid_ids
            costs.append(None if paid else 0)
            search_attempts.append({"source": identifier, "status": "cancelled",
                                    "cost_usd": None if paid else 0, "raced": identifier in hedged})
        if stopped is None:
            stopped = "source_cap" if ladder and len(order) > policy.search_max_sources else "exhausted"
        pool = [(name, rung["kept"]) for name, rung in rungs.items()]
        if winner in rungs:
            final = rungs[winner]["kept"]
        elif len(pool) > 1:
            final = merge_results(pool, windowed=bool(recency))
        else:
            final = pool[0][1] if pool else []
        final = final[:limit]
        if winner is None and final:
            winner = "merged" if len(pool) > 1 else pool[0][0]

        if last_bundle is None:
            failure = planning_failure or WebFailure("SEARCH_UNAVAILABLE", "Search sources were exhausted")
            receipt = {"trace_id": uuid.uuid4().hex, "operation": "search",
                "action_class": "READ_PUBLIC", "status": "failed",
                "observed_at": None, "freshness_seconds": None, "cache_hit": False,
                "method": None, "identity": policy.identity, "http_status": failure.http_status,
                "failure": {"code": failure.code, "message": failure.message}, "requested_url": None,
                "final_url": None, "confidence": None, "cost_usd": _total_cost(costs),
                "cost_basis": "aggregate search-source acquisition cost", "evidence": [],
                "attempts": provider_attempts, "latency_ms": round((time.monotonic() - started) * 1000)}
            response = {"query": query, "source": source, "results": [], "receipt": receipt,
                "query_attribution": None, "coverage": "unknown", "upstream_failures": []}
            acquisition = {"receipt": receipt, "url": None, "search_results": []}
        else:
            # The set that won, else the observed one with the most passing results,
            # else the last one that came back at all.
            base_name = winner if winner in bundles else best
            if base_name in bundles:
                last_bundle, last_bundle_binding_id = bundles[base_name], bindings[base_name]
            response, acquisition = last_bundle["response"], last_bundle["acquisition"]
            receipt = acquisition["receipt"]
            receipt["attempts"] = provider_attempts
            receipt["cost_usd"] = _total_cost(costs)
            receipt["latency_ms"] = round((time.monotonic() - started) * 1000)
            final_failure = (receipt.get("failure") or {}).get("code")
            if (receipt.get("status") == "failed" and source is None
                    and final_failure not in policy.search_terminal_failures):
                receipt["failure"] = {"code": "SEARCH_UNAVAILABLE",
                    "message": "Eligible search sources were exhausted"}
            if winner == "merged":
                response["upstream_failures"] = [row for name, bundle in bundles.items()
                                                 for row in bundle["response"].get("upstream_failures", [])]
        receipt["search_attempts"] = search_attempts
        selected_search_binding_id = (last_bundle_binding_id
            if last_bundle_binding_id is not None
            else self.searches.binding_id(source) if source is not None else None)
        if selected_search_binding_id is not None:
            receipt["search_binding_id"] = selected_search_binding_id
        if source is not None and "search_source_version" not in receipt:
            source_record = next((item for item in self.searches.inspect()
                                  if item["id"] == source), None)
            if source_record is not None:
                receipt["search_source_version"] = source_record["version"]
        receipt["requested_search_source"] = source
        basis = ("explicit_source" if source is not None else
                 "policy_candidate_order" if policy.search_source_candidates is not None else
                 "policy_preference_then_registry_order" if policy.search_source_prefer else
                 "provisional_free_registry_order")
        if options:
            filtered = {"excluded": 0, "off_site": 0, "too_old": 0, "undated": 0}
            for rung in rungs.values():
                for key, value in rung["dropped"].items():
                    filtered[key] += value
            receipt["search_options"] = {**options, "filtered": filtered}
        response["results"] = final
        receipt["search_routing"] = {"selection_basis": basis, "vertical": vertical,
            "candidates": candidates, "automatic_fallback": source is None,
            "benchmark_earned_order": False,
            "source_timeout_seconds": policy.search_source_timeout_seconds}
        # The ladder's receipt: how the sources were ordered and raced, who won, and
        # what the checks made of the results that came back.
        need = max(1, min(policy.search_good_results, limit))
        reasons, unknown = {}, 0
        for rung in rungs.values():
            unknown += rung["report"]["unknown_dates"]
            for reason, count in rung["report"]["reasons"].items():
                reasons[reason] = reasons.get(reason, 0) + count
        passed = sum(1 for item in final if item["check"]["passed"])
        verdict = (rungs[winner]["report"]["verdict"] if winner in rungs
                   else None if not rungs else "good" if passed >= need else "weak" if passed else "empty")
        receipt["search_ladder"] = {"enabled": ladder, "query_type": qtype, "order": order,
            "winner": winner, "stopped": stopped, "hedged": hedged, **order_notes}
        receipt["search_checks"] = {"verdict": verdict, "need": need, "passed": passed,
            "reasons": reasons, "unknown_dates": unknown,
            "results": [{"url": item["url"],
                         "source": (item.get("query_attribution") or {}).get("source")
                                   or next(iter(item.get("engines") or []), None),
                         **{key: item["check"][key] for key in
                            ("in_window", "on_query", "published", "date_source", "kind")}}
                        for item in final]}
        if ladder and policy.search_learned_routing:
            self._learn_search(qtype, [(item["source"], item["source"] in rungs
                                        and rungs[item["source"]]["report"]["verdict"] == "good",
                                        item.get("cost_usd")) for item in search_attempts
                                       if item["status"] != "cancelled"])
        if policy.max_cost_usd is not None:
            receipt.update(max_cost_usd=policy.max_cost_usd,
                cost_budget_satisfied=receipt["cost_usd"] is not None
                and receipt["cost_usd"] <= policy.max_cost_usd)
        response["receipt"] = receipt
        response["results"] = [{**item, "receipt": copy.deepcopy(receipt)}
                               for item in response["results"]]
        acquisition["search_results"] = response["results"]
        acquisition["query_attribution"] = response["query_attribution"]
        self._save_trace(acquisition, record_observations=False)
        return response

    async def _search_merged(self, query, limit, engine_config, policy, **options):
        """Every eligible source at once, fused by reciprocal rank (k=60)."""
        from .search import VERTICALS, normalize_options
        if options["vertical"] not in VERTICALS:
            raise ValueError("vertical must be one of: " + ", ".join(VERTICALS))
        normalize_options(options["site"], options["exclude_domains"], options["recency"], options["region"])
        policy = policy or WebPolicy()
        started = time.monotonic()
        sources = self.searches.candidates(policy, vertical=options["vertical"])[:policy.search_merge_sources]
        if not sources:
            return await self.search(query, None, limit, engine_config, policy, **options)

        async def one(identifier):
            try:
                return identifier, await self.search(query, identifier, limit,
                                                     engine_config if identifier == "searxng" else None,
                                                     policy, **options)
            except WebFailure as exc:
                return identifier, {"results": [], "receipt": {"status": "failed",
                                                               "failure": {"code": exc.code}}}
        answers = await asyncio.gather(*(one(identifier) for identifier in sources))
        fused, order = {}, []
        for identifier, answer in answers:
            for rank, item in enumerate(answer.get("results") or []):
                key = _result_key(item.get("url") or "")
                if key not in fused:
                    fused[key] = {**item, "engines": list(item.get("engines") or []),
                                  "fusion": {"score": 0.0, "sources": []}}
                    order.append(key)
                else:
                    for engine in item.get("engines") or []:
                        if engine not in fused[key]["engines"]:
                            fused[key]["engines"].append(engine)
                fused[key]["fusion"]["score"] += 1.0 / (60 + rank + 1)
                fused[key]["fusion"]["sources"].append({"source": identifier, "rank": rank + 1})
        results = sorted((fused[key] for key in order), key=lambda item: -item["fusion"]["score"])[:limit]
        for item in results:
            item["fusion"]["score"] = round(item["fusion"]["score"], 5)
        observed = [identifier for identifier, answer in answers
                    if (answer.get("receipt") or {}).get("status") == "observed"]
        costs = [(answer.get("receipt") or {}).get("cost_usd") for _, answer in answers]
        receipt = {"trace_id": uuid.uuid4().hex, "operation": "search", "action_class": "READ_PUBLIC",
                   "status": "observed" if observed else "failed",
                   **({} if observed else {"failure": {"code": "SEARCH_UNAVAILABLE",
                                                       "message": "Every merged search source failed"}}),
                   "observed_at": utcnow(), "cost_usd": None if None in costs else _total_cost(costs),
                   "latency_ms": round((time.monotonic() - started) * 1000),
                   "search_merge": [{"source": identifier,
                                     "status": (answer.get("receipt") or {}).get("status"),
                                     "failure": ((answer.get("receipt") or {}).get("failure") or {}).get("code"),
                                     "results": len(answer.get("results") or []),
                                     "trace_id": (answer.get("receipt") or {}).get("trace_id")}
                                    for identifier, answer in answers],
                   "search_routing": {"selection_basis": "merge", "vertical": options["vertical"],
                                      "candidates": sources, "automatic_fallback": False}}
        return {"query": query, "source": None, "results": results, "receipt": receipt,
                "query_attribution": None, "coverage": "returned-results" if observed else "unknown",
                "upstream_failures": []}

    def import_evidence(self, content: str, url: str, observed_at: str,
                        method: str = "operator_browser", content_type: str = "text/html",
                        http_status: int | None = None, image_urls: list[str] | None = None,
                        note: str | None = None) -> dict:
        """Persist supplied browser observations without claiming automated access or live state.

        This is an operator evidence bridge, not a programmable browser provider. The
        caller supplies actual observation time and facts; availability remains unknown.
        """
        _validate_url(url)
        if method not in {"operator_browser", "user_supplied"}: raise ValueError("Invalid import method")
        observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        if observed.tzinfo is None: raise ValueError("Observation time must include timezone")
        age = (datetime.now(timezone.utc) - observed).total_seconds()
        if age < -60: raise ValueError("Observation time cannot be in the future")
        if not isinstance(content, str) or len(content.encode()) > 40 * 1024 * 1024: raise ValueError("Invalid evidence size")
        if content_type not in {"text/html", "text/plain", "application/json"}: raise ValueError("Unsupported imported representation")
        parsed = parse_content(content, content_type, url, None,
                               adapter_registry=self.adapters)
        if image_urls is not None:
            for image in image_urls: _validate_url(image)
            parsed["image_urls"] = list(dict.fromkeys(image_urls))
        artifact = self._save_bytes(content.encode(), ".json" if "json" in content_type else ".html" if "html" in content_type else ".txt")
        result = {"url": url, "content": content, "content_type": content_type, "headers": {},
                  **parsed, "images": [], "field_status": {"content": "operator_observed", "availability": "unknown", "transaction_price": "unknown"},
                  "receipt": {"trace_id": uuid.uuid4().hex, "operation": "import_evidence", "status": "observed", "method": method,
                    "observed_at": observed.isoformat(), "freshness_seconds": max(age,0), "http_status": http_status,
                    "confidence": None, "failure": None, "cache_hit": False, "evidence": [artifact], "cost_usd": None,
                    "cost_basis": "Operator observation cost not measured", "latency_ms": None, "identity": None,
                    "attempts": [], "automation_verified": False, "note": note}}
        self._save_trace(result)
        return result

    def import_image_evidence(self, raw: bytes, url: str, observed_at: str,
                              policy: WebPolicy | None = None) -> dict:
        """Decode supplied public browser bytes without fetching or claiming automation."""
        policy = policy or WebPolicy()
        if policy.identity or policy.provider == "local_cdp":
            raise WebFailure("IDENTITY_POLICY_DENIED", "Public image import cannot attest a named identity")
        _validate_url(url)
        observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        if observed.tzinfo is None:
            raise ValueError("Observation time must include timezone")
        age = (datetime.now(timezone.utc) - observed).total_seconds()
        if age < -60:
            raise ValueError("Observation time cannot be in the future")
        if not isinstance(raw, bytes):
            raise ValueError("Image evidence must be bytes")
        if len(raw) > policy.max_image_bytes:
            raise WebFailure("LIMIT_EXCEEDED", "Imported image exceeds policy byte budget")
        try:
            with Image.open(io.BytesIO(raw)) as image:
                image.load()
                width, height, fmt = image.width, image.height, image.format
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
            raise WebFailure("INVALID_IMAGE", "Imported image could not be decoded") from None
        return {"url": url, "status": "decoded", "http_status": None,
                "width": width, "height": height, "format": fmt,
                **self._save_bytes(raw, ".image"), "observed_at": observed.isoformat(),
                "freshness_seconds": max(age, 0), "failure": None,
                "method": "operator_browser", "automation_verified": False,
                "source_binding": "operator_supplied"}

    observe = import_evidence

    def route_capabilities(self):
        from .route_memory import capability_summary
        return capability_summary(self.state_dir)

    async def paginate(self, url, adapter=None, policy=None, *,
                       continuation_adapter=None, policy_overrides=None):
        from .pagination import paginate
        from .routes import request_policy
        if adapter is None:
            raise ValueError("Paginate requires an adapter")
        effective, protected = request_policy(policy, policy_overrides)
        return await paginate(
            self, url, adapter, effective,
            continuation_adapter=continuation_adapter,
            protected_fields=protected, policy_overrides=policy_overrides)

    async def extract(self, url: str, adapter: str | None = None, policy: WebPolicy | None = None,
                      provider: str | None = None, *, policy_overrides: dict | None = None,
                      workload_assertions: dict | None = None):
        if adapter is None:
            adapter = "html"
        if not self.adapters.contains(adapter):
            raise ValueError("Unsupported adapter; register an installed trusted adapter first")
        return await self.read(url, policy, provider=provider, adapter=adapter,
                               policy_overrides=policy_overrides,
                               workload_assertions=workload_assertions)

    async def repair(self, trace_id: str, policy=None) -> dict:
        from .repair import run_repair
        return await run_repair(self, trace_id, policy)

    async def propose_module_repair(self, trace_id: str, module: dict, *, run_live_canary: bool = True) -> dict:
        """Validate a drafted next version of a site module that failed on a read.

        Checked with the base module's assertions against the page that read
        retained and a fresh independent read; activated only by the owner's
        promote_repair(proposal_id, proposal_sha256)."""
        from .site_modules import propose_module_repair
        return await propose_module_repair(self, trace_id, module, run_live_canary=run_live_canary)

    def promote_repair(self, proposal_id: str, expected_sha256: str, policy=None) -> dict:
        from .repair import promote_repair
        return promote_repair(self, proposal_id, expected_sha256, policy)

    def disable_repair(self, overlay_id: str, reason: str, policy=None) -> dict:
        from .repair import disable_repair
        return disable_repair(self, overlay_id, reason, policy)

    async def download_images(self, urls: list[str], policy: WebPolicy | None = None) -> list[dict]:
        """Download/decode exact image URLs with bounded concurrency and source attribution.

        Byte artifacts are returned by private local path/hash, never base64 in a tool prompt.
        A source rate/access block stops remaining downloads from that host.
        """
        policy = policy or WebPolicy()
        from .actions import require_action, required_read_action
        action_class = required_read_action(policy.identity)
        try:
            require_action(policy, action_class)
        except WebFailure as exc:
            return [{"url": url, "status": "failed", "failure": exc.code,
                     "action_class": action_class,
                     "identity": policy.identity}
                    for url in urls[:policy.max_images]]
        if policy.identity or policy.provider == "local_cdp":
            images = []
            resolved = None
            try:
                if not policy.identity:
                    raise WebFailure("IDENTITY_REQUIRED", "Local CDP requires an enrolled named identity")
                if not urls: return []
                resolved = self.identities.resolve(policy.identity,urls[0],provider=policy.provider,
                    allow_local_browser=policy.allow_local_browser,image=True)
                with self.identities.lease(resolved):
                    context = await self._identity_step(self._identity_context(resolved,policy),policy,probe=True)
                    await self._identity_step(self._identity_health(context,resolved,policy),policy)
                    images = await self._download_identity_images(urls,policy,resolved,context)
                    self.identities.recheck(resolved)
                return images
            except (WebFailure,IdentityFailure) as exc:
                return [{"url":url,"status":"failed","failure":exc.code,"identity":policy.identity} for url in urls[:policy.max_images]]
        async def one(url):
            domain = urlparse(url).netloc
            sem = self._domain_sems.setdefault(domain, asyncio.Semaphore(self._per_domain_count))
            async with sem:
                if self._blocked_domains.get(domain) in policy.context_stop_failures:
                    return {"url": url, "status": "failed", "failure": self._blocked_domains[domain]}
                delay = self._domain_next.get(domain, 0) - time.monotonic()
                if delay > 0: await asyncio.sleep(delay)
                self._domain_next[domain] = time.monotonic() + self._domain_delay
                result = await self._image(url, policy)
                if result.get("failure") in policy.context_stop_failures:
                    self._blocked_domains[domain] = result["failure"]
                return result
        # Batch releases its page semaphore before image acquisition. Sharing this
        # instance limit therefore bounds concurrent independent image batches too.
        async def limited(url):
            async with self._global: return await one(url)
        return await asyncio.gather(*(limited(url) for url in urls[:policy.max_images]))

    async def batch(self, urls: list[str], policy: WebPolicy | None = None, adapter: str | None = None,
                    **read_options) -> list[dict]:
        if policy and (policy.identity or policy.provider == "local_cdp"):
            # One canonical profile lease per operation; serialize named batch jobs.
            return [await self.read(url,policy,adapter=adapter,**read_options) for url in urls]
        # The first URL per site scouts: it climbs the ladder alone, and the rest
        # of that site wait for it, then start from the route it found (route
        # hints and clearance) instead of each climbing from the bottom at once.
        scouts = {}
        for index, url in enumerate(urls):
            scouts.setdefault(urlparse(url).netloc, (index, asyncio.Event()))

        async def one(index, url):
            domain = urlparse(url).netloc
            scout, scouted = scouts[domain]
            if index != scout:
                await scouted.wait()
            try:
                return await paced(url, domain)
            finally:
                if index == scout:
                    scouted.set()

        async def paced(url, domain):
            sem = self._domain_sems.setdefault(domain, asyncio.Semaphore(self._per_domain_count))
            async with self._global, sem:
                delay = self._domain_next.get(domain, 0) - time.monotonic()
                if delay > 0: await asyncio.sleep(delay)
                self._domain_next[domain] = time.monotonic() + self._domain_delay
                result = await self.read(url, replace(policy, include_images=False) if policy and policy.include_images else policy, adapter=adapter, **read_options)
            if policy and policy.include_images and result["receipt"]["status"] == "observed":
                result["images"] = await self.download_images(result["image_urls"], policy)
                self._save_trace(result)
            return result
        # Preserve input attribution and order, including duplicate URLs and individual failures.
        return await asyncio.gather(*(one(index, url) for index, url in enumerate(urls)))

    def capabilities(self, domain: str | None = None) -> list[dict]:
        """Empirical observation outcomes. These are not calibrated task-success probabilities."""
        groups = {}
        for path in (self.state_dir / "traces").glob("*.json"):
            result = json.loads(path.read_text())
            receipt = result["receipt"]
            host = urlparse(result["url"]).hostname
            if receipt.get("cache_hit") or (domain and host != domain): continue
            for attempt in receipt.get("attempts", []):
                key = (host, receipt.get("operation", "read"), attempt["provider"],
                       receipt.get("identity"),receipt.get("authority_mode"),receipt.get("executor"),
                       receipt.get("geography"),receipt.get("network_context"))
                group = groups.setdefault(key, {"domain": host, "operation": key[1], "provider": key[2],
                                              "identity_class": "explicit-local" if receipt.get("identity") else "public",
                                              "identity":receipt.get("identity"),"authority_mode":receipt.get("authority_mode"),
                                              "executor":receipt.get("executor"),"geography":receipt.get("geography"),
                                              "network_context":receipt.get("network_context"),
                                              "samples": 0, "observed": 0, "failures": {}, "latencies": [], "last_observed_at": None})
                group["samples"] += 1
                if attempt["status"] == "observed": group["observed"] += 1
                else:
                    code = attempt.get("failure", "UNKNOWN")
                    group["failures"][code] = group["failures"].get(code, 0) + 1
                group["latencies"].append(attempt["latency_ms"])
                if receipt.get("observed_at"):
                    group["last_observed_at"] = max(group["last_observed_at"] or "", receipt["observed_at"])
        results = []
        for group in groups.values():
            group["median_latency_ms"] = statistics.median(group.pop("latencies"))
            group["caveat"] = "Observation success only; no semantic listing-state correctness guarantee"
            results.append(group)
        return sorted(results, key=lambda group: (group["domain"] or "", group["operation"], group["provider"]))

    def trace(self, trace_id: str) -> dict:
        if not trace_id.isalnum(): raise ValueError("Invalid trace identifier")
        result = _redact_result_urls(json.loads((self.state_dir / "traces" / (trace_id + ".json")).read_text()))
        if result["receipt"].get("identity"):
            resolved = self.identities.resolve(result["receipt"]["identity"],result["url"])
            receipt = result["receipt"]
            if receipt.get("identity_generation") != resolved.generation or receipt.get("executor_generation") != resolved.executor_generation:
                raise IdentityFailure("IDENTITY_CHANGED", "Trace belongs to a different identity or profile generation")
        return result
