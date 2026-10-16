# GPT Researcher, as it ships vs pointed at `frankensurf serve` (16 October 2026)

The 150 sites of held-out set 7 (`scripts/toolbench-heldout7.json`: chosen before any was read,
never tuned on), read with GPT Researcher 0.16.1's own `Scraper` class, one URL at a time, and each
page graded by toolbench's check (`scripts/toolbench.py`: the text must hold what the page should,
at least three times). One run each, free tools only, no paid spend.

| Arm | Right page | Wrong page passed on | No page |
| --- | --- | --- | --- |
| GPT Researcher's default scraper (`SCRAPER=bs`) | 58 | 15 | 77 |
| GPT Researcher with `SCRAPER=firecrawl` pointed at `frankensurf serve` | 134 | 3 | 13 |
| Frankensurf read directly (`stitched_free` arm), for reference | 134 | 4 | 12 |

- Pointed at Frankensurf, GPT Researcher read 76 sites its default scraper didn't, and none the
  other way.
- Of the default scraper's 15 wrong pages, 2 were a "Client Challenge" interstitial its block-page
  check doesn't know (wikiHow, Clas Ohlson); the rest were search pages that came back as just the
  menu because their results load with JavaScript, and "not found" pages served as 200.
- Speed, from the direct arm (four reads at a time): median 9.0 s a page, slowest tenth 40.1 s;
  the default scraper took 1.7 s and 4.9 s. The `gptr-via-*` latencies include time queued behind
  GPT Researcher's own Firecrawl concurrency limit (`FIRECRAWL_CONCURRENCY=4`), so don't compare them.
- travelocity.com is dropped in every GPT Researcher arm by its own URL check (a DNS failure at the
  time), before any scraper runs.

## What this run changed in Frankensurf

The first runs through `serve` honoured Firecrawl's `onlyMainContent` (the Python SDK sends `true` by
default) with Frankensurf's main-content cut. That cut is built for articles: on search and listing
pages it kept a few lines and dropped the results. With the cut always on, 90 right and 46 wrong;
with the cut used only when it kept 30% of the page, 126 and 11
(`gptr-via-serve-article-cut.jsonl`). `serve` now ignores `onlyMainContent` and returns the whole
page, which gives the 134 above.

## Files

- `gptr-default-bs.jsonl`, `gptr-via-frankensurf-serve.jsonl`, `frankensurf-direct-free.jsonl`,
  `gptr-via-serve-article-cut.jsonl`: one row per site, with the verdict (`right`,
  `wrong_page_passed_on`, `no_page`), the check's reason, text length and latency. Page text isn't
  kept.
- `scripts/`: `gptr_scrape.py` (GPT Researcher's Scraper over the set; `ICP_SCRAPER=firecrawl` with
  `FIRECRAWL_SERVER_URL` for the `serve` arm), `fs_read.py` (the direct arm), `grade.py`, `pack.py`.
  Run from the repo root.
