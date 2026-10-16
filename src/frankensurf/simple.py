"""The short way in: plain functions, no context manager, sync or async.

    import frankensurf as fs

    page = fs.read("https://example.com/pricing")
    if page.ok:
        print(page.title, page.text[:500])
    else:
        print("couldn't read it:", page.failure)        # e.g. {"code": "CAPTCHA", ...}

    pages = fs.batch(["https://a.example/1", "https://b.example/2"])
    hits = fs.search("frankensurf web reader", recency="month")

    page = await fs.aread(url)                           # inside async code

Every keyword the functions don't name is a WebPolicy setting (see the settings reference), so
anything Runtime.read can do, these can. Each call opens a Runtime on the default state directory
($FRANKENSURF_STATE or ~/.local/share/frankensurf/state), so route memory carries over between calls;
batch shares one Runtime across its URLs. For a long-running service, keep one `Runtime` open.
"""
from __future__ import annotations

from typing import Any, Iterable

__all__ = ["Page", "read", "aread", "batch", "abatch", "search", "asearch", "map", "amap",
           "crawl", "acrawl", "ask", "aask", "extract", "aextract", "summarize", "asummarize"]


class Page(dict):
    """A read's result: the same dict Runtime.read returns, with attribute shortcuts."""

    @property
    def receipt(self) -> dict:
        return self.get("receipt") or {}

    @property
    def ok(self) -> bool:
        """True when Frankensurf got the page; False when every tool it tried failed."""
        return self.receipt.get("status") == "observed"

    @property
    def failure(self) -> dict | None:
        """Why it couldn't read the page ({"code", "message"}), or None."""
        return None if self.ok else (self.receipt.get("failure") or {"code": "UNKNOWN"})

    @property
    def url(self) -> str | None:
        return self.get("url")

    @property
    def title(self) -> str | None:
        return self.get("title")

    @property
    def text(self) -> str:
        """The page's text, or the article alone when read with main=True."""
        return self.get("main_text") or self.get("text") or ""

    @property
    def markdown(self) -> str:
        """The page as markdown, when read with markdown=True (else the plain text)."""
        return self.get("markdown") or self.text

    @property
    def links(self) -> list[dict]:
        return self.get("links") or []

    @property
    def attempts(self) -> list[dict]:
        """Each tool tried, in order: provider, status, failure, message, failure_stage, latency_ms."""
        from .runtime import attempt_summary
        return attempt_summary(self.receipt)

    @property
    def via(self) -> str | None:
        """The tool that got the page."""
        return self.receipt.get("method")

    @property
    def cost_usd(self) -> float:
        return self.receipt.get("cost_usd") or 0.0

    @property
    def trace_id(self) -> str | None:
        """Pass to try_harder_than when the page isn't the one you needed."""
        return self.receipt.get("trace_id")


def _overrides(paid: bool, max_cost: float | None, main: bool, links: bool, policy: dict,
               markdown: bool = False) -> dict:
    overrides = dict(policy)
    if paid:
        overrides["allow_paid_fallbacks"] = True
    if max_cost is not None:
        overrides["max_cost_usd"] = max_cost
    if main:
        overrides["main_content"] = True
    if links:
        overrides["include_links"] = True
    if markdown:
        overrides["markdown_output"] = True
    return overrides


async def aread(url: str, *, paid: bool = False, max_cost: float | None = None, main: bool = False,
                links: bool = False, markdown: bool = False, provider: str | None = None,
                try_harder_than: str | None = None, state_dir: str | None = None, **policy: Any) -> Page:
    """Read one page. paid allows paid tools whose keys are set; max_cost caps this read in USD."""
    from .runtime import Runtime, default_state_dir
    async with Runtime(state_dir or default_state_dir()) as web:
        result = await web.read(url, provider=provider, retry_of=try_harder_than,
                                policy_overrides=_overrides(paid, max_cost, main, links, policy, markdown))
    return Page(result)


async def abatch(urls: Iterable[str], *, paid: bool = False, max_cost: float | None = None,
                 main: bool = False, links: bool = False, markdown: bool = False,
                 state_dir: str | None = None, **policy: Any) -> list[Page]:
    """Read many pages, paced per site; results keep the order of urls."""
    from .runtime import Runtime, default_state_dir
    async with Runtime(state_dir or default_state_dir()) as web:
        results = await web.batch(list(urls), policy_overrides=_overrides(paid, max_cost, main, links, policy,
                                                                          markdown))
    return [Page(result) for result in results]


async def asearch(query: str, *, limit: int = 10, recency: str | None = None, site: str | None = None,
                  vertical: str | None = None, paid: bool = False, state_dir: str | None = None,
                  **policy: Any) -> list[dict]:
    """Checked search results: dicts with url, title, snippet and published (when known)."""
    from .runtime import Runtime, WebPolicy, default_state_dir
    settings = dict(policy)
    if paid:
        settings["allow_paid_fallbacks"] = True
    settings.setdefault("provider", "http")  # search pages load over plain HTTP, as the CLI does
    async with Runtime(state_dir or default_state_dir()) as web:
        found = await web.search(query, limit=limit, recency=recency, site=site, vertical=vertical,
                                 policy=WebPolicy(**settings))
    return [{"url": item.get("url"), "title": item.get("title"), "snippet": item.get("snippet"),
             "published": item.get("published")} for item in found.get("results") or []]


