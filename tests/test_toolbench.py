import importlib.util
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("toolbench", SCRIPTS / "toolbench.py")
toolbench = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(toolbench)


def test_frozen_main_keeps_exact_urls_and_drops_dead_links(tmp_path):
    corpus = json.loads(toolbench.sitebench.CORPUS.read_text())["cases"]
    search = next(case for case in corpus if case["kind"] == "search")
    rows = [{"site": search["site"], "wall": search["wall"], "kind": "search", "url": search["url"],
             "followed_from": None, "status": "observed"},
            {"site": search["site"], "wall": search["wall"], "kind": "listing", "url": "https://x.test/1",
             "followed_from": search["url"], "status": "observed"},
            {"site": search["site"], "wall": search["wall"], "kind": "listing", "url": "https://x.test/2",
             "followed_from": search["url"], "status": "dead_link"}]
    path = tmp_path / "rows.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows))
    cases = toolbench.frozen_main(path)
    assert [case["url"] for case in cases] == [search["url"], "https://x.test/1"]
    assert cases[0]["follow"]["n"] == 0 and cases[0]["follow"]["pattern"] == search["follow"]["pattern"]


def test_heldout_sites_are_not_in_the_tuned_corpus_or_seeds():
    corpus_sites = {case["site"] for case in json.loads(toolbench.sitebench.CORPUS.read_text())["cases"]}
    seeds = json.loads((SCRIPTS.parent / "src/frankensurf/bundled_route_seeds.json").read_text())["recipes"]
    seeded = {recipe["origin"].split("//")[1].removeprefix("www.") for recipe in seeds}
    cases = toolbench.heldout()
    assert {case["set"] for case in cases} == {"heldout", "heldout2", "heldout3", "heldout4", "heldout5", "heldout6", "heldout7"}
    assert len({case["site"] for case in cases}) == len(cases)
    for case in cases:
        host = case["url"].split("//")[1].split("/")[0].removeprefix("www.")
        assert case["site"] not in corpus_sites and host not in seeded


def test_heldout_needs_the_query_term_three_times():
    case = {"set": "heldout", "kind": "article", "expect": "drill", "expect_min": 3, "url": "u"}
    page = {"text": "Search results. " * 60 + "drill " * 2, "receipt": {"status": "observed"}}
    assert toolbench.check(case, page) == (False, "expect_count:2")
    page["text"] += "drill"
    assert toolbench.check(case, page) == (True, None)


def test_summary_separates_login_cases_and_reports_the_single_tool_union():
    def row(arm, url, valid, login=False, cost=None, provider="http"):
        return {"arm": arm, "set": "main", "url": url, "login": login, "valid": valid,
                "status": "observed", "latency_ms": 100, "cost_usd": cost, "provider": provider}
    rows = [row("http", "a", True), row("http", "b", False), row("zenrows", "a", False, cost=0.025, provider="zenrows"),
            row("zenrows", "b", True, cost=0.025, provider="zenrows"), row("stitched_paid", "a", True),
            row("stitched_paid", "b", True), row("stitched_paid", "c", True, login=True)]
    summary = toolbench.summarize(rows)
    assert summary["http"]["main_public"]["valid"] == 1 and summary["stitched_paid"]["main_login"]["valid"] == 1
    assert summary["zenrows"]["main_public"]["usd_per_1k_valid"] == 50.0
    assert summary["_union_of_single_tools"]["main_public"] == {"cases": 2, "valid": 2}
