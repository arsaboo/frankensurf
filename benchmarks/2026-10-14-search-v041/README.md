# Search v0.41 against v0.40, judged blind (14 October 2026)

Blind set 8: 120 queries (20 each: announcements, news, how-to, code, papers, facts), written and
frozen on 13 October before any v0.41 change was measured, and never run before this. Both versions
read them as plain text (no vertical hints), one after the other, against the same local SearXNG
with the v0.41 engine settings, so the comparison isolates the search code. No paid sources: the
Exa and Firecrawl plans were out of credits.

Graded as before (`benchmarks/2026-10-13-search/`): every arm's top five per query pooled,
de-duplicated and shuffled, which tool found them hidden, and graded one by one by independent
model reviewers from title, snippet, URL and shown date: the right subject, the right kind of page,
inside the date window. 817 results; every verdict is in `review.json` (keyed by query and URL).

| Arm | Useful results (of 5) | Queries with any useful | With 3+ useful |
| --- | ---: | ---: | ---: |
| **v0.41, free, with the local search engine** | **3.42** | 116/120 | 83/120 |
| v0.40, free, with the local search engine | 2.71 | 106/120 | 62/120 |
| v0.41, free, no local search engine | 2.44 | 90/120 | 53/120 |

v0.41 against v0.40: more useful results on 41 queries, fewer on 12 (sign test p = 0.0001).
v0.41 with the local engine against v0.41 without it: more on 43, fewer on 13 (p = 0.0001).

By kind (useful of 5):

| Arm | Announcements | Code | Facts | How-to | News | Papers |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| v0.41 | 2.95 | 3.05 | 2.75 | 3.25 | 4.00 | 4.50 |
| v0.40 | 2.30 | 2.65 | 1.65 | 2.50 | 2.35 | 4.80 |
| v0.41, no local engine | 0.00 | 2.40 | 1.65 | 1.75 | 4.05 | 4.80 |

## Reading it

- The gain is broad: news (Google News, and company-event queries routed to news), facts and
  how-to gained most. Papers dipped slightly.
- Absolute scores are lower than blind set 7's (3.83 free), on different queries and a different
  night: several public engines behind SearXNG were refusing this address after a day of testing.
  Compare arms within a set, not across sets.
- Without the local engine, announcements score 0: the free web sources left (DuckDuckGo behind a
  CAPTCHA, Bing's web feed answering with stubs) find nothing in a month's window. Install the local
  engine (`frankensurf install search`) or allow a paid source.
- The reviewers are language models, told nothing about which tool found what.

Files: `blind8-v041-rows.jsonl` (arms `frankensurf`, `frankensurf_stock`), `blind8-v040-rows.jsonl`
(arm `frankensurf`, scored as v0.40), `review.json`. Scored with `scripts/searchreview.py`.
