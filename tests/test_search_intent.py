"""Query-intent routing: infer_vertical, and the wiring that uses it when no vertical is given."""
import pytest

from frankensurf import Runtime, WebPolicy
from frankensurf.search_checks import infer_vertical

from test_search_ladder import CALLS, Fake, fresh_calls, good, install  # noqa: F401 (autouse fixture)

CASES = [
    # code
    ("fastify github repo", "code"),
    ("gitlab runner source", "code"),
    ("htmx repository", "code"),
    ("serde crate", "code"),
    ("left-pad npm", "code"),
    ("requests pypi", "code"),
    ("postgres source code", "code"),
    ("library for parsing pdf files", "code"),
    ("framework for building cli tools", "code"),
    ("sdk for stripe payments", "code"),
    ("python library for image processing", "code"),
    ("rust crate for http servers", "code"),
    ("golang package for rate limiting", "code"),
    ("TypeScript library for validation", "code"),
    ("C++ library for matrices", "code"),
    ("ruby gem for pagination", "code"),
    ("typescript testing framework", "code"),
    ("python web framework", "code"),
    # papers
    ("papers on retrieval augmented generation", "papers"),
    ("paper on diffusion models", "papers"),
    ("research on sleep and memory", "papers"),
    ("arxiv mixture of experts", "papers"),
    ("transformer preprint", "papers"),
    ("survey of vector databases", "papers"),
    # news
    ("openai news", "news"),
    ("headlines", "news"),
    ("latest on the tariff talks", "news"),
    ("Nvidia today", "news"),
    ("what happened with Tesla this week", "news"),
    # reference
    ("who wrote Hamlet", "reference"),
    ("what is a monad", "reference"),
    ("what year did the Berlin Wall fall", "reference"),
    ("when was the telephone invented", "reference"),
    ("how tall is Mount Everest", "reference"),
    ("how many moons does Jupiter have", "reference"),
    ("capital of Mongolia", "reference"),
    ("population of Lagos", "reference"),
    ("currency of Japan", "reference"),
    ("Starry Night painted", "reference"),
    # qa
    ("how to reverse a list in python", "qa"),
    ("how do I undo a git commit", "qa"),
    ("TypeError: cannot read properties of undefined", "qa"),
    ("fix segfault in my_function()", "qa"),
    ("rust borrow checker error E0382", "qa"),
    ("npm install fails with ERR_OSSL_EVP_UNSUPPORTED", "qa"),
    ("java null pointer exception in main.java", "qa"),
    # web, including every negative
    ("Go to market strategy", "web"),
    ("Python the snake", "web"),
    ("python snake habitat", "web"),
    ("how to remove rust from a bike", "web"),
    ("how to boil an egg", "web"),
    ("how to fix a leaky tap", "web"),
    ("public library for kids near me", "web"),
    ("package delivery tracking", "web"),
    ("what is the best laptop to buy", "web"),
    ("cheap flights to lisbon", "web"),
    ("Anthropic announces new model", "web"),
    ("Apple launch event announcement", "web"),
    ("OpenAI press release", "web"),
    ("weather in Sydney", "web"),
    ("how I wrote my first novel", "web"),
    ("", "web"),
]


@pytest.mark.parametrize("query,expected", CASES)
def test_infer_vertical(query, expected):
    vertical, reason = infer_vertical(query)
    assert vertical == expected, (query, vertical, reason)
    assert reason


def test_inference_is_deterministic_and_only_returns_known_verticals():
    from frankensurf.search import VERTICALS
    for query, _ in CASES:
        assert infer_vertical(query) == infer_vertical(query)
        assert infer_vertical(query)[0] in VERTICALS


def test_announcement_phrasing_stays_web_with_or_without_a_window():
    assert infer_vertical("Nvidia announces earnings")[0] == "web"
    assert infer_vertical("Nvidia announces earnings", "week")[0] == "web"


def test_latest_entity_is_news_only_inside_a_short_window():
    assert infer_vertical("latest Nvidia", "day")[0] == "news"
    assert infer_vertical("latest Nvidia", "year")[0] == "web"
    assert infer_vertical("latest Nvidia")[0] == "web"


