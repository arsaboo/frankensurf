Frankensurf
===========

**One read() call for AI agents, stitched from 28 web tools.**

Every web tool breaks somewhere. Frankensurf sits in front of all of them and
gives your agent one call. It tries the cheapest tool first, checks the page is
real, climbs to a stronger tool only when a site pushes back, and returns clean
page data with a receipt of how it got there.

- **Cheapest first.** Plain HTTP, Jina Reader, a local browser, free stealth
  browsers, hosted browsers, then paid unblockers only if you allow them.
- **Every page checked.** A challenge page, a login wall, or a search page whose
  results never loaded counts as a failure, not a success.
- **Shows its work.** Every result says which tools ran, how long each took and
  what it cost.

Website and docs: https://frankensurf.dev ·
`Why I built it <https://frankensurf.dev/blog/i-just-wanted-a-cheap-film-camera/>`_

Try it
------

Python 3.12 or newer, on Linux or Windows through WSL (macOS should work but
isn't tested yet)::

  pip install "frankensurf[mcp]"
  frankensurf setup        # Chromium + the free stealth browsers, a few minutes
  frankensurf read "https://www.walmart.com/search?q=air+fryer" --explain

No account or key needed. The read climbs until it gets the real page::

  https://www.walmart.com/search?q=air+fryer
    ✗ http                   CAPTCHA                0.4s
    ✗ local                  CAPTCHA                1.5s
    ✓ camoufox               got the page           15.6s

  air fryer - Walmart.com
  24,192 characters via camoufox, complete (107 results), free

Drop ``--explain`` for the full JSON: text or markdown, JSON-LD, the page's
embedded and fetched JSON, images, and the receipt.

Free or paid
------------

Everything works with no keys. Paid tools are opt-in, and only come in once a
site has walled a free tool, cheapest likely success first. From the benchmark below (152 sites picked before any was read):

=========================================  ==============  =============
Setup                                      Sites read      Cost per 1k
=========================================  ==============  =============
Free tools only (``frankensurf setup``)    73.7%           $0
Plus paid fallbacks (Firecrawl, Zyte, …)   89.5%           $0.73
=========================================  ==============  =============

To allow paid tools, set their keys (for example ``FIRECRAWL_API_KEY``) and pass
``allow_paid_fallbacks``. See
`Paid tools and budgets <https://frankensurf.dev/docs/blocked/paid-tools/>`_.

Use it
------

From an agent, as an MCP server
(`setup for each client <https://frankensurf.dev/docs/agents/>`_)::

  claude mcp add frankensurf -- frankensurf-mcp

From Python::

  from frankensurf import Runtime

  async with Runtime("state") as web:
      page = await web.read("https://example.com", policy_overrides={"prefer_markdown": True})
      hits = await web.search("playwright python tutorial")

From the command line::

  frankensurf read https://example.com --markdown
  frankensurf search playwright python tutorial
  frankensurf watch https://news.ycombinator.com/ --link-pattern 'item\?id=\d+'

What you get back is raw page data, not answers. Your agent decides what the page
means, and if it isn't what it wanted, it can ask again with
``retry_of=<trace_id>`` to skip every tool already tried.

Also in the box: profiles (log in once, reuse the session from any tool), human
handoff for CAPTCHAs and 2FA, site modules (save what your agent learns about a
site as data), and Web Bot Auth request signing.

Want the article, not the menus? Pass ``--main`` (MCP ``main_content=True``)
for ``main_text`` (best with ``pip install "frankensurf[main]"``).

Search takes ``--site``, ``--exclude-domain``, ``--recency`` and ``--region`` on
every source, a ``--vertical`` (news, reference, discussions, qa, code, papers,
books) backed by official free APIs, and ``--mode merge`` to fuse every source
at once. Running on a server that reads URLs it didn't choose? Set
``FRANKENSURF_BLOCK_PRIVATE_NETWORK=1``.

Every page read carries ``receipt.quality``: ``good``, ``partial`` or ``poor``,
with flags such as ``paywall``, ``menus`` or ``placeholder``, so an agent (or a
paid fallback) knows when a page didn't really come through.

Slow page? A read with no page after 3 seconds starts two more free tools beside
it and keeps the first complete page (``--lightning`` races from the start). A
URL that answers not found is looked up with the site's own search, and
``receipt.rescued`` says so. Still walled? Allow a real Chrome on your machine
(``--real-browser``, with its own profile, never yours) or a second connection
(``FRANKENSURF_EGRESS``; ``frankensurf egress check`` shows where each comes out).

Keeping up with many sites? ``frankensurf watch-sites URL… --since DATE`` lists
their new pages from the feeds and sitemaps they publish, polled with
conditional requests. Running on a server? Run Steel beside it
(``docker compose up -d``) and, if sites still refuse your data-centre address,
set ``FRANKENSURF_PROXY`` to a proxy you bring.

Agents learn the tools from a bundled skill: the MCP server sends it as its
instructions, and ``frankensurf skill install`` adds it to ``~/.claude/skills``.

Reading many pages from one site? Let Frankensurf learn it first::

    frankensurf module discover https://www.example.com/ --query "desk lamp" --save

That finds the site's search (its schema.org ``SearchAction``, its search form,
or by typing once into its search box), reads the results, and saves a site
module: where the items live in the page's own data, plus a search template.
From then on every search on that site comes back as structured ``items``, and
``batch_template`` runs many searches as one polite batch. Check the sample rows
before saving; the draft can pick the wrong list.

