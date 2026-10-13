"""Frankensurf's read and search as tools for agent frameworks.

    from frankensurf.agent_tools import langchain_tools, openai_agents_tools, crewai_tools

    tools = langchain_tools()          # LangChain / LangGraph: StructuredTool objects
    tools = openai_agents_tools()      # OpenAI Agents SDK: FunctionTool objects
    tools = crewai_tools()             # CrewAI: BaseTool objects

Each returns two tools, frankensurf_read and frankensurf_search, backed by a Runtime on the
default state directory (or state_dir). Policy settings pass through as keyword arguments, for
example langchain_tools(allow_paid_fallbacks=True, max_cost_usd=0.02). The framework itself is
imported only when its function is called, so none of them is a dependency of Frankensurf.

A tool returns what an agent can use directly: the page text (or the article, with main), the
title and URL, a quality grade, and the trace_id to pass back as try_harder_than when the page
wasn't the right one. With extract_schema, the fields filled from the page's own data come back too.
"""
from __future__ import annotations

import asyncio
import json
import threading

READ_DESCRIPTION = (
    "Read a web page and return its text. Tries the cheapest tool first (plain HTTP, then browsers, "
    "stealth browsers, and paid tools only if allowed) and checks the page is real: a CAPTCHA, login "
    "wall or empty page counts as a failure. Set main=true for an article without menus and footers. "
    "Pass extract_schema (a JSON Schema object) to get fields filled from the page's own published data. If "
    "the page isn't what you needed, call again with try_harder_than set to the trace_id it returned.")
SEARCH_DESCRIPTION = (
    "Search the web and return checked results (url, title, snippet, published date). Every result "
    "is checked for topic, kind of page and date. Optional: recency (day, week, month, year), site "
    "(one domain), vertical (news, reference, code, papers, qa, discussions, books).")
MAX_TEXT = 20_000


def _runner():
    """Run a coroutine to completion from sync code, even inside a running event loop."""
    def run(coro):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        box = {}

        def target():
            try:
                box["value"] = asyncio.run(coro)
            except BaseException as error:  # handed back to the caller's thread
                box["error"] = error
        thread = threading.Thread(target=target)
        thread.start()
        thread.join()
        if "error" in box:
            raise box["error"]
        return box["value"]
    return run


class Tools:
    """The two operations, shared by every framework adapter."""

    def __init__(self, state_dir=None, max_text=MAX_TEXT, **policy):
        from .runtime import WebPolicy, default_state_dir
        WebPolicy(**policy)  # fail early on a bad setting
        self.state_dir = state_dir or default_state_dir()
        self.policy = policy
        self.max_text = max_text

    async def read(self, url: str, main: bool = False, extract_schema: dict | None = None,
                   try_harder_than: str | None = None) -> dict:
        from .runtime import Runtime
        overrides = {**self.policy, "main_content": bool(main)}
        if extract_schema:
            overrides["extract_schema"] = extract_schema
        extra = {"retry_of": try_harder_than} if try_harder_than else {}
        async with Runtime(self.state_dir) as web:
            result = await web.read(url, policy_overrides=overrides, **extra)
        receipt = result.get("receipt") or {}
        text = (result.get("main_text") if main and result.get("main_text") else result.get("text")) or ""
        out = {"url": result.get("url") or url, "title": result.get("title"),
               "status": receipt.get("status"),
               "quality": (receipt.get("quality") or {}).get("grade"),
               "text": text[:self.max_text], "truncated": len(text) > self.max_text,
               "trace_id": receipt.get("trace_id"),
               "via": (receipt.get("attempts") or [{}])[-1].get("provider"),
               "cost_usd": receipt.get("cost_usd")}
        if receipt.get("failure"):
            out["failure"] = receipt["failure"]
        if "extracted" in result:
            out["extracted"] = result["extracted"]
        return out

    async def search(self, query: str, recency: str | None = None, site: str | None = None,
                     vertical: str | None = None, limit: int = 8) -> dict:
        from .runtime import Runtime, WebPolicy
        async with Runtime(self.state_dir) as web:
            result = await web.search(query, limit=max(1, min(int(limit), 20)), recency=recency, site=site,
                                      vertical=vertical, policy=WebPolicy(**self.policy) if self.policy else None)
        results = [{"url": item.get("url"), "title": item.get("title"), "snippet": item.get("snippet"),
                    "published": item.get("published")} for item in result.get("results") or []]
        return {"query": query, "results": results,
                "verdict": ((result.get("receipt") or {}).get("search_checks") or {}).get("verdict")}


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def langchain_tools(state_dir=None, **policy):
    """frankensurf_read and frankensurf_search as LangChain StructuredTools (sync and async)."""
    from langchain_core.tools import StructuredTool
    tools, run = Tools(state_dir, **policy), _runner()

    async def frankensurf_read(url: str, main: bool = False, extract_schema: dict | None = None,
                               try_harder_than: str | None = None) -> str:
        return _json(await tools.read(url, main, extract_schema, try_harder_than))

    async def frankensurf_search(query: str, recency: str | None = None, site: str | None = None,
                                 vertical: str | None = None, limit: int = 8) -> str:
        return _json(await tools.search(query, recency, site, vertical, limit))

    def read_sync(url: str, main: bool = False, extract_schema: dict | None = None,
                  try_harder_than: str | None = None) -> str:
        return run(frankensurf_read(url, main, extract_schema, try_harder_than))

    def search_sync(query: str, recency: str | None = None, site: str | None = None,
                    vertical: str | None = None, limit: int = 8) -> str:
        return run(frankensurf_search(query, recency, site, vertical, limit))

    return [StructuredTool.from_function(func=read_sync, coroutine=frankensurf_read, name="frankensurf_read",
                                         description=READ_DESCRIPTION),
            StructuredTool.from_function(func=search_sync, coroutine=frankensurf_search, name="frankensurf_search",
                                         description=SEARCH_DESCRIPTION)]


