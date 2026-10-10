# Benchmarks, 10 October 2026: held-out set 5

- `scripts/toolbench-heldout5.json` (built by `scripts/make_heldout5.py`): 150
  sites chosen from general knowledge before any of their pages was read. Every
  host and every brand on an earlier set or spot check (519 hosts) was excluded
  automatically, and the 150 were taken round-robin across 15 categories (US,
  AU/NZ, EU and other retail, classifieds, jobs, property, travel, news,
  government, media, food, cars, developer sites and social).
- Some URLs were guessed wrong (Adore Beauty, Cotswold, Seat61 and Lastminute
  answer 404 to every tool, including after their walls); that counts against
  every arm alike.

## First run (blind), v0.30.0 code

`toolbench-heldout5-v0.30.jsonl` (home connection) and
`toolbench-heldout5-free-azure-v0.30.jsonl` (free arm on an Azure VM):

| Arm | Sites read | Median | p90 | Reported cost |
|---|---|---|---|---|
| FrankenSurf, paid fallbacks | 130/150 (86.7%) | 5.1 s | 59.6 s | $0.068 |
| FrankenSurf, free only | 120/150 (80.0%) | 4.9 s | 24.1 s | $0 |
| FrankenSurf, free only, Azure VM | 115/150 (76.7%) | | | $0 |
| Firecrawl alone | 113/150 (75.3%) | 4.3 s | 8.5 s | $0.555 |

131 sites were read by at least one arm; FrankenSurf with paid fallbacks got
130 of them (99.2%), Firecrawl 113 (86.3%). FrankenSurf alone read 18 sites
Firecrawl missed; Firecrawl alone read 1. Firecrawl's single rate-limited page
was re-run one at a time.

Every news, government, property, media and developer site passed. The misses
were wrong URLs (above), bot walls (mobile.de, Avito, Bass Pro, Strava's
sign-in), and search pages that came back without their results.

After this run the set is no longer blind; later runs on it measure changes.

## Iteration 1 (in-sample)

`toolbench-heldout5-free-iter1.jsonl`: the free arm after fixes found on this set:
walls named in more languages (mobile.de, Avito), off-query pages from plain
HTTP getting one rendered read, consent-led pages getting the interact step,
search words in the path, a 404 after a rate-limit checkpoint getting a
confirming read, content inside open shadow roots, and browser error and
maintenance pages no longer counted as pages. 125/150 (83.3%) against
120/150: Wotif, Rays Outdoors, Bass Pro, Sierra and OpenTable gained, none
lost. Median 7.1 s against 4.9 s (escalations picked different winners; the
plain-HTTP fast path was unchanged at 53 reads).

## Iteration 2 (in-sample)

`toolbench-heldout5-free-iter2.jsonl`: escalation stops once two more tools read
the same page as the first (`completeness_stop_on_agreement`), and search
results are recognised by shape (many links under one path prefix whose text
mentions the query) when they do not look like product links.

| Free arm | Sites read | Median (read) | p75 | p90 | p90 (all reads) |
|---|---|---|---|---|---|
| Blind, v0.30.0 | 120/150 | 4.9 s | 10.6 s | 24.1 s | 29.4 s |
| Iteration 1 | 125/150 | 7.1 s | 16.6 s | 28.5 s | 40.6 s |
| Iteration 2 | 127/150 | 4.5 s | 12.0 s | 26.1 s | 44.6 s |

Against the blind run: Wotif, Rays Outdoors, Dubizzle, JobServe, B&Q, Sierra
and OpenTable gained, none lost (Bass Pro passed in iteration 1 and was walled
again here). Of the 23 misses, 8 are URLs that answered 404 to every arm in
the blind run, 7 are hard walls (CAPTCHA or BLOCKED), 1 needs a sign-in, and 7
loaded without the expected content (every arm missed 6 of those 7; only
archive.org was read by another arm). Reads that fail now climb further before
giving up, which is where the all-reads p90 went.

## Learned wall routing (out of sample)

`toolbench-heldout5-free-learned.jsonl`: the free arm read held-out set 4 first
(training), which left `wall-stats-trained-on-heldout4.json`: for each wall
vendor named by response headers, which tools got past it. Then it read
held-out set 5 with that state. No site is on both sets, so only the vendor
statistics carry over.

