<!-- mcp-name: io.github.yail259/frankensurf -->

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/yail259/frankensurf/main/site/src/assets/stitched-wave-mint.svg">
    <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/yail259/frankensurf/main/site/src/assets/stitched-wave-ink.svg">
    <img src="https://raw.githubusercontent.com/yail259/frankensurf/main/site/src/assets/stitched-wave-ink.svg" alt="Frankensurf" width="120">
  </picture>
</p>

<h1 align="center">Frankensurf</h1>

<p align="center">
  <strong>One <code>read()</code> call for AI agents, stitched from 28 web tools.</strong><br>
  Cheapest tool first, every page checked, a receipt for every attempt.
</p>

<p align="center">
  <a href="https://pypi.org/project/frankensurf/"><img alt="PyPI" src="https://img.shields.io/pypi/v/frankensurf?style=flat-square&logo=pypi&logoColor=white&label=pypi&color=blue"></a>
  <a href="https://pypi.org/project/frankensurf/"><img alt="Python" src="https://img.shields.io/pypi/pyversions/frankensurf?style=flat-square&logo=python&logoColor=white"></a>
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/badge/license-Apache--2.0-green?style=flat-square"></a>
  <a href="https://github.com/yail259/frankensurf/stargazers"><img alt="GitHub stars" src="https://img.shields.io/github/stars/yail259/frankensurf?style=flat-square&logo=github&color=lightgrey"></a>
  <a href="https://frankensurf.dev/docs/agents/"><img alt="MCP server" src="https://img.shields.io/badge/MCP-server-8A2BE2?style=flat-square"></a>
</p>

<p align="center">
  <a href="https://frankensurf.dev"><strong>Website</strong></a> ·
  <a href="https://frankensurf.dev/docs/"><strong>Docs</strong></a> ·
  <a href="#quick-start"><strong>Quick start</strong></a> ·
  <a href="#benchmark"><strong>Benchmark</strong></a> ·
  <a href="https://frankensurf.dev/blog/i-just-wanted-a-cheap-film-camera/"><strong>Why I built it</strong></a>
</p>

---

Every web tool breaks somewhere, and they break on different sites. Frankensurf sits in front of
all of them and gives your agent one call. It tries the cheapest tool first, checks the page is
real, moves to a stronger tool only when a site pushes back, and returns clean page data with a
receipt of how it got there.

<table>
  <tr>
    <td width="33%" valign="top"><strong>Cheapest first</strong><br><sub>Plain HTTP, Jina Reader, a local browser, free stealth browsers, hosted browsers, then paid APIs only if you allow them.</sub></td>
    <td width="33%" valign="top"><strong>Every page checked</strong><br><sub>A challenge page, a login wall or a search page whose results never loaded counts as a failure, not a success.</sub></td>
    <td width="33%" valign="top"><strong>Shows its work</strong><br><sub>Every result says which tools ran, how long each took, what it cost, and why it stopped.</sub></td>
  </tr>
</table>

<p align="center">
  <img src="https://frankensurf.dev/blog/head-to-head-set7.png" alt="Share of 150 sites read correctly: Frankensurf 96%, Zyte 86%, Firecrawl 85%, free tools only 88%" width="820">
</p>

## Quick start

Python 3.12+, on Linux, macOS, or Windows through WSL. No account or key needed.

```bash
pip install "frankensurf[mcp]"
frankensurf install        # every free tool: browsers, stealth browsers, local search, Crawl4AI
frankensurf read "https://www.walmart.com/search?q=air+fryer" --explain
```

The read climbs until it gets the real page:

```text
https://www.walmart.com/search?q=air+fryer
  ✗ http                   CAPTCHA                0.4s
  ✗ local                  CAPTCHA                1.5s
  ✓ camoufox               got the page           15.6s

air fryer - Walmart.com
24,192 characters via camoufox, complete (107 results), free
```

`frankensurf install` works like `playwright install`: name just the tools you want
(`frankensurf install search chromium`), add `--with-deps` for the browsers' system libraries on
Linux, `--list` to see what's installed where, and `frankensurf uninstall all` to remove it. No
Docker needed. Each release is installed from PyPI on fresh Ubuntu and macOS machines and checked.

## Use it

