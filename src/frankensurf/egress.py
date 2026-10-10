"""More than one connection (opt-in): an egress pool the owner configures.

Some walls block an address rather than a browser, and once a site has seen
enough reads from one address, every tool on that machine meets the wall. With
``FRANKENSURF_EGRESS`` set, a read that the direct connection walled gets one
more pass through each other connection in the pool: proxies you already have
(an SSH tunnel to a VPS is ``ssh -D 1080 vps``, then ``socks5://127.0.0.1:1080``;
a phone's hotspot proxy; a residential pool). The connection that gets through
becomes the site's hint, so the next read of that site starts there.

  FRANKENSURF_EGRESS="vps=socks5://127.0.0.1:1080, phone=http://192.168.1.20:8080"

Entries are comma- or newline-separated, ``name=url`` or a bare URL. Like
``FRANKENSURF_PROXY``, the pool comes from the environment (or the .env file)
only: receipts, traces and hints name a connection, never its URL or
credentials.
"""
from __future__ import annotations

import re
from contextvars import ContextVar

# The connection the current read pass goes through, or None for the direct one.
CURRENT: ContextVar[dict | None] = ContextVar("frankensurf_egress", default=None)
_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")


def pool() -> list[dict]:
    """The configured connections, each {"name", "url", "server"[, "username", "password"]}."""
    from .hosted_providers import _setting
    from .runtime import proxy_settings
    raw = _setting("egress") or ""
    entries, names = [], set()
    items = [part.strip() for part in re.split(r"[,\n]", raw) if part.strip()]
    for index, item in enumerate(items):
        name, separator, url = item.partition("=")
        if not separator or "://" in name:
            name, url = f"egress{index + 1}", item
        name = name.strip()
        if not _NAME.match(name) or name in names or name == "direct":
            raise ValueError("FRANKENSURF_EGRESS names must be short, unique words (not 'direct')")
        try:
            settings = proxy_settings(url.strip())
        except ValueError:
            raise ValueError(f"FRANKENSURF_EGRESS entry {name!r} must look like http://user:pass@host:port"
                             " or socks5://host:port") from None
        names.add(name)
        entries.append({"name": name, **settings})
    return entries


# A small public page that says which address a request came from (and whether
# it came through Cloudflare WARP). Read once per connection by `egress check`.
TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"


async def check(entries=None, *, url=TRACE_URL, timeout=15.0):
    """Can each connection reach the web, and from where? The direct connection
    first, then each one in the pool: [{"name", "ok", "ip", "country", "warp",
    "seconds", "error"}]. Never a connection's URL or credentials."""
    import time

    import httpx
    entries = pool() if entries is None else entries
    rows = []
    for entry in [{"name": "direct"}] + list(entries):
        started = time.monotonic()
        row = {"name": entry["name"], "ok": False}
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                         **({"proxy": entry["url"]} if entry.get("url") else {})) as client:
                response = await client.get(url)
            fields = dict(line.split("=", 1) for line in response.text.splitlines() if "=" in line)
            row.update(ok=response.status_code == 200, ip=fields.get("ip"), country=fields.get("loc"),
                       warp=fields.get("warp"))
        except ImportError:
            row["error"] = "needs the socks extra: pip install 'frankensurf[socks]'"
        except Exception as error:  # the type only: a proxy error can carry its address
            row["error"] = type(error).__name__
        row["seconds"] = round(time.monotonic() - started, 2)
        rows.append(row)
    return rows
