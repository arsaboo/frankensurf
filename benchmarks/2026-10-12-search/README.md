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