**From an agent**, as an MCP server ([setup for each client](https://frankensurf.dev/docs/agents/)):

```bash
claude mcp add frankensurf -- frankensurf-mcp
```

**From Python:**

```python
from frankensurf import Runtime

async with Runtime("state") as web:
    page = await web.read("https://example.com", policy_overrides={"prefer_markdown": True})
    hits = await web.search("playwright python tutorial")
```

**From the command line:**

```bash
frankensurf read https://example.com --markdown
frankensurf search playwright python tutorial --recency week
frankensurf watch https://news.ycombinator.com/ --link-pattern 'item\?id=\d+'
```

**In Claude Code as a plugin** (the MCP server plus the skill that teaches Claude to use it):

```
/plugin marketplace add yail259/frankensurf
/plugin install frankensurf@frankensurf
```

**From LangChain, the OpenAI Agents SDK or CrewAI** (`pip install "frankensurf[langchain]"`, and so on):

```python
from frankensurf.agent_tools import langchain_tools   # or openai_agents_tools, crewai_tools
tools = langchain_tools()                              # frankensurf_read, frankensurf_search
```

You get raw page data, not answers: text or markdown, JSON-LD, the page's own JSON, images, and
the receipt. Your agent decides what the page means. If it isn't what it wanted, it asks again with
`retry_of=<trace_id>` and Frankensurf skips every tool it already tried.

## Benchmark

`scripts/toolbench.py` runs each tool on its own, then Frankensurf, on the same pages with the same
content checks. The newest set: 150 sites in 15 categories, picked before any was read, read once
(11 October 2026, raw rows in [`benchmarks/2026-10-11/`](benchmarks/2026-10-11/)).

| Tool | Sites read | Median | Cost per 1k |
| --- | ---: | ---: | ---: |
| Plain HTTP only | 48.7% | 1.2 s | free |
| Camoufox only | 68.7% | 10.9 s | free |
| Jina Reader only | 73.3% | 1.5 s | free |
| Firecrawl only | 84.7% | 4.6 s | $4.28 |
| Zyte only | 86.0% | 10.7 s | not reported |
| Frankensurf, free tools only | 88.0% | 3.6 s | $0 |
| **Frankensurf, with paid fallbacks** | **96.0%** | **2.9 s** | **$0.98** |

- Frankensurf read 17 sites Firecrawl missed; Firecrawl read none it missed.
- With no paid tools at all it is level with the best paid tool on its own (88.0% against 86.0% and
  84.7%; at 150 sites that gap is within the noise).
- Its slowest reads are slower: the slowest tenth takes 25.7 s against Firecrawl's 10.6 s, when a
  read climbs several tools.
- Not a one-off: 95.3% vs 85.3% on the set the day before; 89.5% vs 80.3% and 88.5% vs 74.1% on
  earlier fresh sets.

<details>
<summary>Every tool on the same 150 sites</summary>

| Tool | Sites read | Median | Cost per 1k |
| --- | ---: | ---: | ---: |
| Patchright only | 38.7% | 4.1 s | free |
| Headless Chromium only | 40.7% | 2.7 s | free |
| Plain HTTP only | 48.7% | 1.2 s | free |
| Camoufox only | 68.7% | 10.9 s | free |
| Scrapling only | 68.7% | 8.5 s | free |
| Jina Reader only | 73.3% | 1.5 s | free |
| Firecrawl only | 84.7% | 4.6 s | $4.28 |
| Zyte only | 86.0% | 10.7 s | not reported |
| Frankensurf, free tools only | 88.0% | 3.6 s | $0 |
| Frankensurf, with paid fallbacks | 96.0% | 2.9 s | $0.98 |

Medians are over pages read; cost is per 1,000 pages read right. ZenRows and Scrapfly are left out
(their plans ran out of credits). From a fresh cloud VM, whose data-centre address sites wall more
often: free tools 82.0%, lightning mode 84.0% at a 1.2 s median.
See [Benchmarks](https://frankensurf.dev/docs/more/benchmarks/).
</details>

## Search

`search` uses free sources first and checks every result: on topic, the right kind of page, inside
the date window. Exa, Brave, Tavily, Serper and other paid sources join only when you allow paid
tools. Judged blind (results pooled and shuffled, which tool found them hidden), useful results in
the top five:

| Blind set 7 (120 queries) | Useful results (of 5) | Cost per 1k |
| --- | ---: | ---: |
| Frankensurf, paid allowed | 4.67 | $9.38 |
| Exa only | 4.66 | $9.50 |
| Frankensurf, free (with the local search engine) | 3.83 | $0 |

Free, it beats Firecrawl's search: better on 35 queries, worse on 5. With paid allowed it is level
with Exa. The local search engine (`frankensurf install search`, SearXNG without Docker) makes the
free tier much stronger. v0.41 improved free search again: on a fresh blind set of 120
queries it beat v0.40 on 41 and lost on 12 (3.42 against 2.71 useful results of 5,
[`benchmarks/2026-10-14-search-v041/`](benchmarks/2026-10-14-search-v041/README.md)). Details in
[`benchmarks/2026-10-13-search/`](benchmarks/2026-10-13-search/README.md).

## What else is in the box

<details>
<summary><strong>Reading</strong>: quality grades, main content, racing slow reads, PDFs</summary>

- Every read carries `receipt.quality`: `good`, `partial` or `poor`, with flags such as `paywall`,
  `menus` or `placeholder`, so an agent knows when a page didn't really come through.
- `--schema product.json` (MCP `schema`) fills a JSON Schema from the page's JSON-LD, microdata,
  meta tags and own JSON into `extracted`, and climbs to a stronger tool while a required field is
  missing ([Structured fields](https://frankensurf.dev/docs/read/structured/)).
- `--main` (MCP `main_content=True`) adds `main_text`: the article without menus, footers and
  banners (best with `pip install "frankensurf[main]"`).
- A read with no page after 3 seconds starts two more free tools beside it and keeps the first
  complete page (`--lightning` races from the start).
- A URL that answers "not found" is looked up with the site's own search; `receipt.rescued` says so.
- Still walled? Allow a real Chrome on your machine (`--real-browser`, with its own profile, never
  yours) or a second connection (`FRANKENSURF_EGRESS`).
</details>

<details>
<summary><strong>Search</strong>: verticals, boolean queries, local engine</summary>

- `--site`, `--exclude-domain`, `--recency` and `--region` on every source, and a `--vertical`
  (news, reference, discussions, qa, code, papers, books) backed by official free APIs.
- Monitoring-style queries (`"Acme" OR "Acme Corp" (acquisition OR layoffs)`) are judged by the
  name they're about, and a weak answer is retried once with just the name.
- `--mode merge` asks every source at once and fuses the results.
- `frankensurf search-engine start|stop|status` manages the local SearXNG; search restarts it after
  a reboot.
</details>

<details>
<summary><strong>Agents</strong>: skill, profiles, handoff, site modules</summary>

- A bundled skill teaches agents the tools: the MCP server sends it as its instructions, and
  `frankensurf skill install` adds it to `~/.claude/skills`.
- **Profiles:** log in once and any tool can use the session; the model never sees cookies or keys.
- **Human handoff:** for a CAPTCHA or 2FA only a person can clear, it opens the page for you, then
  carries on.
- **Site modules:** save what your agent learns about a site as data. `frankensurf module discover
  URL --query "desk lamp" --save` finds the site's search, reads the results and saves a module;
  after that, searches on that site come back as structured `items`.
- **Shared site knowledge:** `frankensurf share sync` starts from which tool reads which site, as the
  community learned it; `share export` writes yours to review and contribute (opt in both ways).
- `frankensurf watch-sites URL… --since DATE` lists new pages from the feeds and sitemaps sites
  publish.
</details>

<details>
<summary><strong>Paid tools and budgets</strong></summary>

Everything works with no keys. Paid tools are opt-in and only come in once a site has walled a free
tool, cheapest likely success first. Set their keys (for example `FIRECRAWL_API_KEY`) and pass
`--paid`; `--max-cost USD` caps a read. From Python: `allow_paid_fallbacks` and `max_cost_usd`.
See [Paid tools and budgets](https://frankensurf.dev/docs/blocked/paid-tools/).
</details>

## How it works

Every way to fetch a page is a rung: plain HTTP and markdown negotiation, browsers, stealth
browsers and signed requests, hosted browsers, paid APIs, and finally a person clearing the wall in
a visible browser. A read starts at the cheapest rung and climbs only when a page pushes back. Every
tool is a plugin, so new ones slot in as another rung.

<p align="center">
  <img src="https://frankensurf.dev/blog/fallback-chain.png" alt="One read of Crunchbase: plain HTTP, headless Chrome, Steel and Camoufox were blocked; Firecrawl got through" width="760">
</p>

See [How escalation works](https://frankensurf.dev/docs/blocked/escalation/) and the
[full list of integrations](https://frankensurf.dev/docs/integrations/).

## Known walls

It's an alpha, and some sites still win:

- **Need the paid tools:** in a free-only spot check, Etsy, Home Depot, Yelp, Crunchbase and G2
  blocked every free tool.
- **Beat everything so far:** Shopee, Temu and Idealista.
- **Social sites without a login:** LinkedIn, X, YouTube, Threads, Bluesky and Reddit read fine.
  Instagram, TikTok, Facebook and Pinterest show only the public shell; the posts need a login.
- **Logged-in pages** (your feeds, your orders) need a
  [profile](https://frankensurf.dev/docs/read/profiles/).
- **A slow tail:** most reads take a few seconds, but one in ten climbs several tools and takes 25 s
  or more.

Found a site it can't read?
[Report it](https://github.com/yail259/frankensurf/issues/new?template=site-it-cant-read.yml); every
report becomes a test case.

## Develop

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test,mcp]'
.venv/bin/frankensurf install
PYTHONPATH=.:src .venv/bin/pytest -q
```

The product design is in `SPEC.rst` and contributor rules in `AGENTS.md`. New tools are plugins,
never branches in Core: see [Writing a plugin](https://frankensurf.dev/docs/more/plugins/) and the
example in `examples/frankensurf-plugin-example`. The website and docs live in `site/`.

## Licence

Apache-2.0. See [`LICENSE`](LICENSE).