- Training settled on two moves: Steel first behind Akamai, Camoufox first
  behind DataDome. Behind Cloudflare no tool reached even odds (Steel 8 of 22,
  Camoufox 4 of 14); the local browser was 0 of 20, and four other tools 0 of 8.
  Learning carried on through the test run, and by its end Steel was through
  Cloudflare 21 times in 41, the local browser 0 in 36, and five tools 0 in 12.
- On set 5 the learned order fired on 10 of the 150 reads: 7 came through
  (Kickstarter, EB Games and RedBook on the second attempt), and 3 stayed
  walled (mobile.de, Immobiliare, Mighty Ape).
- Overall 124/150 against 127/150 for iteration 2, median 6.4 s against 4.5 s.
  The three sites lost (Dubizzle and JobServe missing content, B&Q failing at
  Jina Reader) never met a learned move, so this is run-to-run variation, not a
  gain or a loss from learning. Learned routing stays: it only reorders after
  at least 3 tries at even odds, and it needs more reads than one set gives.

## Iteration 3 (in-sample): tools that fetch from elsewhere

Across every free run since the hedge, all wins after the sixth try came from
Jina Reader, which fetches from its own network and sat last on the ladder.
`toolbench-heldout5-free-iter3.jsonl`: after 4 walls, remote tools move ahead
(`ProviderManifest.remote`), and an observed hedge page stands in when the main
read fails. Fresh state, like iteration 2.

| Free arm | Sites read | Median (read) | p75 | p90 | p90 (all reads) | Total time |
|---|---|---|---|---|---|---|
| Iteration 2 | 127/150 | 4.5 s | 12.0 s | 26.1 s | 44.6 s | 1,999 s |
| Iteration 3 | 126/150 | 4.3 s | 10.4 s | 23.4 s | 34.4 s | 1,790 s |

Bass Pro gained; JobServe (a plain-HTTP page missing its content) and B&Q (Jina
Reader down after 21 tries) lost. Against the blind run: 6 gained, none lost.
Two problems showed up in the traces: walled reads now ended on a down tool
and reported `PROVIDER_DOWN` instead of the wall, and OpenTable failed 13
tries without one wall (timeouts, unrendered pages) before Jina Reader read
it in 0.4 s, so the wall-only trigger never fired. Browser Use, configured on
this machine, failed every try in 18 s with `INVALID_URL`: the address its
agent ended on was not a web page.

## Iteration 4 (in-sample)

`toolbench-heldout5-free-iter4.jsonl`: failed tries of any kind (not only
walls) move remote tools ahead; tools that never got past a wall vendor in 8
tries go last; a read whose last tool was down reports what the last working
tool met; Browser Use reports `PROVIDER_DOWN` when its agent never reached the
page. Fresh state.

| Free arm | Sites read | Median (read) | p75 | p90 | p90 (all reads) | Total time |
|---|---|---|---|---|---|---|
| Iteration 3 | 126/150 | 4.3 s | 10.4 s | 23.4 s | 34.4 s | 1,790 s |
| Iteration 4 | 125/150 | 5.3 s | 11.5 s | 22.9 s | 41.1 s | 2,162 s |

Immobiliare gained: every tool on the ladder met DataDome, then the home-page
warm-up (Camoufox entering through the home page) read it in 12 s. Croma (read
in iterations 2 and 3; its results did not render this time) and Bass Pro (it
passes in some runs and not others) lost. Against the blind run: 6 gained, 1
lost.

Walled reads now end with the wall's own code (`CAPTCHA`, `BLOCKED`), which
also gives them the warm-up they were meant to get. The run was slower for
another reason: Browser Use's new `PROVIDER_DOWN` is a code the ladder retries,
so its 18-second failure ran twice on every walled read (40 tries, about 340 s
more than iteration 3). It now reports `PROVIDER_UNAVAILABLE`, which is not
retried.

## Iteration 5 (in-sample)

`toolbench-heldout5-free-iter5.jsonl`: Browser Use's never-reached failure is
not retried. Fresh state.

