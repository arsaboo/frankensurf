# ShardX, 14 October 2026

[ShardX](https://github.com/ProxyShard/ShardBrowser) (shardx 2.0.3, ProxyShard's anti-detect
browser) against the free browsers Frankensurf already uses. Both runs were on a throwaway GitHub
machine (`.github/workflows/shardbench.yml`), because ShardX's engine is a closed, prebuilt
Chromium that its SDK downloads from the vendor. One read per tool per site, same pass/fail check
as every other benchmark here, script `scripts/shardbench.py`.

## Held-out set 7 (150 sites picked blind on 10 October)

| Tool | Read right | Median | Slowest tenth |
| --- | --- | --- | --- |
| ShardX | 104/150 (69.3%) | 3.9 s | 7.6 s |
| Scrapling | 96/150 (64.0%) | 5.0 s | 11.3 s |
| Camoufox | 94/150 (62.7%) | 7.6 s | 12.5 s |
| Patchright | 67/150 (44.7%) | 2.1 s | 4.6 s |
| Headless Chromium | 66/150 (44.0%) | 1.3 s | 4.5 s |

Two sites were read by ShardX and by none of the other browsers (bayt-ae, konga). Frankensurf with
free tools read both in the weekly run of 13 October, a day earlier (81.3% of the set overall).
Site by site against that run, ShardX read 3 sites Frankensurf missed (lidl-de, makemytrip,
rijksoverheid-nl) and Frankensurf read 21 that ShardX missed. Rows: `heldout7-rows.jsonl`.

## Akamai-protected sites

`scripts/shardbench-akamai-candidates.json` lists 46 retail search pages, written before any tool
read them. Each got one plain request first, and the 18 where Frankensurf's `wall_vendor()` saw
Akamai answer were read (`akamai-probe.json`). Frankensurf, free tools only, ran in the same pass.

| Tool | Read right | Median |
| --- | --- | --- |
| Frankensurf (free tools) | 15/18 | 25.0 s |
| ShardX | 11/18 | 5.4 s |
| Camoufox | 6/18 | 6.2 s |
| Scrapling | 5/18 | 4.2 s |
| Patchright | 3/18 | 1.4 s |
| Headless Chromium | 3/18 | 0.2 s |

Paired, site by site (two-sided sign test):

- ShardX against Scrapling: 6 sites only ShardX read, 0 only Scrapling (p = 0.03).
- ShardX against Camoufox: 5 against 0 (p = 0.06).
- ShardX against Frankensurf: 1 against 5 (p = 0.22). Kmart AU was the one only ShardX read.

So on Akamai ShardX is the strongest single browser here, and much faster than Frankensurf's climb
to get there; Frankensurf still reads more of them. 18 sites is a small set. Rows:
`akamai-rows.jsonl`.

## What changed because of it

Nothing in Frankensurf itself. ShardX is available as an optional plugin you install and trust
yourself: `contrib/frankensurf-shardx`, documented at
[frankensurf.dev/docs/integrations/shardx](https://frankensurf.dev/docs/integrations/shardx/).
