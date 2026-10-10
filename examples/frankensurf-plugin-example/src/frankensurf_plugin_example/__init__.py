"""An example Frankensurf provider plugin: it reads pages through your own
scraping service, an HTTP API that takes a URL and returns the page.

Copy this package, rename it, and change acquire() to call your service. The
service here is described by two environment variables, so no key is ever in
code or in a message:

  EXAMPLE_API_URL   the endpoint, which takes POST {"url": ...}
                    and answers {"status": 200, "url": ..., "html": ...}
  EXAMPLE_API_KEY   sent as a bearer token
"""
import os

import httpx

from frankensurf.providers import ProviderManifest
from frankensurf.runtime import WebFailure


class ExampleFetch:
    manifest = ProviderManifest(
        "example_fetch", "0.1.0",  # the ID must match the entry point name in pyproject.toml
        rendering=True,            # the service runs a browser, so it can satisfy render=True
        paid=True,                 # it costs money: only with allow_paid_fallbacks=True
        remote=True,               # it fetches from its own network, not this machine's address
    )

    def available(self, configured):
        # Unavailable means skipped, never failed: no key, no tries.
        return bool(os.environ.get("EXAMPLE_API_URL") and os.environ.get("EXAMPLE_API_KEY"))

    async def acquire(self, request, services):
        # Named explicitly (--provider example_fetch), it runs even when
        # available() is false: say what is missing instead of crashing.
        if not self.available(None):
            raise WebFailure("PROVIDER_UNAVAILABLE", "set EXAMPLE_API_URL and EXAMPLE_API_KEY first")
        # Keep state local: Frankensurf copies a plugin for each session and turns
        # away one whose methods read mutable module-level values.
        # What the service's own status means: a problem with the tool, not the page.
        service_failures = {401: "PROVIDER_UNAVAILABLE", 402: "BUDGET_EXHAUSTED",
                            403: "PROVIDER_UNAVAILABLE", 429: "RATE_LIMITED"}
        # What the page's status means: a wall makes Frankensurf climb to the next
        # tool, not-found ends the read.
        page_failures = {401: "AUTH_REQUIRED", 403: "BLOCKED", 404: "NOT_FOUND", 410: "NOT_FOUND",
                         429: "RATE_LIMITED"}
        try:
            async with httpx.AsyncClient(timeout=request.policy.unblocker_timeout_seconds) as client:
                response = await client.post(
                    os.environ["EXAMPLE_API_URL"], json={"url": request.url},
                    headers={"Authorization": "Bearer " + os.environ["EXAMPLE_API_KEY"]})
        except httpx.TimeoutException:
            raise WebFailure("TIMEOUT", "example service timed out") from None
        except httpx.HTTPError as error:
            # Only the error's type: its text could include the endpoint.
            raise WebFailure("PROVIDER_DOWN", f"example service unreachable ({type(error).__name__})") from None
        if response.status_code != 200:
            raise WebFailure(service_failures.get(response.status_code, "PROVIDER_DOWN"),
                             f"example service answered {response.status_code}")
        body = response.json()
        status = int(body.get("status") or 0)
        if status in page_failures:
            raise WebFailure(page_failures[status], f"page answered {status}", status)
        html = body.get("html") or ""
        # Frankensurf checks the page itself (challenge pages, empty shells,
        # results that never loaded), so return what you got and let it judge.
        return {"url": body.get("url") or request.url, "content": html, "raw": html.encode(),
                "content_type": "text/html", "http_status": status or 200, "headers": {},
                "cost_usd": 0.001}  # what this read cost; None if you don't know