How it works
------------

Every way to fetch a page is a rung: plain HTTP and markdown negotiation,
browsers, stealth browsers and signed requests, hosted browsers, paid unblockers,
and finally a person clearing the wall in a visible browser. A read starts at the
cheapest rung and climbs only when a page pushes back. Every tool is a plugin, so
new ones slot in as another rung. See
`How escalation works <https://frankensurf.dev/docs/blocked/escalation/>`_
and the `full list of integrations <https://frankensurf.dev/docs/integrations/>`_.

Benchmark
---------

``scripts/toolbench.py`` runs each tool on its own, then Frankensurf, on the same
pages with the same content checks. 152 sites picked before any was read, one run
on 6 October 2026, raw rows in ``benchmarks/2026-10-06/``:

==========================  ===========
Tool                        Sites read
==========================  ===========
Headless Chromium only      32.2%
Plain HTTP only             46.1%
Camoufox only               48.7%
Scrapling only              53.3%
ZenRows only                55.3%
Jina Reader only            63.8%
Zyte only                   73.7%
Firecrawl only              80.3%
**Frankensurf**             **89.5%**
==========================  ===========

Frankensurf read 19 sites Firecrawl missed and Firecrawl read 5 Frankensurf
missed. Frankensurf costs a fifth as much per page, but is slower: median 9.5 s
against 5.6 s. Scrapfly is left out because its free plan ran out mid-run.

A second fresh set, 139 different sites picked on 9 October before any was read
(raw rows in ``benchmarks/2026-10-09/``):

==========================================  ===========  ========
Tool                                        Sites read   Median
==========================================  ===========  ========
Firecrawl only                              74.1%        6.6 s
Frankensurf, free tools only                81.3%        5.6 s
**Frankensurf, with paid fallbacks**        **88.5%**    9.2 s
==========================================  ===========  ========

Frankensurf read 22 sites Firecrawl missed; Firecrawl read 2 Frankensurf
missed. Firecrawl's rate-limited pages were re-run one at a time until none
were left. Racing the free tools (v0.26) then took the free tier to 82.0%,
with its median down to 4.5 s and its p90 from 50 s to 37 s. See
`Benchmarks <https://frankensurf.dev/docs/more/benchmarks/>`_.

Known walls
-----------

It's an alpha, and some sites still win:

- **Need the paid tools:** in a free-only spot check on 9 October, Etsy, Home
  Depot, Yelp, Crunchbase and G2 blocked every free tool.
- **Beat everything so far:** Shopee, Temu and Idealista.
- **Social sites without a login:** LinkedIn, X, YouTube, Threads, Bluesky and
  Reddit read fine. Instagram, TikTok, Facebook and Pinterest show only the public
  shell (name, bio, counts); the posts need a login.
- **Logged-in pages** (your feeds, your orders) need a
  `profile <https://frankensurf.dev/docs/read/profiles/>`_.
- **Slower than one tool:** a read that climbs several rungs takes 10–30 s.

Found a site it can't read?
`Report it <https://github.com/yail259/frankensurf/issues/new?template=site-it-cant-read.yml>`_;
every report becomes a test case.

Develop
-------

From a clone::

  python3 -m venv .venv
  .venv/bin/pip install -e '.[test,mcp]'
  .venv/bin/frankensurf setup
  PYTHONPATH=.:src .venv/bin/pytest -q

The product design is in ``SPEC.rst``; contributor rules are in ``AGENTS.md``.
New tools are plugins, never branches in Core; see
`Writing a plugin <https://frankensurf.dev/docs/more/plugins/>`_. The
website and docs live in ``site/``.

Licence
-------

Apache-2.0. See ``LICENSE``.
