"""Frankensurf's read and search as framework tools (agent_tools.py). The frameworks are optional:
their tests skip when the library isn't installed; the shared operations are tested with a stand-in
Runtime, so nothing here touches the network."""
import asyncio
import json

import pytest

from frankensurf import agent_tools


class FakeRuntime:
    calls = []

    def __init__(self, state_dir):
        self.state_dir = state_dir

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def read(self, url, policy_overrides=None, **extra):
        FakeRuntime.calls.append(("read", url, policy_overrides, extra))
        result = {"url": url, "title": "Ferry timetable", "text": "The harbour ferry runs every twenty minutes. " * 900,
                  "main_text": "The article.",
                  "receipt": {"status": "observed", "trace_id": "t-1", "quality": {"grade": "good"},
                              "attempts": [{"provider": "http"}, {"provider": "camoufox"}], "cost_usd": 0}}
        if (policy_overrides or {}).get("extract_schema"):
            result["extracted"] = {"data": {"name": "Ferry"}, "missing": [], "valid": True}
        return result

    async def search(self, query, limit=10, recency=None, site=None, vertical=None, policy=None):
        FakeRuntime.calls.append(("search", query, limit, recency, site, vertical))
        return {"results": [{"url": "https://ferry.example.com/a", "title": "Ferry", "snippet": "Every 20 min",
                             "published": "2026-10-12T00:00:00Z", "check": {"passed": True}}],
                "receipt": {"search_checks": {"verdict": "good"}}}


@pytest.fixture
def fake(monkeypatch):
    from frankensurf import runtime
    FakeRuntime.calls = []
    monkeypatch.setattr(runtime, "Runtime", FakeRuntime)
    return FakeRuntime


def test_read_returns_what_an_agent_uses_and_caps_the_text(fake, tmp_path):
    tools = agent_tools.Tools(tmp_path, max_text=1000, allow_paid_fallbacks=True)
    out = asyncio.run(tools.read("https://ferry.example.com/timetable"))
    assert out["status"] == "observed" and out["quality"] == "good" and out["trace_id"] == "t-1"
    assert out["via"] == "camoufox" and len(out["text"]) == 1000 and out["truncated"] is True
    _, _, overrides, extra = fake.calls[0]
    assert overrides == {"allow_paid_fallbacks": True, "main_content": False} and extra == {}


def test_read_passes_main_schema_and_try_harder(fake, tmp_path):
    tools = agent_tools.Tools(tmp_path)
    schema = {"type": "object", "properties": {"name": {"type": "string"}}}
    out = asyncio.run(tools.read("https://ferry.example.com/x", main=True, extract_schema=schema,
                                 try_harder_than="t-0"))
    assert out["text"] == "The article." and out["extracted"]["data"] == {"name": "Ferry"}
    _, _, overrides, extra = fake.calls[0]
    assert overrides["extract_schema"] == schema and overrides["main_content"] is True
    assert extra == {"retry_of": "t-0"}


def test_search_returns_plain_results_and_bounds_the_limit(fake, tmp_path):
    out = asyncio.run(agent_tools.Tools(tmp_path).search("ferry timetable", recency="week", limit=500))
    assert out["results"][0] == {"url": "https://ferry.example.com/a", "title": "Ferry",
                                 "snippet": "Every 20 min", "published": "2026-10-12T00:00:00Z"}
    assert out["verdict"] == "good" and fake.calls[0][2] == 20


def test_a_bad_policy_setting_fails_when_the_tools_are_made(tmp_path):
    with pytest.raises((TypeError, ValueError)):
        agent_tools.Tools(tmp_path, not_a_setting=True)


def test_the_sync_runner_works_inside_a_running_event_loop():
    run = agent_tools._runner()

    async def inner():
        return 42

    async def outer():
        return run(inner())       # a framework calling a sync tool from async code
    assert run(inner()) == 42 and asyncio.run(outer()) == 42


def test_langchain_tools(fake, tmp_path):
    pytest.importorskip("langchain_core")
    read, search = agent_tools.langchain_tools(tmp_path)
    assert (read.name, search.name) == ("frankensurf_read", "frankensurf_search")
    assert json.loads(read.invoke({"url": "https://ferry.example.com/x"}))["title"] == "Ferry timetable"
    assert json.loads(asyncio.run(search.ainvoke({"query": "ferry"})))["results"]


def test_openai_agents_tools(fake, tmp_path):
    pytest.importorskip("agents")
    read, search = agent_tools.openai_agents_tools(tmp_path)
    assert read.name == "frankensurf_read" and "extract_schema_json" in read.params_json_schema["properties"]


def test_crewai_tools(fake, tmp_path):
    pytest.importorskip("crewai")
    read, search = agent_tools.crewai_tools(tmp_path)
    assert json.loads(read.run(url="https://ferry.example.com/x"))["status"] == "observed"


def test_the_claude_code_plugin_ships_the_same_skill_and_server():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    bundled = (root / "src/frankensurf/skill/SKILL.md").read_text()
    assert (root / "plugin/skills/frankensurf/SKILL.md").read_text() == bundled
    manifest = json.loads((root / "plugin/.claude-plugin/plugin.json").read_text())
    servers = json.loads((root / "plugin/.mcp.json").read_text())["mcpServers"]
    market = json.loads((root / ".claude-plugin/marketplace.json").read_text())
    assert manifest["name"] == "frankensurf" and servers["frankensurf"]["command"] == "frankensurf-mcp"
    assert market["plugins"][0]["source"] == "./plugin"


def test_the_registry_entry_matches_the_package():
    import tomllib
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    entry = json.loads((root / "server.json").read_text())
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    assert entry["version"] == version and entry["packages"][0]["version"] == version
    assert entry["packages"][0]["identifier"] == "frankensurf"
    assert f"mcp-name: {entry['name']}" in (root / "README.md").read_text()
