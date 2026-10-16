"""Read every held-out set 7 URL with Frankensurf, free tools only (the toolbench stitched_free arm)."""
import asyncio, json, sys, time
sys.path.insert(0, "scripts")
import toolbench
from frankensurf import Runtime

async def main(cases_path, out, state):
    cases = json.load(open(cases_path))["cases"]
    overrides = {**toolbench.COMMON, **toolbench.ARMS["stitched_free"]}
    sem = asyncio.Semaphore(4)
    async with Runtime(state_dir=state) as web:
        async def one(c):
            async with sem:
                t = time.monotonic()
                try:
                    r = await asyncio.wait_for(web.read(c["url"], policy_overrides=overrides), 300)
                except Exception as e:
                    return {"site": c["site"], "url": c["url"], "text": None, "status": "failed", "failure": type(e).__name__}
                rec = r.get("receipt") or {}
                return {"site": c["site"], "url": c["url"], "latency_ms": round((time.monotonic() - t) * 1000),
                        "text": r.get("text"), "status": rec.get("status"), "method": rec.get("method"),
                        "failure": (rec.get("failure") or {}).get("code"), "trace_id": rec.get("trace_id"),
                        "cost_usd": rec.get("cost_usd") or 0}
        rows = await asyncio.gather(*(one(c) for c in cases))
    with open(out, "w") as f:
        for r in rows: f.write(json.dumps(r) + "\n")
    print("fs rows", len(rows), "observed", sum(1 for r in rows if r["status"] == "observed"))

asyncio.run(main(*sys.argv[1:4]))
