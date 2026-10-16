"""Grade each arm's rows with toolbench's own check (held-out set 7: kind article, expect >= 3 times).

right      the text passed the check
junk_in    the scraper handed back text that failed it (this goes into the report)
dropped    nothing came back (a block, an error, or the scraper's own rejection)
"""
import json, sys, statistics
sys.path.insert(0, "scripts")
import toolbench

cases = {c["url"]: c for c in toolbench.heldout() if c["set"] == "heldout7"}
out = {}
for label, path in [a.split("=", 1) for a in sys.argv[1:]]:
    rows = [json.loads(l) for l in open(path)]
    right = junk = dropped = 0
    lat, per_site = [], {}
    for r in rows:
        case = cases[r["url"]]
        text = r.get("text") or ""
        if not text:
            dropped += 1; per_site[r["site"]] = "dropped"; continue
        ok, reason = toolbench.check(case, {"text": text, "url": r["url"]})
        if ok:
            right += 1; per_site[r["site"]] = "right"
        else:
            junk += 1; per_site[r["site"]] = "junk:" + (reason or "")
        if r.get("latency_ms"): lat.append(r["latency_ms"] / 1000)
    lat_all = sorted((r.get("latency_ms") or 0) / 1000 for r in rows)
    out[label] = {"rows": len(rows), "right": right, "junk_in": junk, "dropped": dropped,
                  "median_s": round(statistics.median(lat_all), 1), "p90_s": round(lat_all[int(len(lat_all) * .9)], 1),
                  "per_site": per_site}
    print(f"{label:28s} right {right:3d}  junk_in {junk:3d}  dropped {dropped:3d}  median {out[label]['median_s']}s  p90 {out[label]['p90_s']}s")
labels = list(out)
if len(labels) >= 2:
    a, b = labels[0], labels[-1]
    gained = [s for s in out[a]["per_site"] if out[a]["per_site"][s] != "right" and out[b]["per_site"].get(s) == "right"]
    lost = [s for s in out[a]["per_site"] if out[a]["per_site"][s] == "right" and out[b]["per_site"].get(s) != "right"]
    print(f"{b} right where {a} wasn't: {len(gained)}; the reverse: {len(lost)} {lost}")
json.dump(out, open(sys.argv[1].split("=", 1)[1].rsplit("/", 1)[0] + "/summary.json", "w"), indent=1)
