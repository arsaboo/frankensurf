# Search benchmark, 12 October 2026

150 search queries in six kinds, 25 each: windowed news ("news about X this week"),
company announcements, how-to and docs, recent papers, code and repos, and
factual reference. They were written before any source was run, then split
75 dev / 75 held-out by a seeded shuffle (`scripts/searchbench-queries.json`).
The dev set was used while fixing search; the held-out set was run once, after.

A result is **right** when all of these hold:

- it is about the query (the query's entity terms are in its title or snippet,
  or in the page once read);
- it is inside the date window when the query has one (the result's
  publication date, or the page's own published date once read; unknown counts
  as not verified);
- it is an item, not a homepage or listing, when the query asks for one;
- it is not a challenge or error page.

**hit@5** is the share of queries with at least one right result in the top
five. **precision@5** is the share of the top five that are right. Pairs are
compared with an exact McNemar test.

## Held-out set (75 queries)

| Arm | hit@5 | precision@5 | cost |
|---|---|---|---|
| Frankensurf v0.37 (free) | 96.0% | 82.9% | $0 |
| Frankensurf v0.37, paid allowed | 96.0% | 83.2% | $0 (never needed) |
| Frankensurf v0.36.1 (free) | 61.3% | 45.6% | $0 |
| Exa Only | 100% | 92.5% | $0.73 |
| Firecrawl Search Only | 89.3% | 61.9% | $0.88 |
| SearXNG Only | 86.7% | 57.6% | $0 |
| Bing News RSS Only | 69.3% | 48.8% | $0 |
| Bing RSS Only | 37.3% | 28.5% | $0 |

Paired, v0.37 free against:

- v0.36.1: right on 26 queries it missed, never the reverse, p < 0.0001
- Exa Only: 0 against 3, p = 0.25 (a tie within the noise of 75 queries)
- Firecrawl Search Only: 7 against 2, p = 0.18 (not significant)
- SearXNG Only: 8 against 1, p = 0.039

Per-category tables: `python scripts/searchbench.py --rows
benchmarks/2026-10-12-search/heldout-rows.jsonl --set heldout`. In the rows file
the v0.36.1 router is the `frankensurf_old` arm.

| hit@5 by kind | announcement | code | fact | how-to | news | paper |
|---|---|---|---|---|---|---|
| v0.37 (free) | 88.2% | 100% | 100% | 100% | 100% | 93.8% |
| v0.36.1 | 82.4% | 25.0% | 100% | 100% | 87.5% | 0% |
| Exa Only | 100% | 100% | 100% | 100% | 100% | 100% |

## Caveats

- The v0.36.1 and v0.37 runs ran at the same time from one machine. GitHub's
  search API rate-limited the v0.36.1 run (20 of its GitHub queries failed),
  which lowers v0.36.1's code score. Removing the code category, v0.37 still
  leads on every other kind but facts and how-to, where both score 100%.
- Precision on company announcements is the weak spot: 48% against Exa's 91%.
- Judging is automatic: term matching, published dates, and URL shape, not a
  human reading every result.
- 75 queries is small. A tie with Exa means "not distinguishable here", not "as good".

## Blind set 2: announcement precision (30 queries)

After v0.37.0, announcement precision was the weak spot. Undated results and
newsroom/changelog index pages filled the top five. The fix reads undated top
results for their publication date, drops the ones outside the window, and
recognises index pages by path. To measure it without reusing a spent set,
30 new queries were written before the change was measured: 20 announcement
queries for companies in no other set, in five phrasings, and 10 news queries.
Each arm was run once (`--set blind2`).

| Arm | hit@5 | precision@5 | announcement precision | cost |
|---|---|---|---|---|
| v0.37.0 (free) | 90.0% | 56.7% | | $0 |
| v0.38 announcement change (free) | 96.7% | 92.0% | 93.0% | $0 |
| Exa Only | 100% | 94.0% | 92.0% | $0.36 |

Rows: `blind2-rows-old.jsonl` (v0.37.0) and `blind2-rows-new.jsonl`.
The stricter homepage and company-name checks in v0.38 were added after this
run and are not part of these numbers.

## Blind set 3 (60 queries, 10 per kind)

Written at 16:46 AEDT before v0.39 was measured, with entities in no other set,
and run once. "v0.39 candidate" is v0.38 plus entity aliases and acronyms, the
cost-cap and PDF changes, and the macOS install changes.

| Arm | hit@5 | precision@5 | announcement precision | paper precision | cost |
|---|---|---|---|---|---|
| v0.38.0 (free) | 96.7% | 89.0% | 62.0% | 72.0% | $0 |
| v0.39 candidate (free) | 96.7% | 89.4% | 64.5% | 72.0% | $0 |
| v0.39 candidate, paid allowed | 96.7% | 90.7% | 72.2% | 72.0% | $0.015 |
| Exa Only | 100% | 94.7% | 100% | 92.0% | $0.57 |

The candidate did not beat v0.38 here (0 queries either way). Announcement
precision on these companies was far below blind set 2's 93%, and arXiv
returned off-topic papers. Two causes, found on this set and fixed after it:

1. Undated results filled the top five: URLs dated as `/2026/sep/0915-...` or
   `20261006...` were not read as dates, and pages without date metadata stayed in.
   v0.39 reads both URL forms, and in a windowed search, when any result's date is
   verified, unverified ones leave `results` for `unverified_results`.
2. arXiv was sorted by date, so any new paper mentioning the words ranked first.
   v0.39 sorts by relevance within the date window, with the topic as a phrase first.

**These fixes were found on blind set 3, so blind set 3 cannot measure them.**
On the dev set after the fixes: announcement precision 100% (8 queries, 4.0
results each on average, down from 5), paper 97.8%, news 98.8%, hit@5 100% in
all three. A further claim needs a new blind set.

Rows: `blind3-rows.jsonl` (`frankensurf_v038` is v0.38.0).

## Blind set 4: v0.39.0 (120 queries, 20 per kind)

Written at 21:34 AEDT on 12 October 2026, after v0.39.0 was released and before
it was measured: no query and no company from an earlier set. Every tool on its
own and Frankensurf, run once, the same night, from a home connection (paid
APIs from their own networks).

| Tool | Queries with a right result in the top 5 | Right results per query (of 5) | Share of returned results right | Cost per 1k searches |
| --- | --- | --- | --- | --- |
| Exa Only | 100% | 4.68 | 93.7% | $9.50 |
| Frankensurf, paid allowed | 100% | 4.53 | 97.4% | $0.76 |
| **Frankensurf, free** | **100%** | **4.36** | **95.8%** | **free** |
| Firecrawl Search Only | 99.2% | 3.54 | 70.8% | $11.40 |
| SearXNG Only | 82.5% | 2.92 | 58.5% | free |
| Bing News RSS Only | 74.2% | 2.38 | 64.4% | free |
| Wikipedia Only | 49.2% | 1.97 | 41.5% | free |
| arXiv Only | 39.2% | 1.53 | 37.7% | free |
| Bing RSS Only | 39.2% | 1.39 | 27.8% | free |
| GitHub Only | 33.3% | 1.31 | 31.3% | free |
| Stack Exchange Only | 28.3% | 1.21 | 24.6% | free |
| Hacker News Only | 27.5% | 0.85 | 22.0% | free |
| Open Library Only | 13.3% | 0.28 | 8.6% | free |
| DuckDuckGo Only | 0% (CAPTCHA every time) | 0 | 0% | free |

Paired, query by query (sign test):

- Frankensurf free against Firecrawl Search Only: more right results on 53 queries, fewer on 22 (p = 0.0004); higher precision on 65, lower on 8 (p < 0.0001).
- Frankensurf free against Exa Only: Exa returns more right results on 33 queries, Frankensurf on 19 (p = 0.07, not significant); Frankensurf has higher precision on 24, lower on 10 (p = 0.02), partly because it returns fewer results when it cannot verify a date (4.56 per query against 5.0).
- Frankensurf free against SearXNG Only, the best free engine on its own: more right results on 64 queries, fewer on 6 (p < 0.0001).

By kind, Frankensurf free against Exa Only, right results per query: announcements 2.7 against 4.7 (Frankensurf returns 3.1 results; the rest it cannot date), news 4.2 against 4.7, how-to 4.8 against 4.8, code 4.7 against 4.2, papers 4.9 against 5.0, facts 4.8 against 4.8.

So: free Frankensurf search answers as many queries as Exa, is clearly ahead
of Firecrawl's search and of every free engine on its own, and trails Exa
slightly on how many right results it returns, most of the gap on company
announcements, where it holds back results it cannot date.

Rows: `blind4-rows.jsonl`.

## Independent review (blind): the automatic judge overstated Frankensurf

The judge above counts a result as right when it names the query's subject,
falls inside the date window and is not a homepage, listing or challenge page.
Frankensurf's own search checks apply the same rules, so the router is graded
on its own test. To check it, 60 of blind set 5's queries (10 per kind) were
reviewed blind: the top five from Frankensurf's default install (no SearXNG)
and from Exa Only, pooled, deduplicated, shuffled and stripped of which tool
found them (571 results), then graded one by one by six independent model
reviewers on whether each actually answers the query (right subject, right kind
of thing, right time), from title, snippet, URL and date.

| Arm | Useful results per query (of 5) | Not useful | Queries with any useful | Queries with 3+ useful |
| --- | --- | --- | --- | --- |
| Exa Only | 4.83 | 0.17 | 60/60 | 60/60 |
| Frankensurf, default install | 2.80 | 2.03 | 51/60 | 31/60 |

Exa Only had more useful results on 42 queries, Frankensurf on 3 (sign test
p < 0.0001). By kind (Frankensurf against Exa Only): how-to 4.8 against 4.9,
papers 3.8 against 4.8, news 3.3 against 4.8, announcements 2.2 against 4.9,
facts 1.7 against 5.0, code 1.0 against 4.6. Of the results the automatic judge
called right, the reviewers rejected 126 of 543: forks and demo repos for
"fastify github repo", film pages for "who wrote War and Peace", a TV show for
"Block latest announcement".

**Every comparison claim above this section is withdrawn.** The automatic
judge stays as a quick regression check; claims about search need the blind
review. Files: `review/`.
