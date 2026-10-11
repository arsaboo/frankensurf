# Benchmarks, 11 October 2026: held-out set 7 and fresh-machine installs

## Held-out set 7 (blind)

`scripts/toolbench-heldout7.json` (built by `scripts/make_heldout7.py`): the 171
candidates proposed for set 6 that set 6 did not take, chosen from general
knowledge by agents without web access on 10 October before any page was read,
and never read since; every host and brand already read was left out. 150 sites
across the same 15 categories (6 classifieds and 9 media after brand clashes).
Read once. No code change looked at these sites.

| Arm | Where | Sites read | Median (read) | p90 (read) | Cost | Per 1k read |
|---|---|---|---|---|---|---|
| FrankenSurf, paid fallbacks | home | 144/150 (96.0%) | 2.9 s | 25.7 s | $0.14 | $0.98 |
| FrankenSurf, free | home | 132/150 (88.0%) | 3.6 s | 21.3 s | $0 | |
| Firecrawl alone | its network | 127/150 (84.7%) | 4.6 s | 10.6 s | $0.54 | $4.28 |
| FrankenSurf, lightning, one at a time | Azure VM (v0.34) | 122/150 (81.3%) | 1.5 s | 11.9 s | $0 | |
| FrankenSurf, free, four at a time | Azure VM (v0.34) | 120/150 (80.0%) | 7.3 s | 29.4 s | $0 | |

`toolbench-heldout7-home.jsonl`, `toolbench-heldout7-firecrawl.jsonl`,
`toolbench-heldout7-*-azure-v0.34.jsonl`.

- 145 sites were read by at least one arm; FrankenSurf with paid fallbacks read
  144 of them. It read 17 that Firecrawl missed; Firecrawl read none it missed.
- NBC News first failed for all three home arms, Firecrawl's included, with a
  crash in FrankenSurf's own parsing: structured data whose image URL was an
  object. Fixed (`_image_urls` skips anything not a string; a test covers the
  shapes), and those three reads were re-run; the rows above include them.
  The VM rows were read with v0.34 and keep the crash.
- The home run's paid arm used Bright Data's old handling (fixed during the run:
  a Bright Data refusal is no longer taken for a page).
- The paid arm's misses: Indiegogo, Casa del Libro, Pricespy NZ and Lidl
  (results missing), ListenNotes (challenge), Vitacost (not found). The free
  arm's other misses were pages the paid tools got, mostly walls (Adverts.ie, Ingatlan,
  Duunitori, Loblaws, Europeana, Fodor's, 99.co, the Art Institute of Chicago,
  La Centrale, The Telegraph) and two thin pages (La Redoute, Grainger).
- The VM run had no Scrapling: see below.

## Fresh-machine installs

`.github/workflows/install-check.yml` installs from PyPI on fresh GitHub machines
(Ubuntu 24.04 with Python 3.12 and 3.13, macOS on Apple silicon with 3.12, plus
`uv tool install` and `pipx install`), runs `frankensurf setup`, reads pages
with every browser, starts the MCP server and runs `plugins` and `egress check`.
The install, setup, reads and MCP server worked everywhere on v0.34.0. It found
two browsers that `setup` reported installed but that never worked:

- **Scrapling on stock Ubuntu 23.10+ (24.04 servers).** The kernel forbids the
  unprivileged user namespaces Chrome's sandbox needs, and FrankenSurf asked
  Scrapling for the sandbox, so Chrome aborted at launch ("No usable
  sandbox!"). This is why Scrapling never served a read on any cloud-VM run.
  Now the sandbox stays on wherever the kernel allows it and is off only where
  the kernel forbids it, as for the bundled local Chromium.
- **Camoufox on macOS.** Given an explicit executable, Camoufox 0.5 looks for
  `properties.json` beside it; the macOS app bundle keeps it in
  `Contents/Resources`. The worker now looks in the bundle too.

With both fixed, every browser started and read a page on all three machines.
`frankensurf setup` now ends with `works_here`: which browsers started and read
a page on this machine. `FRANKENSURF_DEBUG_WORKERS=1` shows a browser worker's
own error when one fails.