async def amap(url: str, *, limit: int = 5000, search: str | None = None, include_subdomains: bool = False,
               state_dir: str | None = None, **policy: Any) -> list[str]:
    """A site's URLs, from its sitemaps and the start page's links, without reading them. search
    keeps the URLs that mention its words, best match first."""
    from .crawl import site_map
    from .runtime import Runtime, default_state_dir
    async with Runtime(state_dir or default_state_dir()) as web:
        found = await site_map(web, url, limit=limit, search=search, include_subdomains=include_subdomains,
                               policy_overrides=policy)
    return found["links"]


async def acrawl(url: str, *, limit: int = 100, max_depth: int = 2, include: list[str] | None = None,
                 exclude: list[str] | None = None, include_subdomains: bool = False, paid: bool = False,
                 max_cost: float | None = None, respect_robots: bool = True, entire_domain: bool = False,
                 state_dir: str | None = None, **policy: Any) -> list[dict]:
    """Read a site: the start page, its sitemap and the links they lead to, up to max_depth hops and
    limit pages, only under the start URL's path unless entire_domain. include/exclude are regular
    expressions on the URL path. Each page: url, status,
    title, markdown, failure, trace_id."""
    from .crawl import crawl as crawl_site
    from .runtime import Runtime, default_state_dir
    async with Runtime(state_dir or default_state_dir()) as web:
        done = await crawl_site(web, url, limit=limit, max_depth=max_depth, include=include, exclude=exclude,
                                include_subdomains=include_subdomains, respect_robots=respect_robots,
                                entire_domain=entire_domain,
                                policy_overrides=_overrides(paid, max_cost, False, False, policy))
    return done["pages"]


async def aask(question: str, *, model: str | None = None, max_steps: int = 8, paid: bool = False,
               max_cost: float | None = None, state_dir: str | None = None, **completion: Any) -> dict:
    """Agent mode: a model you choose (any LiteLLM model) searches and reads with Frankensurf and
    answers with sources. Returns answer, sources, couldnt_read, steps, model, llm_cost_usd. Extra
    keywords go to litellm.acompletion (temperature, api_base ...). pip install "frankensurf[agent]"."""
    from .agent import ask
    from .runtime import Runtime, default_state_dir
    async with Runtime(state_dir or default_state_dir()) as web:
        return await ask(web, question, model=model, max_steps=max_steps,
                         policy=_overrides(paid, max_cost, False, False, {}), **completion)


async def aextract(urls, *, schema: dict | None = None, prompt: str | None = None, model: str | None = None,
                   paid: bool = False, state_dir: str | None = None, **completion: Any) -> dict:
    """Structured data from one or more pages with an LLM, checked against schema (JSON Schema) when
    given. Returns data, sources, couldnt_read, model, llm_cost_usd (and error if it never fit)."""
    from .agent import extract
    from .runtime import Runtime, default_state_dir
    async with Runtime(state_dir or default_state_dir()) as web:
        return await extract(web, [urls] if isinstance(urls, str) else list(urls), schema=schema, prompt=prompt,
                             model=model, policy=_overrides(paid, None, False, False, {}), **completion)


async def asummarize(url: str, *, model: str | None = None, paid: bool = False, state_dir: str | None = None,
                     **completion: Any) -> dict:
    """A short summary of a page with an LLM, or why it couldn't be read."""
    from .agent import summarize
    from .runtime import Runtime, default_state_dir
    async with Runtime(state_dir or default_state_dir()) as web:
        return await summarize(web, url, model=model, policy=_overrides(paid, None, False, False, {}), **completion)


def _run(coroutine):
    from .agent_tools import _runner
    return _runner()(coroutine)


def read(url: str, **options: Any) -> Page:
    """Sync read(); safe to call from inside a running event loop too. Options as aread()."""
    return _run(aread(url, **options))


def batch(urls: Iterable[str], **options: Any) -> list[Page]:
    """Sync batch(). Options as abatch()."""
    return _run(abatch(urls, **options))


def search(query: str, **options: Any) -> list[dict]:
    """Sync search(). Options as asearch()."""
    return _run(asearch(query, **options))


def map(url: str, **options: Any) -> list[str]:  # noqa: A001 - Firecrawl's name for it
    """Sync map(). Options as amap()."""
    return _run(amap(url, **options))


def crawl(url: str, **options: Any) -> list[dict]:
    """Sync crawl(). Options as acrawl()."""
    return _run(acrawl(url, **options))


def ask(question: str, **options: Any) -> dict:
    """Sync ask(). Options as aask()."""
    return _run(aask(question, **options))


def extract(urls, **options: Any) -> dict:
    """Sync extract(). Options as aextract()."""
    return _run(aextract(urls, **options))


def summarize(url: str, **options: Any) -> dict:
    """Sync summarize(). Options as asummarize()."""
    return _run(asummarize(url, **options))