| Free arm | Sites read | Median (read) | p75 | p90 | p90 (all reads) | Total time |
|---|---|---|---|---|---|---|
| Blind, v0.30.0 | 120/150 | 4.9 s | 10.6 s | 24.1 s | 29.4 s | 1,730 s |
| Iteration 2 | 127/150 | 4.5 s | 12.0 s | 26.1 s | 44.6 s | 1,999 s |
| Iteration 3 | 126/150 | 4.3 s | 10.4 s | 23.4 s | 34.4 s | 1,790 s |
| Iteration 4 | 125/150 | 5.3 s | 11.5 s | 22.9 s | 41.1 s | 2,162 s |
| Iteration 5 | 124/150 | 4.0 s | 11.4 s | 24.3 s | 39.2 s | 1,958 s |

Against iteration 4, Immobiliare was lost (the warm-up that read it there met
DataDome this time). Against the blind run: Wotif, Rays Outdoors, Dubizzle,
Sierra and OpenTable gained; Croma lost.

B&Q failed with `PROVIDER_DOWN` from every tool, plain HTTP and Jina Reader
included, in runs 3, 4 and 5: the site refused connections from here, after
six runs of about 20 tries each. Each tool's outage was retried, 21 tries over
110 s. Now, once two different tools on an automatic route fail the same
retryable way, the rest get one try each: a read of B&Q afterwards failed in
54 s over 12 tries.

## Where this leaves the free tier

Since iteration 2 the free arm has read 124 to 127 of 150 on every run; the
differences are single sites that pass in some runs and not others (Bass Pro,
Croma, Immobiliare, JobServe, B&Q). The 26 misses in iteration 5:

- 8 URLs that answered 404 to every arm in the blind run (7 still do; Food52
  now answers a page without the expected items);
- 8 walls: Mighty Ape, Carousell, PrimeLocation and Jiji, which only paid tools
  have read; Immobiliare and B&Q, which paid tools and one free run each read;
  Bass Pro, read by two free runs and no paid one; and Avito, which no arm has
  read (a rate limit this time);
- 1 sign-in wall (Strava);
- 9 pages read without the expected content: CareerOne, Mecca, mobile.de,
  Superdrug, Welcome to the Jungle and Bukalapak, which every arm missed in
  the blind run; archive.org, which paid tools read; Croma, read in six runs
  and missed in the last two; and JobServe, read only in iteration 2.

The paid arm read 130 of the 131 pages any arm read on the blind run. What is
left for the free tier is hard walls, which paid unblockers or a person clear
(`allow_paid_fallbacks`, handoff), and pages whose results never load for any
tool.

The slowest successful reads are search pages that every tool reads without
result links (Bandcamp, Substack, Twitch, Croma: their results load from
scripts; pub.dev: 10 package links, but only 2 names contain the query, too
few to tell them from a menu). The climb tries four tools and keeps the best,
20 to 50 s, bounded by `completeness_deadline_seconds`.

## Lightning mode (v0.31)

Lightning starts the read and two free tools (Jina Reader and Scrapling by
default) at once and keeps the first complete page.

`toolbench-heldout5-lightning-fair.jsonl`: 40 sites spread over the set, one
read at a time, both modes on every site in a shuffled order. Fresh state.

| | Sites read | Median (read) | p75 | p90 | Total time |
|---|---|---|---|---|---|
| Normal | 35/40 | 5.0 s | 10.6 s | 14.9 s | 353 s |
| Lightning | 35/40 | 1.9 s | 5.8 s | 15.7 s | 325 s |

The 8 sites that took normal mode over 10 s came back in a median of 2.7 s
(12.5 s normally): StudentAid 18.6 to 1.0 s, Jobindex 14.9 to 1.1 s, Sports
Direct 12.0 to 2.0 s. Lightning was slower on 7 sites, most on Bon Appetit
(7.3 to 19.8 s) and AlternativeTo (5.7 to 15.7 s): the read's own page was
incomplete and the racers were slow, so it waited for them. OpenTable was read
only normally and Sierra only with lightning.

`toolbench-heldout5-lightning-4workers.jsonl` is the full set with four reads
at a time on one machine: 124/150 as in iteration 5, p75 9.4 s against 11.4 s
and p90 20.3 s against 24.3 s, but a median of 5.3 s against 4.0 s. Three
browsers per read across four parallel reads compete for the CPU; one read at
a time is what an agent waiting on a page sees.

## The real browser (v0.31)

