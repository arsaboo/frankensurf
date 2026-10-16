"""Write compact, graded rows (no page text) for benchmarks/2026-10-16-gpt-researcher."""
import json, sys
from pathlib import Path
sys.path.insert(0, "scripts")
import toolbench
cases = {c["url"]: c for c in toolbench.heldout() if c["set"] == "heldout7"}
out_dir = Path(sys.argv[1]); out_dir.mkdir(parents=True, exist_ok=True)
for arm, path in [a.split("=", 1) for a in sys.argv[2:]]:
    with open(out_dir / f"{arm}.jsonl", "w") as out:
        for line in open(path):
            r = json.loads(line)
            case, text = cases[r["url"]], r.get("text") or ""
            if text:
                ok, reason = toolbench.check(case, {"text": text})
                verdict = "right" if ok else "wrong_page_passed_on"
            else:
                ok, reason, verdict = False, None, "no_page"
            out.write(json.dumps({"arm": arm, "site": r["site"], "wall": case["wall"], "url": r["url"],
                "verdict": verdict, "invalid_reason": reason, "text_chars": len(text),
                "latency_ms": r.get("latency_ms"), "status": r.get("status"), "failure": r.get("failure"),
                "method": r.get("method"), "error": r.get("error")}) + "\n")
    print("wrote", arm)
