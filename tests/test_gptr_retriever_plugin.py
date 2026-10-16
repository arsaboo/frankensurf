"""contrib/gptr-frankensurf: GPT Researcher's retriever contract, with gpt_researcher faked."""
import importlib
import sys
import types
from pathlib import Path

import pytest

PLUGIN_SRC = Path(__file__).resolve().parents[1] / "contrib" / "gptr-frankensurf" / "src"


@pytest.fixture
def plugin(monkeypatch):
    base = types.ModuleType("gpt_researcher.retrievers.base")
    base.BaseRetriever = type("BaseRetriever", (), {})
    for name in ("gpt_researcher", "gpt_researcher.retrievers"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "gpt_researcher.retrievers.base", base)
    monkeypatch.syspath_prepend(str(PLUGIN_SRC))
    monkeypatch.delitem(sys.modules, "gptr_frankensurf", raising=False)
    return importlib.import_module("gptr_frankensurf")


def fake_search(calls, results_by_site):
    async def search(query, recency=None, site=None, vertical=None, limit=8):
        calls.append((query, site, limit))
        if results_by_site == "boom":
            raise RuntimeError("search source down")
        return {"results": results_by_site.get(site, [])}
    return search


def test_returns_href_and_body_in_the_contract_shape(plugin):
    retriever = plugin.FrankensurfSearch("python asyncio task groups")
    calls = []
    retriever.tools.search = fake_search(calls, {None: [
        {"url": "https://docs.python.org/3/library/asyncio-task.html", "snippet": "Coroutines and tasks"},
        {"url": "https://example.com/no-snippet", "title": "A title"},
        {"url": None, "snippet": "dropped: no URL"}]})
    assert retriever.requires_scraping is True
    assert retriever.search(max_results=5) == [
        {"href": "https://docs.python.org/3/library/asyncio-task.html", "body": "Coroutines and tasks"},
        {"href": "https://example.com/no-snippet", "body": "A title"}]
    assert calls == [("python asyncio task groups", None, 5)]


def test_domain_filter_searches_each_domain_and_merges_without_duplicates(plugin):
    retriever = plugin.FrankensurfSearch("release notes", query_domains=["a.example", "b.example"])
    calls = []
    retriever.tools.search = fake_search(calls, {
        "a.example": [{"url": "https://a.example/1"}, {"url": "https://shared.example/x"}],
        "b.example": [{"url": "https://shared.example/x"}, {"url": "https://b.example/2"}]})
    hrefs = [item["href"] for item in retriever.search(max_results=3)]
    assert [site for _, site, _ in calls] == ["a.example", "b.example"]
    assert hrefs == ["https://a.example/1", "https://shared.example/x", "https://b.example/2"]


def test_a_failing_search_returns_an_empty_list_instead_of_raising(plugin):
    retriever = plugin.FrankensurfSearch("anything")
    retriever.tools.search = fake_search([], "boom")
    assert retriever.search() == []


def test_paid_sources_only_when_asked(plugin, monkeypatch):
    assert plugin.FrankensurfSearch("q").tools.policy == {}
    monkeypatch.setenv("FRANKENSURF_GPTR_PAID", "1")
    monkeypatch.setenv("FRANKENSURF_GPTR_MAX_COST", "0.01")
    assert plugin.FrankensurfSearch("q").tools.policy == {"allow_paid_fallbacks": True, "max_cost_usd": 0.01}
