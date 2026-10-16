"""One GPT Researcher run per (arm, question); writes the report and what it read."""
import asyncio, json, os, sys, time
from gpt_researcher import GPTResearcher

async def main(question, out_path):
    t0 = time.monotonic()
    researcher = GPTResearcher(query=question, report_type="research_report")
    await researcher.conduct_research()
    report = await researcher.write_report()
    sources = researcher.get_research_sources() or []
    row = {"arm": os.environ["E2E_ARM"], "question": question,
           "seconds": round(time.monotonic() - t0, 1), "cost_usd": researcher.get_costs(),
           "visited": len(researcher.visited_urls or []),
           "sources": [{"url": s.get("url"), "chars": len(s.get("raw_content") or s.get("content") or "")} for s in sources],
           "report": report}
    with open(out_path, "a") as f:
        f.write(json.dumps(row) + "\n")
    print(row["arm"], "|", question[:50], "|", row["seconds"], "s |", f"${row['cost_usd']:.4f}", "|",
          len(sources), "sources |", len(report), "chars report")

asyncio.run(main(sys.argv[1], sys.argv[2]))