Google Chrome 155, installed in WSL and found by itself, read alone
(`provider="real_browser"`) the 8 sites that walled every free tool in
iteration 5: none came through. Mighty Ape and Immobiliare blocked it and Bass
Pro answered with a challenge, all within 5 s; Carousell, PrimeLocation, Jiji
and Avito showed challenges that did not clear by themselves in 15 s (they
want a click or a press-and-hold, which the real browser never does); B&Q
still refused connections. Four of these (Mighty Ape, Carousell,
PrimeLocation, Jiji) only paid unblockers have ever read, and this machine's
address had met each of them in about eight runs that day. The option is for
walls that recognise automated browsers rather than an address.

## Held-out set 6 (blind), v0.32 code

`scripts/toolbench-heldout6.json` (built by `scripts/make_heldout6.py`): 150
sites, 10 in each of 15 categories, proposed per category by agents working from
memory with no web access and checked by a second agent the same way
(`scripts/heldout6-candidates.json`). Every host and brand on an earlier set,
row file, spot check, tuned corpus or route seed (`scripts/used_hosts.py`) was
left out. Our arms ran first on a fresh GitHub-hosted Azure VM (Dulles,
Virginia: a data-centre address, which sites wall more than a home one), so no
page of the set was read from the home connection; Firecrawl fetches from its
own network.

| Arm | Where | Sites read | Median (read) | p75 | p90 | Cost |
|---|---|---|---|---|---|---|
| FrankenSurf free, lightning, one read at a time | Azure VM | 124/150 (82.7%) | 1.7 s | 4.8 s | 13.3 s | $0 |
| FrankenSurf free, four reads at a time | Azure VM | 118/150 (78.7%) | 5.9 s | 15.8 s | 25.1 s | $0 |
| Firecrawl alone | its network | 128/150 (85.3%) | 4.8 s | 7.7 s | 11.3 s | $0.54 |

`toolbench-heldout6-lightning-azure.jsonl`, `toolbench-heldout6-free-azure.jsonl`,
`toolbench-heldout6-firecrawl.jsonl` (Firecrawl's API rate-limited 120 pages at
four at a time; those were re-read one at a time).

- 143 sites were read by at least one arm. FrankenSurf (either mode) read 15
  that Firecrawl missed; Firecrawl read 19 that FrankenSurf missed.
- The free arm's misses were mostly `BLOCKED` (16): walls that refuse a
  data-centre address. Lightning read 6 more: four through Jina Reader, which
  fetches from its own network (Houzz, Udemy, Petstock, Blibli), and CB2 and
  Summit Racing through Camoufox and Steel (the four-at-a-time run ended at a
  wall on both). Neither a proxy nor the new connection
  pool (`FRANKENSURF_EGRESS`) was configured on the VM.
- Every site the four-at-a-time free arm read, lightning read too; on the 118
  both read, lightning's median was 1.6 s faster.
- No paid arm: the VM has no keys.

The same VM read the 108 news and company articles of
`benchmarks/2026-10-09/articles-108.json` (`articles-108-azure-v0.32.jsonl`):
103 read and the article present on 100, as on Azure before v0.32.

## v0.33: smart lightning, paid after the first wall, wrong-URL rescue

### Against v0.32, one read at a time (set 5)

`ab-heldout5-free-v0.33-vs-v0.32.jsonl` and `ab-heldout5-paid-v0.33-vs-v0.32.jsonl`:
40 sites spread over set 5 (`--sample 40`), from home, both versions on every
site in a shuffled order. The `_v032` arms reproduce v0.32 in v0.33 code (one
racer after 10 s, paid tools after 4 walls, no rescue).

| Arm | Sites read | Median (read) | p90 (read) | Total time | Cost |
|---|---|---|---|---|---|
| Free, v0.32 | 35/40 | 6.5 s | 19.6 s | 450 s | $0 |
| Free, v0.33 | 37/40 | 3.9 s | 16.9 s | 404 s | $0 |
| Paid fallbacks, v0.32 | 36/40 | 5.1 s | 15.2 s | 336 s | $0.030 |
| Paid fallbacks, v0.33 | 37/40 | 3.6 s | 15.1 s | 278 s | $0.049 |

- The second read of a site is faster (Jina Reader's and the site's caches), so
  compare reads in the same position: free v0.33 against v0.32 was 5.3 s
  against 7.0 s when both read first, 3.4 s against 5.6 s when both read second.
