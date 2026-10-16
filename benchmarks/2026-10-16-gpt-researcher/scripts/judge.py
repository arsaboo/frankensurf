"""Blind judge: per question, the three reports under shuffled labels, scored by a different model."""
import json, os, random, sys, collections
import httpx

MODEL = os.environ.get("JUDGE_MODEL", "openai/gpt-5.4")
rows = [json.loads(l) for l in open(sys.argv[1])]
by_q = collections.defaultdict(dict)
for r in rows:
    by_q[r["question"]][r["arm"]] = r
PROMPT = """You are grading research reports that answer the same question. You don't know how they were made.
Today is 16 October 2026. The reports cite web pages read today, so they may contain facts newer than
your training data: don't mark a fact wrong because you don't recognise it. Judge what the reports show.
Question: {q}

Score each report from 1 to 10 for how useful it is to someone who needs a current, correct answer:
- specific, current facts (numbers, prices, dates, names) that answer the question
- facts tied to cited sources, not vague generalities
- says plainly when something couldn't be found, instead of guessing
Ignore length and style.

{reports}

Reply with JSON only: {{"scores": {{"Report X": <1-10>, "Report Y": <1-10>, "Report Z": <1-10>}}, "why": "<one sentence>"}}"""
out, totals = [], collections.defaultdict(list)
for i, (q, arms) in enumerate(by_q.items()):
    labels = list(arms)
    random.Random(i).shuffle(labels)
    names = dict(zip(labels, ["Report X", "Report Y", "Report Z"]))
    text = "\n\n".join(f"=== {names[a]} ===\n{arms[a]['report'][:12000]}" for a in labels)
    resp = httpx.post("https://openrouter.ai/api/v1/chat/completions", timeout=180,
        headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"},
        json={"model": MODEL, "messages": [{"role": "user", "content": PROMPT.format(q=q, reports=text)}],
              "response_format": {"type": "json_object"}})
    resp.raise_for_status()
    verdict = json.loads(resp.json()["choices"][0]["message"]["content"])
    back = {v: k for k, v in names.items()}
    norm = lambda label: "Report " + label.strip().split()[-1].upper()
    scores = {back[norm(label)]: s for label, s in verdict["scores"].items() if norm(label) in back}
    for arm, s in scores.items():
        totals[arm].append(s)
    out.append({"question": q, "scores": scores, "why": verdict.get("why")})
    print(q[:60], scores, "|", verdict.get("why", "")[:140])
print("mean:", {a: round(sum(v) / len(v), 2) for a, v in sorted(totals.items())})
json.dump(out, open(sys.argv[2], "w"), indent=1)
