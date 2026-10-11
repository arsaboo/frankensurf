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

Python 3.12 or newer, on Linux, macOS, or Windows through WSL (each release is
installed from PyPI on fresh Ubuntu 24.04 and macOS machines and checked)::

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
site has walled a free tool, cheapest likely success first. From the newest
benchmark below (150 sites picked before any was read, read once):

=========================================  ==============  ========  =============
Setup                                      Sites read      Median    Cost per 1k
=========================================  ==============  ========  =============
Free tools only (``frankensurf setup``)    88.0%           3.6 s     $0
Plus paid fallbacks (Firecrawl, Zyte, …)   96.0%           2.9 s     $0.98
=========================================  ==============  ========  =============

With no paid tools at all, it reads as many sites as the best paid tool on its
own (88.0% against Zyte's 86.0% and Firecrawl's 84.7%; at 150 sites that gap
is within the noise).

To allow paid tools, set their keys (for example ``FIRECRAWL_API_KEY``) and pass
``--paid`` (``--max-cost USD`` caps a read; from Python, ``allow_paid_fallbacks``
and ``max_cost_usd``). See
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
pages with the same content checks. The newest: 150 sites in 15 categories
picked before any was read and read once (held-out set 7, 11 October 2026,
v0.35), raw rows in ``benchmarks/2026-10-11/``:

====================================  ===========  ========  =============
Tool                                  Sites read   Median    Cost per 1k
====================================  ===========  ========  =============
Patchright only                       38.7%        4.1 s     free
Headless Chromium only                40.7%        2.7 s     free
Plain HTTP only                       48.7%        1.2 s     free
Camoufox only                         68.7%        10.9 s    free
Scrapling only                        68.7%        8.5 s     free
Jina Reader only                      73.3%        1.5 s     free
Firecrawl only                        84.7%        4.6 s     $4.28
Zyte only                             86.0%        10.7 s    not reported
Frankensurf, free tools only          88.0%        3.6 s     $0
**Frankensurf, with paid fallbacks**  **96.0%**    2.9 s     $0.98
====================================  ===========  ========  =============

Frankensurf with paid fallbacks read 17 sites Firecrawl missed; Firecrawl read
none it missed. Of the 146 sites any tool read, it got 98.6%. Its median read
is faster than Firecrawl's, but its slowest reads are slower: p90 25.7 s
against 10.6 s, when a read climbs several tools. Medians are over pages read;
cost is per 1,000 pages read right. ZenRows is left out (its plan ran out of
credits), as is Scrapfly. From a fresh cloud VM, a data-centre address sites
wall more often: free tools 82.0%, lightning mode 84.0% at a 1.2 s median.

The set before it (set 6, 10 October, every tool alone the same way): 95.3%
with paid fallbacks against Firecrawl's 85.3% and Zyte's 86.0%. Earlier fresh
sets: 152 sites on 6 October (v0.16) 89.5% against Firecrawl's 80.3%; 139 sites
on 9 October (v0.26) 88.5% against 74.1%. See
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
- **A slow tail:** most reads take a few seconds, but one in ten climbs several
  tools and takes 25 s or more.

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
`Writing a plugin <https://frankensurf.dev/docs/more/plugins/>`_ for building,
installing and trusting your own (``frankensurf plugins``), with a working
example in ``examples/frankensurf-plugin-example``. The
website and docs live in ``site/``.

Licence
-------

Apache-2.0. See ``LICENSE``.