- v0.33 read OpenTable and Cotswold Outdoor (a wrong URL, rescued) that v0.32
  missed, and missed nothing v0.32 read.
- Paid tools coming in at the first wall: Sierra 35 s (Camoufox) to 3 s
  (Firecrawl), Ricardo 11 to 3 s. The cost per 1,000 pages read goes from
  $0.83 to $1.32.
- A first paid run found a bug: vendor routing moved a paid tool with a strong
  record ahead of a free tool that usually beats that vendor, even with
  `paid_after_walls=0`. Fixed before the rows above; paid tools are now placed
  only by the paid escalation.

### Wrong URLs (set 5)

`rescue-heldout5-dead-urls.jsonl`: the 7 set-5 URLs that ended `NOT_FOUND` for
every arm in earlier runs, read with v0.33 defaults. Adore Beauty ("serum",
schema.org SearchAction), Barnes & Noble ("cookbook", the search form), Cotswold
Outdoor ("boots") and Dunelm ("cushion") came back as the site's own search
results; lastminute.com, Qoo10 and Seat61 found nothing that fit and stayed
`NOT_FOUND`.

### Held-out set 6, every tool alone (home)

`toolbench-heldout6-v0.33.jsonl` (FrankenSurf free and paid, both per site in a
shuffled order), `toolbench-heldout6-alone.jsonl` (each tool alone),
`toolbench-heldout6-firecrawl.jsonl` (Firecrawl alone, earlier the same day). The
set had been read once before (v0.32 on the VM, above); no v0.33 change looks at
its sites.

| Arm | Sites read | Median (read) | p90 (read) | Cost | Per 1k read |
|---|---|---|---|---|---|
| Headless Chromium alone | 68/150 (45.3%) | 2.6 s | 4.9 s | $0 | |
| Patchright alone | 68/150 (45.3%) | 3.6 s | 6.8 s | $0 | |
| Plain HTTP alone | 77/150 (51.3%) | 1.0 s | 3.0 s | $0 | |
| Camoufox alone | 99/150 (66.0%) | 8.5 s | 13.3 s | $0 | |
| Scrapling alone | 109/150 (72.7%) | 6.8 s | 17.2 s | $0 | |
| Jina Reader alone | 111/150 (74.0%) | 2.3 s | 10.4 s | $0 | |
| Firecrawl alone | 128/150 (85.3%) | 4.8 s | 11.3 s | $0.54 | $4.25 |
| Zyte alone | 129/150 (86.0%) | 12.0 s | 42.3 s | not reported | |
| FrankenSurf free | 127/150 (84.7%) | 3.3 s | 18.1 s | $0 | |
| FrankenSurf, paid fallbacks | 143/150 (95.3%) | 3.0 s | 25.9 s | $0.15 | $1.06 |

- 146 sites were read by at least one arm; FrankenSurf with paid fallbacks read
  143 of them. It read 18 that Firecrawl missed; Firecrawl read none it missed.
- Its 7 misses: one each in cars, developer sites, Australian, European and US
  retail, two in Asian and other retail.
- A racer won 32 free reads (30 of them valid) and 10 paid ones. Adairs was
  rescued (a moved search path).
- ZenRows answered `BUDGET_EXHAUSTED` on every page (its plan had run out), so
  it has no row; Scrapfly is still left out.

### Held-out set 6 on a cloud VM again

`toolbench-heldout6-free-azure-v0.33.jsonl`, `toolbench-heldout6-lightning-azure-v0.33.jsonl`
(GitHub-hosted Azure VM, Moses Lake; v0.32 ran in Dulles):

| Arm | v0.32 | v0.33 |
|---|---|---|
| Free, four reads at a time | 118/150, median 5.9 s | 124/150, median 6.4 s |
| Lightning, one read at a time | 124/150, median 1.7 s | 126/150, median 1.6 s |

v0.33's free arm read Adairs, Houzz, Infobae, Petstock, SpareRoom and Udemy that
v0.32 missed, and lost none; lightning gained three and lost Blibli. The free
median is from two different VMs with four reads at a time, where racers add
browsers to a four-core machine. `articles-108-azure-v0.33.jsonl`: 103 read and
the article present on 101 (v0.32: 100).
