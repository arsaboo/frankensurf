# Search, judged blind (13 October 2026, v0.40.0)

## How it was judged
An automatic judge that checks a result for the query's words, the date window and
page shape agrees with Frankensurf's own checks by construction, so it cannot
measure Frankensurf (see `benchmarks/2026-10-12-search/` for the claims that judge
produced and their withdrawal). Every number here comes from a blind review:
each tool's top five results for each query, pooled, de-duplicated, shuffled and
stripped of which tool found them, then graded one by one by independent model
reviewers on whether the result actually answers the query: the right subject (not
a same-name company or a passing mention), the right kind of page (a story or press
release, a tutorial, the project's own repository, a paper, a page with the fact),
and inside the date window when the query has one. The reviewers see the title,
snippet, URL and shown date, not the page. `review.json` holds every verdict, keyed by query id and URL;
`scripts/searchreview.py` pools, blinds and scores (copy it to `$SEARCHREVIEW_DIR/verdicts.json`).

The metric is **useful results in the top five** (0 to 5), compared query by query
with a sign test. Queries are given to every tool as plain text: no vertical hints.

## The sets
- Blind set 5 (`benchmarks/2026-10-12-search/`) was used to tune v0.40 and is spent.
- **Blind set 6**: 120 queries (20 each: announcements, news, how-to, code, papers,
  facts), written before any v0.40 change was measured, run once with the code
  frozen at tag `search-v2-frozen-blind6`.
- **Blind set 7**: 120 more, written before any set 6 result was looked at, run once
  with the release candidate (tag `search-v2-release-candidate`).

## Blind set 7: the release
| Arm | Useful results (of 5) | Queries with any useful | Cost per 1k searches |
| --- | --- | --- | --- |
| Frankensurf, paid allowed | 4.67 | 120/120 | $9.38 |
| Exa Only | 4.66 | 120/120 | $9.50 |
| Frankensurf, free, with the local search engine | 3.83 | 116/120 | $0 |
| Frankensurf, free, no local search engine | 1.62 | 66/120 | $0 |

Frankensurf with paid allowed against Exa Only: more useful results on 11 queries,
fewer on 9 (p = 0.82): level. With paid allowed, Frankensurf asks Exa for the kind
of page the query wants, keeps Exa's results in Exa's order, and replaces the ones
its checks catch as wrong (org pages for a repository, docs sites, listings, papers
outside the window) with the best results from the free sources.

By kind, useful results: announcements 4.20 vs Exa 4.00; code 4.40 vs 4.60;
facts 4.95 vs 4.95; how-to 4.95 vs 4.95; news 4.60 vs 4.70; papers 4.90 vs 4.75.

## Blind set 6: against Firecrawl's search
Firecrawl's search was rate-limited on 47 of the 120 queries and its plan then ran
out of credits, so the comparison is on the 69 queries it answered (all
announcement, news and how-to queries, and 9 code queries):

| Arm | Useful results (of 5) | Cost per 1k searches |
| --- | --- | --- |
| Frankensurf, paid allowed | 4.55 | $9.33 |
| Exa Only | 4.55 | $9.50 |
| Frankensurf, free, with the local search engine | 3.78 | $0 |
| Firecrawl Search Only | 2.91 | $6.90 |
| Frankensurf, free, no local search engine | 1.13 | $0 |

Free Frankensurf against Firecrawl Search Only: more useful results on 35 queries,
fewer on 5 (p < 0.0001). On all 120 queries of set 6, Frankensurf with paid allowed
against Exa Only: 4.66 vs 4.70, more on 14, fewer on 11 (p = 0.69): level.

## Caveats
- The reviewers are language models (the same family that helped write the code),
  told nothing about which tool found what, and graded from title, snippet, URL and
  date only.
- "Free" means with the local search engine (SearXNG, run in Docker for these
  runs; `frankensurf install search` installs the same engine without Docker). Without it, free search has no general web
  engine and is far weaker (1.62).
- With paid allowed, Frankensurf's answer is built on Exa's, so "level with Exa" is
  the honest reading: it does not beat Exa on these sets.
- Firecrawl's numbers cover 69 of set 6's queries; it could not be run on set 7.