def test_a_discovery_phrase_beats_a_how_to():
    assert infer_vertical("how to pick a python library for scraping")[0] == "code"


# --- wiring ------------------------------------------------------------------

async def search(tmp_path, query, **kwargs):
    async with Runtime(tmp_path) as web:
        return await web.search(query, policy=WebPolicy(provider="http"), **kwargs)


async def test_inferred_vertical_picks_the_source_and_shows_in_the_receipt(monkeypatch, tmp_path):
    install(monkeypatch, Fake("webby", good(4, words="fastify github repo")),
            Fake("coder", good(4, "c", words="fastify github repo"), vertical="code"))
    result = await search(tmp_path, "fastify github repo")
    routing = result["receipt"]["search_routing"]
    assert CALLS[0] == "coder"
    assert routing["vertical"] == "code"
    assert routing["vertical_inferred"]["vertical"] == "code"
    assert "github" in routing["vertical_inferred"]["reason"]


async def test_a_plain_query_with_no_signal_goes_to_web_and_still_says_so(monkeypatch, tmp_path):
    install(monkeypatch, Fake("webby", good(4)), Fake("coder", good(4, "c"), vertical="code"))
    result = await search(tmp_path, "rust async")
    routing = result["receipt"]["search_routing"]
    assert CALLS == ["webby"]
    assert routing["vertical"] == "web"
    assert routing["vertical_inferred"] == {"vertical": "web", "reason": "no vertical signal"}


async def test_an_explicit_vertical_wins_and_is_not_marked_inferred(monkeypatch, tmp_path):
    install(monkeypatch, Fake("webby", good(4, words="fastify github repo")),
            Fake("coder", good(4, "c", words="fastify github repo"), vertical="code"))
    result = await search(tmp_path, "fastify github repo", vertical="web")
    routing = result["receipt"]["search_routing"]
    assert CALLS[0] == "webby"
    assert routing["vertical"] == "web"
    assert "vertical_inferred" not in routing


async def test_an_explicit_source_is_not_rerouted_by_inference(monkeypatch, tmp_path):
    install(monkeypatch, Fake("webby", good(4, words="fastify github repo")),
            Fake("coder", good(4, "c", words="fastify github repo"), vertical="code"))
    result = await search(tmp_path, "fastify github repo", source="webby")
    routing = result["receipt"]["search_routing"]
    assert CALLS == ["webby"]
    assert routing["vertical"] == "web"
    assert "vertical_inferred" not in routing


async def test_an_unknown_explicit_vertical_is_still_refused(monkeypatch, tmp_path):
    install(monkeypatch, Fake("webby", good(4)))
    with pytest.raises(ValueError, match="vertical must be one of"):
        await search(tmp_path, "rust", vertical="gossip")


async def test_merge_mode_infers_too(monkeypatch, tmp_path):
    install(monkeypatch, Fake("webby", good(4, words="fastify github repo")),
            Fake("coder", good(4, "c", words="fastify github repo"), vertical="code"))
    result = await search(tmp_path, "fastify github repo", mode="merge")
    routing = result["receipt"]["search_routing"]
    assert routing["selection_basis"] == "merge"
    assert routing["vertical"] == "code" and routing["candidates"] == ["coder"]
    assert routing["vertical_inferred"]["vertical"] == "code"
    assert CALLS == ["coder"]


async def test_merge_mode_with_an_explicit_vertical_does_not_infer(monkeypatch, tmp_path):
    install(monkeypatch, Fake("webby", good(4, words="fastify github repo")),
            Fake("coder", good(4, "c", words="fastify github repo"), vertical="code"))
    result = await search(tmp_path, "fastify github repo", mode="merge", vertical="web")
    routing = result["receipt"]["search_routing"]
    assert routing["vertical"] == "web" and "vertical_inferred" not in routing
    assert CALLS == ["webby"]


def test_the_cli_and_mcp_tool_default_to_inference():
    import inspect
    from frankensurf import cli, mcp_server
    parser = cli.build_parser() if hasattr(cli, "build_parser") else None
    if parser is not None:
        assert parser.parse_args(["search", "x"]).vertical is None
    assert inspect.signature(mcp_server.search.fn if hasattr(mcp_server.search, "fn") else mcp_server.search
                             ).parameters["vertical"].default is None
