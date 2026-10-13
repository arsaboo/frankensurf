"""The weekly benchmark script: which arms run, and how a week is summarised (no reads here)."""
import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "weekly_bench.py"


def load():
    sys.path.insert(0, str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("weekly_bench", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_paid_arms_run_only_with_their_keys(monkeypatch):
    weekly = load()
    for name in ("FIRECRAWL_API_KEY", "ZYTE_API_KEY", "FRANKENSURF_FIRECRAWL_API_KEY", "FRANKENSURF_ZYTE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert weekly.arms() == weekly.FREE
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-key")
    assert weekly.arms() == [*weekly.FREE, "firecrawl", "stitched_paid"]


def test_a_week_is_summarised_per_arm():
    weekly = load()
    rows = [{"arm": "http", "valid": True, "latency_ms": 1000, "cost_usd": 0},
            {"arm": "http", "valid": False, "latency_ms": 9000, "cost_usd": 0},
            {"arm": "stitched_free", "valid": True, "latency_ms": 3000, "cost_usd": None}]
    out = weekly.summarize(rows)
    assert out["http"] == {"label": "Plain HTTP", "sites": 2, "read": 1, "rate": 0.5, "median_s": 1.0,
                           "p90_s": 1.0, "cost_usd": 0}
    assert out["stitched_free"]["label"] == "Frankensurf, free tools only" and out["stitched_free"]["rate"] == 1.0
