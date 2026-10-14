# frankensurf-shardx

An optional [Frankensurf](https://github.com/yail259/frankensurf) provider plugin for
[ShardX](https://github.com/ProxyShard/ShardBrowser), ProxyShard's anti-detect browser.

**ShardX's engine is closed source.** The `shardx` SDK downloads a prebuilt Chromium from the
vendor the first time it starts, and nobody outside ProxyShard can check what it does. That's why
this lives outside Frankensurf: `pip install frankensurf` never installs it, and it never runs
until you trust it.

```bash
pip install "frankensurf-shardx @ git+https://github.com/yail259/frankensurf#subdirectory=contrib/frankensurf-shardx"
frankensurf plugins trust provider shardx frankensurf-shardx
frankensurf read "https://www.example.com/search?q=lamp" --provider shardx
```

On 18 Akamai-protected sites it read 11, against 6 for Camoufox and 5 for Scrapling, in about 5 s
a page. Frankensurf with free tools read 15 but took about 25 s. Details and the 150-site run:
[frankensurf.dev/docs/integrations/shardx](https://frankensurf.dev/docs/integrations/shardx/).

`FRANKENSURF_SHARDX_HEADLESS=1` runs it without a window (the default on Linux with no display);
`FRANKENSURF_SHARDX_HEADED=1` always shows it.