def openai_agents_tools(state_dir=None, **policy):
    """frankensurf_read and frankensurf_search as OpenAI Agents SDK FunctionTools."""
    from agents import function_tool
    tools = Tools(state_dir, **policy)

    async def frankensurf_read(url: str, main: bool = False, extract_schema_json: str | None = None,
                               try_harder_than: str | None = None) -> str:
        schema = json.loads(extract_schema_json) if extract_schema_json else None
        return _json(await tools.read(url, main, schema, try_harder_than))

    async def frankensurf_search(query: str, recency: str | None = None, site: str | None = None,
                                 vertical: str | None = None, limit: int = 8) -> str:
        return _json(await tools.search(query, recency, site, vertical, limit))

    # A strict schema can't express a free-form object, so the schema travels as JSON text.
    read_description = READ_DESCRIPTION.replace("Pass extract_schema (a JSON Schema object)",
                                                "Pass extract_schema_json (a JSON Schema as a JSON string)")
    return [function_tool(frankensurf_read, name_override="frankensurf_read",
                          description_override=read_description),
            function_tool(frankensurf_search, name_override="frankensurf_search",
                          description_override=SEARCH_DESCRIPTION)]


def crewai_tools(state_dir=None, **policy):
    """frankensurf_read and frankensurf_search as CrewAI BaseTools."""
    from crewai.tools import BaseTool
    from pydantic import BaseModel, Field
    tools, run = Tools(state_dir, **policy), _runner()

    class ReadArgs(BaseModel):
        url: str = Field(description="The page to read")
        main: bool = Field(default=False, description="Return the article without menus and footers")
        extract_schema_json: str | None = Field(default=None, description="A JSON Schema, as JSON text, to fill from the page")
        try_harder_than: str | None = Field(default=None, description="trace_id of a read that wasn't right")

    class SearchArgs(BaseModel):
        query: str = Field(description="What to search for")
        recency: str | None = Field(default=None, description="day, week, month or year")
        site: str | None = Field(default=None, description="Only results from this domain")
        vertical: str | None = Field(default=None, description="news, reference, code, papers, qa, discussions, books")
        limit: int = Field(default=8, description="Number of results, up to 20")

    class FrankensurfRead(BaseTool):
        name: str = "frankensurf_read"
        description: str = READ_DESCRIPTION.replace("Pass extract_schema (a JSON Schema object)",
                                                    "Pass extract_schema_json (a JSON Schema as JSON text)")
        args_schema: type[BaseModel] = ReadArgs

        def _run(self, url: str, main: bool = False, extract_schema_json: str | None = None,
                 try_harder_than: str | None = None) -> str:
            schema = json.loads(extract_schema_json) if extract_schema_json else None
            return _json(run(tools.read(url, main, schema, try_harder_than)))

    class FrankensurfSearch(BaseTool):
        name: str = "frankensurf_search"
        description: str = SEARCH_DESCRIPTION
        args_schema: type[BaseModel] = SearchArgs

        def _run(self, query: str, recency: str | None = None, site: str | None = None,
                 vertical: str | None = None, limit: int = 8) -> str:
            return _json(run(tools.search(query, recency, site, vertical, limit)))

    return [FrankensurfRead(), FrankensurfSearch()]
