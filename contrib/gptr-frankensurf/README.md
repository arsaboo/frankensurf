# gptr-frankensurf

Frankensurf search as a [GPT Researcher](https://github.com/assafelovic/gpt-researcher) retriever
plugin, the way GPT Researcher asks new search providers to ship.

```bash
pip install "gptr-frankensurf @ git+https://github.com/yail259/frankensurf#subdirectory=contrib/gptr-frankensurf"
export RETRIEVER=frankensurf          # or frankensurf,duckduckgo
```

Every result is checked for topic, kind of page and date before GPT Researcher sees it. Free search
sources only, unless `FRANKENSURF_GPTR_PAID=1` (with `FRANKENSURF_GPTR_MAX_COST` to cap a search, in
US dollars). A domain filter runs one search per domain.

For the pages themselves, point GPT Researcher's scraper at `frankensurf serve`:

```bash
frankensurf serve &
export SCRAPER=firecrawl FIRECRAWL_SERVER_URL=http://127.0.0.1:3002 FIRECRAWL_API_KEY=local
```

On 150 blind sites, GPT Researcher's default scraper got 58 pages right and Frankensurf 134
([rows](../../benchmarks/2026-10-16-gpt-researcher)). Docs: https://frankensurf.dev/docs/firecrawl-compatible/
