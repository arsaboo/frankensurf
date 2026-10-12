"""Search v2, iteration 3: the stricter on-query rule (action words, term coverage), library-or-tool
ranking for code needs, name lookups that rank add-ons below the project's own pages, stock blurbs in
news, topic-in-the-title for papers, and marketing explainers that are not announcements. The URL and
title shapes are examples of generic patterns; the fixtures are synthetic and nothing here touches the
network."""
from datetime import datetime, timedelta, timezone

import pytest

from frankensurf.search import REGISTRY, _arxiv_query
from frankensurf.search_checks import (accepted, canonical_url, check_results, enough_terms, key_terms, marketing_path,
                                       demote_unconfirmed_repos, query_context, stock_blurb, unrelated_set, _need_fit)

from test_search_v2 import row

NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
FRESH = (NOW - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")


def verdicts(query, rows, **kwargs):
    report = check_results(query, rows, limit=10, now=NOW, **kwargs)
    return {item["url"]: item for item in report["results"]}


def order_of(query, rows, **kwargs):
    report = check_results(query, rows, limit=10, now=NOW, **kwargs)
    return [item["url"] for item in accepted(rows, report)]


def repo(owner, name, snippet, stars, language="Go"):
    return row("https://github.com/%s/%s" % (owner, name), "%s/%s" % (owner, name), snippet,
               extra={"stars": stars, "stargazers_count": stars, "language": language, "fork": False,
                      "archived": False})


# --- 1a. action words and auxiliaries are filler, but still count when alone ------------------------

@pytest.mark.parametrize("query,expected", [
    ("how to use regex lookahead", ["regex", "lookahead"]),
    ("how to use matplotlib subplots", ["matplotlib", "subplot"]),
    ("creating a simple example of pandas groupby", ["panda", "groupby"]),
    ("best way to write a tutorial for rust", ["rust"]),
    ("how to use", ["use"]),                              # nothing else left: the weak word is the query
    ("guide examples", ["guide", "exampl"]),
    ("Use Case Diagram", ["use", "case", "diagram"]),     # written as a name, it is the topic
])
def test_action_and_quality_words_drop_only_when_other_terms_remain(query, expected):
    assert key_terms(query) == expected


# --- 1b. on-query: coverage of the key terms ----------------------------------------------------

DICTIONARY = [
    row("https://www.merriam-webster.com/dictionary/use", "Use Definition & Meaning - Merriam-Webster",
        "The meaning of USE is to put into action or service. How to use a word in a sentence."),
    row("https://dictionary.cambridge.org/dictionary/english/use", "USE | English meaning - Cambridge Dictionary",
        "USE definition: to put something such as a tool, skill or building to a particular purpose."),
    row("https://www.dictionary.com/browse/use", "USE Definition & Meaning | Dictionary.com",
        "Use definition, to employ for some purpose; put into service."),
]


def test_navigational_words_drop_from_the_terms_but_not_when_alone():
    assert key_terms("nintendo home page") == ["nintendo"]
    assert key_terms("nintendo official website") == ["nintendo"]
    assert key_terms("official website") == ["official", "website"]


@pytest.mark.parametrize("query", ["how to use regex lookahead", "how to use matplotlib subplots"])
def test_dictionary_pages_for_the_word_use_are_off_query(query):
    got = verdicts(query, DICTIONARY)
    assert all("off_query" in item["reasons"] and not item["passed"] for item in got.values()), got


def test_pages_about_the_topic_of_a_how_to_query_pass():
    lookahead = row("https://www.regular-expressions.info/lookaround.html", "Regex Lookahead and Lookbehind Tutorial",
                    "Lookahead is zero-length: how to use regex lookahead to match without consuming.")
    subplots = row("https://matplotlib.org/stable/gallery/subplots_axes_and_figures/subplots_demo.html",
                   "Creating multiple subplots using plt.subplots - Matplotlib", "")
    assert verdicts("how to use regex lookahead", [lookahead])[lookahead["url"]]["passed"]
    assert verdicts("how to use matplotlib subplots", [subplots])[subplots["url"]]["passed"]


def test_one_of_two_terms_is_not_enough_without_the_entity():
    assert not enough_terms(["regex", "lookahead"], "lookahead assertions in java", "")
    assert enough_terms(["regex", "lookahead"], "regex lookahead", "")
    # the rarer (longer) one plus the entity
    assert enough_terms(["regex", "lookahead"], "lookahead assertions in java", "", entity=True)
    assert not enough_terms(["regex", "lookahead"], "regex cheat sheet", "", entity=True)


def test_three_or_more_terms_need_half_of_them_or_the_entity_or_a_phrase():
    terms = ["python", "parse", "json", "file"]           # 4 terms: 2 are half
    assert not enough_terms(terms, "python tutorials", "")
    assert enough_terms(terms, "python tutorials", "", entity=True)
    assert enough_terms(terms, "python json basics", "")
    assert enough_terms(terms, "reading a json file", "")  # two terms next to each other, and two terms
    five = ["learn", "machine", "learning", "with", "python"]
    assert not enough_terms(five, "machine overview", "")
    assert enough_terms(five, "machine learning", "")      # a phrase of two adjacent terms
    assert enough_terms(["a1", "b2", "c3"], "a1 and c3", "")           # 2 of 3 is more than half


@pytest.mark.parametrize("query,item", [
    ("fastify github repo", row("https://github.com/fastify/fastify", "fastify/fastify", "Fast and low overhead web framework")),
    ("who founded Samsung", row("https://en.wikipedia.org/wiki/Samsung", "Samsung - Wikipedia",
                                "Samsung was founded by Lee Byung-chul in 1938.")),
    ("news about BP this week", row("https://www.bbc.com/news/business-12345678", "BP profits rise on oil prices",
                                    "BP reported higher third-quarter profits", published=FRESH)),
    ("recent papers on speculative decoding", row("https://arxiv.org/abs/2211.17192",
                                                  "Fast Inference from Transformers via Speculative Decoding",
                                                  "We propose speculative decoding to sample from autoregressive models.")),
    ("rust crate for serialization", row("https://serde.rs/", "Serde",
                                         "Serde is a framework for serializing and deserializing Rust data structures.")),
    ("rust crate for serialization", row("https://crates.io/crates/serde", "serde - crates.io: Rust Package Registry",
                                         "A generic serialization/deserialization framework")),
    ("how to merge two dataframes in pandas", row("https://pandas.pydata.org/docs/reference/api/pandas.DataFrame.merge.html",
                                                  "pandas.DataFrame.merge - pandas documentation", "Merge DataFrame or named Series objects")),
])
def test_legitimate_pages_still_pass_the_stricter_rule(query, item):
    got = verdicts(query, [item])[item["url"]]
    assert got["on_query"] is True, got
    assert "off_query" not in got["reasons"]


def test_serialization_matches_its_verb_forms_but_not_other_words():
    assert enough_terms(["serialization"], "serializing data", "")
    assert enough_terms(["configuration"], "configure the app", "")
    assert not enough_terms(["information"], "informal talk", "")
    assert not enough_terms(["foundation"], "founded in 1938", "")


# --- 1c. an unrelated set, by the stricter rule --------------------------------------------------

def test_a_set_of_dictionary_pages_is_unrelated_for_a_how_to_query():
    assert unrelated_set("how to use regex lookahead", DICTIONARY) is True
    assert unrelated_set("how to use matplotlib subplots", DICTIONARY) is True


def test_one_on_query_result_makes_the_set_related():
    good = row("https://regex101.com/", "regex101: lookahead example", "Test regex lookahead online")
    assert unrelated_set("how to use regex lookahead", DICTIONARY + [good]) is False
    assert unrelated_set("fastify github repo", [row("https://github.com/fastify/fastify", "fastify", "")]) is False
    assert unrelated_set("", DICTIONARY) is False and unrelated_set("how to use regex lookahead", []) is False


def test_the_last_resort_web_source_is_checked_for_unrelated_sets():
    assert REGISTRY["bing_rss"]["verify_relevance"] is True and REGISTRY["bing_rss"]["last_resort"] is True


# --- 2. code needs: a library, or a tool on the topic -----------------------------------------------------

TOOLS = [
    repo("hashicorp", "terraform", "Terraform enables you to safely and predictably create, change, and improve "
         "infrastructure with declarative configuration files.", 43000),
    repo("kelseyhightower", "confd", "Manage local application configuration files using templates and data from "
         "etcd or consul", 8200),
    repo("instrumenta", "kubeval", "Validate your Kubernetes configuration files, supports multiple Kubernetes "
         "versions", 3100),
    repo("lucasdillmann", "nginx-ignition", "A desktop application to manage nginx configuration files", 600),
]
LIBRARIES = [
    repo("knadh", "koanf", "Simple, extremely lightweight, extensible, configuration management library for Go. "
         "Supports reading config files", 3500),
    row("https://pkg.go.dev/github.com/spf13/viper", "viper package - go library for configuration",
        "Package viper is a configuration library: reading config files"),
]


def test_tool_repositories_rank_below_libraries_and_web_pages_that_name_them():
    order = order_of("go library for configuration files", TOOLS + LIBRARIES)
    assert set(order[:2]) == {LIBRARIES[0]["url"], LIBRARIES[1]["url"]}
    assert set(order[2:]) == {item["url"] for item in TOOLS}


def test_a_tool_repository_still_fills_when_nothing_else_is_left():
    assert order_of("go library for configuration files", TOOLS)[0].endswith("/terraform")


@pytest.mark.parametrize("query,description,fits", [
    ("rust crate for serialization", "A serialization library", True),
    ("rust crate for serialization", "A framework for serializing data", True),
    ("go library for configuration files", "A command line tool for configuration files", False),
    ("typescript testing framework", "A delightful JavaScript Testing Framework", True),
    ("library for parsing markdown", "A toolkit that parses markdown", True),
    ("library for parsing markdown", "Render markdown in your terminal", False),
])
def test_a_need_repository_must_call_itself_a_library_or_a_synonym(query, description, fits):
    item = repo("someone", "thing", description, 5000, language=None)
    assert _need_fit(item, query_context(query, vertical="code"), "thing") is fits


def test_a_need_with_no_noun_asked_needs_the_topic_words_in_the_description():
    both = repo("a", "yaml-things", "Parse yaml with comments and schemas", 5000, None)
    one = repo("b", "other", "Parse xml quickly", 9000, None)
    got = verdicts("best yaml comments parsing", [both, one], vertical="code")
    assert got[both["url"]]["relevance"] > got[one["url"]]["relevance"]


def test_a_web_only_library_repository_is_not_held_behind_confirmed_tools_for_a_need():
    tool = {**repo("hashicorp", "terraform", "tool", 43000), "check": {"relevance": -9.0}}
    web = {**row("https://github.com/dsbasko/go-cfg", "Go Configuration Library"), "check": {"relevance": 2.0}}
    page = {**row("https://pkg.go.dev/x/config", "config package"), "check": {"relevance": 2.5}}
    confirmed = {canonical_url("https://github.com/hashicorp/terraform")}
    assert demote_unconfirmed_repos([tool, web, page], confirmed, need=True) == [page, web, tool]
    # not a need (a name lookup): the confirmed repository stays ahead of the web-only one
    assert demote_unconfirmed_repos([tool, web, page], confirmed) == [tool, page, web]


def test_an_ing_term_matches_its_other_forms():
    assert enough_terms(["modeling"], "climate models", "") and enough_terms(["modeling"], "modelling the climate", "")
    assert enough_terms(["learning"], "we learn from data", "")
    assert not enough_terms(["modeling"], "modern art", "")


# --- 3. code name lookups: the project, then web pages about it, then add-ons ---------------------------

def test_add_on_repositories_rank_below_the_projects_own_pages():
    rows = [
        repo("Colin-b", "pytest_httpx", "Send responses to httpx", 400, "Python"),
        repo("florimondmanca", "httpx-sse", "Consume Server-Sent Event (SSE) messages with HTTPX", 500, "Python"),
        repo("encode", "httpx", "A next generation HTTP client for Python", 14000, "Python"),
        repo("frankie567", "httpx-oauth", "Async OAuth client using HTTPX", 600, "Python"),
        row("https://www.python-httpx.org/", "HTTPX", "A next-generation HTTP client for Python."),
        repo("frankie567", "httpx-ws", "WebSocket support for HTTPX", 300, "Python"),
        row("https://pypi.org/project/httpx/", "httpx · PyPI", "The next generation HTTP client."),
    ]
    order = order_of("httpx python github", rows)
    assert order[0] == "https://github.com/encode/httpx"
    assert set(order[1:3]) == {"https://www.python-httpx.org/", "https://pypi.org/project/httpx/"}
    assert {url.rsplit("/", 1)[1] for url in order[3:]} == {"pytest_httpx", "httpx-sse", "httpx-oauth", "httpx-ws"}


def test_tokio_core_ranks_below_the_project_and_its_website():
    rows = [
        repo("tokio-rs", "tokio-core", "Core I/O and event loop for Tokio (deprecated)", 700, "Rust"),
        row("https://tokio.rs/", "Tokio - An asynchronous Rust runtime", "Tokio is a runtime for writing reliable applications."),
        repo("tokio-rs", "tokio", "A runtime for writing reliable asynchronous applications with Rust", 28000, "Rust"),
        row("https://crates.io/crates/tokio", "tokio - crates.io: Rust Package Registry", "An event-driven, non-blocking I/O platform"),
    ]
    order = order_of("tokio github repository", rows)
    assert order[0] == "https://github.com/tokio-rs/tokio"
    assert order[-1] == "https://github.com/tokio-rs/tokio-core"


def test_a_repository_named_for_the_project_still_ranks_first_and_filters_stay():
    fork = repo("someone", "httpx", "fork", 2000, "Python")
    fork["extra"]["fork"] = True
    wrong = repo("x", "httpx", "go clone", 2000, "Go")
    got = verdicts("httpx python github", [fork, wrong, repo("encode", "httpx", "client", 14000, "Python")])
    assert got[fork["url"]]["passed"] is False and "fork" in got[fork["url"]]["reasons"]
    assert "wrong_language" in got[wrong["url"]]["reasons"]
    assert got["https://github.com/encode/httpx"]["passed"] is True


# --- 4. news: auto-generated stock blurbs ---------------------------------------------------------------

BLURBS = [
    ("VALE S.A. (VALE) Outperforms Broader Market", "https://finance.example/news/vale-outperforms-broader-market"),
    ("VALE S.A. (VALE) Is a Trending Stock: Facts to Know", "https://finance.example/news/vale-trending-stock-facts"),
    ("Investors heavily search VALE SA (VALE)", "https://finance.example/news/investors-heavily-search-vale"),
]


@pytest.mark.parametrize("title", [
    "VALE S.A. (VALE) Outperforms Broader Market", "VALE S.A. (VALE) Is a Trending Stock: Facts to Know",
    "Investors heavily search Baidu, Inc. (BIDU): Here is What You Need to Know",
    "Investors heavily search VALE SA (VALE)", "Acme Corp (NYSE: ACME) Underperforms Broader Market",
    "Acme (NASDAQ:ACME) price target raised", "Is Acme (ACME) stock moves a signal?"])
def test_stock_blurb_titles(title):
    assert stock_blurb(title)


@pytest.mark.parametrize("title", [
    "Vale reports third-quarter iron ore output", "Vale shares fall as ore prices drop",     # no ticker
    "Vale S.A. (VALE) to hold earnings call on Thursday", "Iron ore outperforms expectations",       # no ticker / no market word
    "Vale (Brazil) outperforms expectations"])
def test_ordinary_titles_are_not_blurbs(title):
    assert not stock_blurb(title)


def news_rows():
    return [row(url, title, "Vale", published=FRESH) for title, url in BLURBS] + [
        row("https://www.reuters.com/markets/vale-q3-output-2026-10-10/", "Vale reports third-quarter iron ore output",
            "Vale said iron ore output rose", published=FRESH),
        row("https://www.mining.example/news/vale-signs-nickel-deal", "Vale signs nickel supply deal with battery maker",
            "Vale announced a deal", published=FRESH)]


def test_stock_blurbs_fail_when_other_news_exists():
    got = verdicts("news about Vale past month", news_rows(), recency="month", vertical="news")
    for _title, url in BLURBS:
        assert got[url]["passed"] is False and "stock_blurb" in got[url]["reasons"], got[url]
    assert sum(1 for item in got.values() if item["passed"]) == 2


def test_stock_blurbs_stay_but_last_when_nothing_else_exists():
    rows = news_rows()[:3]
    got = verdicts("news about Vale past month", rows, recency="month", vertical="news")
    assert all(item["passed"] for item in got.values())
    ordinary = row("https://www.example.com/news/vale-dam-inspection", "Vale dam inspection finds no fault",
                   "Vale", published=FRESH)
    report = check_results("news about Vale past month", rows + [ordinary], limit=10, now=NOW, recency="month",
                           vertical="news")
    assert min(v["relevance"] for v in report["results"][3:]) > max(v["relevance"] for v in report["results"][:3])


def test_blurbs_are_not_demoted_when_the_query_asks_about_the_stock():
    got = verdicts("Vale stock news", news_rows(), recency="month", vertical="news")
    assert all(got[url]["passed"] for _title, url in BLURBS)
    assert not any("stock_blurb" in item["reasons"] for item in got.values())


def test_stock_blurbs_only_matter_for_news_and_announcement_queries():
    got = verdicts("vale outperforms broader market", news_rows()[:1])
    assert "stock_blurb" not in got[BLURBS[0][1]]["reasons"]


# --- 5. papers: the topic in the title ----------------------------------------------------------------

def paper(i, title, snippet=""):
    return row("https://arxiv.org/abs/2610.%05d" % i, title, snippet, published=FRESH)


CLIMATE = "recent papers on climate modeling with deep learning"


def climate_rows():
    return [
        paper(1, "Epidemic forecasting with climate covariates", "learning from deep neural climate data"),
        paper(2, "Phenology under a changing climate", "deep learning models for climate phenology"),
        paper(3, "Climate Modeling with Deep Learning: an emulator for atmospheric dynamics", ""),
        paper(4, "XVA hedging with deep learning", "climate risk modeling"),
        paper(5, "Deep learning emulators for climate", "modeling the atmosphere"),
        paper(6, "Mortality forecasting under climate change", "deep learning for mortality modeling"),
    ]


def test_papers_with_the_topic_in_the_title_rank_first():
    order = order_of(CLIMATE, climate_rows(), recency="year", vertical="papers")
    assert order[0].endswith("00003") and order[1].endswith("00005")
    assert {url[-5:] for url in order[2:]} == {"00001", "00002", "00004", "00006"}


def test_a_title_with_under_half_the_key_terms_ranks_below_one_with_more():
    got = verdicts(CLIMATE, climate_rows(), recency="year", vertical="papers")
    by = {url[-5:]: item["relevance"] for url, item in got.items()}
    assert by["00003"] > by["00005"] > max(by["00001"], by["00002"], by["00004"], by["00006"])
    # four terms: climate, modeling, deep, learning. "XVA hedging with deep learning" has two (half).
    assert by["00004"] > by["00001"]


def test_all_terms_scattered_across_a_long_title_is_not_a_full_match():
    scattered = paper(20, "Climate-Dyna Deep Hedging for XVAs: Model-Based Reinforcement Learning, Residual Climate HVA")
    compact = paper(21, "Rewiring climate modeling with deep learning emulators")
    got = verdicts(CLIMATE, [scattered, compact], recency="year", vertical="papers")
    assert got[compact["url"]]["relevance"] > got[scattered["url"]]["relevance"]
    assert order_of(CLIMATE, [scattered, compact], recency="year", vertical="papers")[0] == compact["url"]


def test_single_term_papers_rank_lower_still_when_the_best_covers_a_phrase():
    single = paper(10, "Speculative sampling methods", "decoding in large models")
    with_phrase = paper(11, "Speculative Decoding for Large Language Models", "")
    alone = verdicts("papers on speculative decoding", [single], vertical="papers")[single["url"]]["relevance"]
    both = verdicts("papers on speculative decoding", [single, with_phrase], vertical="papers")
    assert both[single["url"]]["relevance"] == pytest.approx(alone - 4.0)
    assert both[with_phrase["url"]]["relevance"] > both[single["url"]]["relevance"]


def test_the_arxiv_query_drops_action_words_when_topic_words_remain():
    assert "all:use" not in _arxiv_query("how to use speculative decoding")
    assert "all:speculative" in _arxiv_query("how to use speculative decoding")


# --- 6. announcements: marketing explainers are not news ---------------------------------------------

EXPLAINERS = [
    ("https://www.paypal.com/us/brc/article/payment-links-make-selling-easy", "Payment links make selling easy | PayPal"),
    ("https://www.paypal.com/us/brc/article/reduce-checkout-abandonment", "How to reduce checkout abandonment | PayPal"),
    ("https://www.paypal.com/us/business/resources/how-to-take-payments-in-store-today", "Take payments in store | PayPal"),
    ("https://www.paypal.com/us/blog/tips/how-to-reduce-chargebacks-for-small-business", "Reduce chargebacks | PayPal"),
    ("https://www.paypal.com/us/smarthelp/article/how-do-i-link-a-card-to-my-account", "Link a card | PayPal Help"),
]


@pytest.mark.parametrize("url,title", EXPLAINERS[:4])
def test_marketing_explainers_on_the_entitys_domain_are_not_announcements(url, title):
    got = verdicts("PayPal latest announcement", [row(url, title)], recency="month")[url]
    assert got["passed"] is False and "not_article" in got["reasons"], got


def test_announcements_and_off_domain_articles_are_unaffected():
    rows = [
        row("https://newsroom.paypal-corp.com/2026-10-08-paypal-announces-new-payment-links", "PayPal announces new payment links"),
        row("https://www.paypal.com/us/newsroom/paypal-launches-agentic-checkout", "PayPal launches agentic checkout"),
        row("https://techcrunch.com/blog/paypal-launches-new-payment-links-for-merchants", "PayPal launches new payment links"),
        row("https://example.com/resources/paypal-announces-new-payment-links-for-merchants", "PayPal announces payment links"),
        # a verified in-window date wins even under a marketing segment
        row("https://www.paypal.com/us/brc/2026/10/08/payment-links-make-selling-easy", "Payment links make selling easy"),
    ]
    got = verdicts("PayPal latest announcement", rows, recency="month")
    assert all("not_article" not in item["reasons"] for item in got.values()), got


def test_marketing_path_segments():
    assert marketing_path("https://x.com/us/brc/article/a-b-c")
    assert marketing_path("https://x.com/learn/how-to-do-things")
    assert marketing_path("https://x.com/blog/tips/save-time-with-this")
    assert not marketing_path("https://x.com/blog/x-announces-a-thing")
    assert not marketing_path("https://x.com/newsroom/support-ukraine-announcement")
