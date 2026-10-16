"""A Firecrawl-compatible HTTP endpoint that reads through Frankensurf.

Anything that can point at a self-hosted Firecrawl (FIRECRAWL_BASE_URL, api_url and the like) reads
through Frankensurf with no code change:

    frankensurf serve                      # http://127.0.0.1:3002, Firecrawl's own port
    FIRECRAWL_BASE_URL=http://127.0.0.1:3002 FIRECRAWL_KEY=local npm start

POST /v1/scrape, /v1/search, /v2/scrape and /v2/search, with Firecrawl's request and response
shapes. A page Frankensurf couldn't read is never passed off as one: scrape answers success false
with the reason and every tool tried (status 500, or 408 for the request's timeout, as Firecrawl
does), and search keeps that result's URL, title and description but leaves its markdown out, with
the reason in metadata.error. Answers come a few seconds inside the client's timeout, so the client
gets the reason instead of hanging up. Standard library only.

onlyMainContent (Firecrawl's SDKs send true by default) is accepted and ignored: the whole page's
text comes back. Frankensurf's main-content cut is built for articles; on search and listing pages it
kept a few lines and lost the results, which cost 8 of 150 pages read through GPT Researcher.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from .runtime import Runtime, WebPolicy, attempt_summary

DEFAULT_PORT = 3002
MAX_BODY = 1_000_000
# Answer this long before the client's own timeout (Firecrawl's SDKs send it as "timeout", in ms).
TIMEOUT_MARGIN_SECONDS = 3.0
REASONS = {200: "OK", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed",
           408: "Request Timeout", 413: "Payload Too Large", 500: "Internal Server Error"}


def _tried(receipt: dict) -> list[str]:
    seen = []
    for attempt in receipt.get("attempts") or ():
        provider = attempt.get("provider")
        if provider and provider not in seen:
            seen.append(provider)
    return seen


def couldnt_read(receipt: dict) -> str:
    """The plain sentence a calling agent gets instead of a page."""
    failure = receipt.get("failure") or {}
    reason = failure.get("code") or "UNKNOWN"
    if failure.get("message"):
        reason += f": {failure['message']}"
    tried = _tried(receipt)
    return (f"Could not read this page ({reason})."
            + (f" Tried: {', '.join(tried)}." if tried else "")
            + " Don't fill in its contents from search snippets or memory.")


def _document(result: dict, url: str, formats: list[str]) -> dict:
    receipt = result.get("receipt") or {}
    text = result.get("markdown") or result.get("text") or ""
    document: dict[str, Any] = {}
    if "markdown" in formats or not formats:
        document["markdown"] = text
    if "links" in formats:
        document["links"] = [link["url"] for link in result.get("links") or []]
    content = result.get("content")
    if isinstance(content, bytes):
        content = content.decode("utf-8", "replace")
    if isinstance(content, str) and ("html" in formats or "rawHtml" in formats):
        document["html" if "html" in formats else "rawHtml"] = content
    document["metadata"] = {
        "title": result.get("title"), "sourceURL": url, "url": result.get("url") or url,
        "statusCode": receipt.get("http_status") or 200,
        "frankensurf": {"trace_id": receipt.get("trace_id"), "via": (_tried(receipt) or [None])[-1],
                        "cost_usd": receipt.get("cost_usd") or 0}}
    return document


class FirecrawlCompat:
    """Request handling, separate from the socket so it can be tested without one."""

    def __init__(self, web: Runtime, policy: dict | None = None):
        self.web = web
        self.policy = dict(policy or {})

    async def _read(self, url: str, timeout_ms: int | None) -> dict:
        # Real markdown, as Firecrawl returns it (headings, lists, links, tables), and links for
        # the links format.
        overrides = {**self.policy, "prefer_markdown": True, "markdown_output": True, "include_links": True}
        read = self.web.read(url, policy_overrides=overrides)
        if timeout_ms:
            return await asyncio.wait_for(read, max(1.0, timeout_ms / 1000 - TIMEOUT_MARGIN_SECONDS))
        return await read

    async def scrape(self, body: dict) -> tuple[int, dict]:
        url = body.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            return 400, {"success": False, "error": "url must be an http(s) URL"}
        formats = [f for f in body.get("formats") or ["markdown"] if isinstance(f, str)]
        try:
            result = await self._read(url, body.get("timeout"))
        except asyncio.TimeoutError:
            return 408, {"success": False, "error": "Could not read this page (TIMEOUT) within the"
                         " request's timeout. Don't fill in its contents from search snippets or memory."}
        receipt = result.get("receipt") or {}
        if receipt.get("status") != "observed":
            return 500, {"success": False, "error": couldnt_read(receipt),
                         "frankensurf": {"trace_id": receipt.get("trace_id"), "tried": _tried(receipt),
                                         "failure": receipt.get("failure"),
                                         "attempts": attempt_summary(receipt),
                                         "next_step": receipt.get("next_step")}}
        return 200, {"success": True, "data": _document(result, url, formats)}

    async def search(self, body: dict, v2: bool = False) -> tuple[int, dict]:
        query = body.get("query")
        if not isinstance(query, str) or not query.strip():
            return 400, {"success": False, "error": "query is required"}
        limit = max(1, min(int(body.get("limit") or 5), 20))
        search_policy = {**self.policy}
        search_policy.setdefault("provider", "http")
        found = await self.web.search(query, limit=limit, policy=WebPolicy(**search_policy))
        items = [{"url": item.get("url"), "title": item.get("title"),
                  "description": item.get("snippet") or ""}
                 for item in (found.get("results") or [])[:limit] if item.get("url")]
        options = body.get("scrapeOptions") or {}
        formats = [f for f in options.get("formats") or [] if isinstance(f, str)]
        if formats:
            async def fill(item):
                try:
                    result = await self._read(item["url"], body.get("timeout"))
                except asyncio.TimeoutError:
                    item["metadata"] = {"sourceURL": item["url"], "error": "Could not read this page"
                                        " (TIMEOUT) within the request's timeout."}
                    return
                receipt = result.get("receipt") or {}
                if receipt.get("status") == "observed":
                    document = _document(result, item["url"], formats)
                    item.update(document)
                    item["title"] = item.get("title") or document["metadata"].get("title")
                else:
                    item["metadata"] = {"sourceURL": item["url"], "error": couldnt_read(receipt),
                                        "frankensurf": {"trace_id": receipt.get("trace_id")}}
            await asyncio.gather(*(fill(item) for item in items))
        return 200, {"success": True, "data": {"web": items} if v2 else items}

    async def handle(self, method: str, path: str, body: bytes) -> tuple[int, dict]:
        path = path.split("?", 1)[0].rstrip("/")
        if method == "GET" and path in ("", "/health"):
            return 200, {"success": True, "service": "frankensurf", "compatible_with": "firecrawl v1, v2"}
        routes = {"/v1/scrape": self.scrape, "/v2/scrape": self.scrape,
                  "/v1/search": self.search, "/v2/search": lambda b: self.search(b, v2=True)}
        if path not in routes:
            return 404, {"success": False, "error": f"Frankensurf serves scrape and search only, not {path}"}
        if method != "POST":
            return 405, {"success": False, "error": "use POST"}
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            return 400, {"success": False, "error": "body must be JSON"}
        if not isinstance(payload, dict):
            return 400, {"success": False, "error": "body must be a JSON object"}
        return await routes[path](payload)


async def _respond(writer, status: int, payload: dict) -> None:
    data = json.dumps(payload, ensure_ascii=False, default=str).encode()
    head = (f"HTTP/1.1 {status} {REASONS.get(status, 'OK')}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n").encode()
    writer.write(head + data)
    await writer.drain()


async def serve(state_dir, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
                policy: dict | None = None, ready=None) -> None:
    async with Runtime(state_dir=state_dir) as web:
        app = FirecrawlCompat(web, policy)

        async def connection(reader, writer):
            try:
                request = await reader.readuntil(b"\r\n\r\n")
                lines = request.decode("latin-1").split("\r\n")
                method, path, _ = (lines[0].split(" ") + ["", ""])[:3]
                headers = {k.strip().lower(): v.strip() for k, _, v in
                           (line.partition(":") for line in lines[1:] if line)}
                length = int(headers.get("content-length") or 0)
                if length > MAX_BODY:
                    await _respond(writer, 413, {"success": False, "error": "body too large"})
                    return
                body = await reader.readexactly(length) if length else b""
                status, payload = await app.handle(method.upper(), path, body)
                await _respond(writer, status, payload)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
                pass
            except Exception as error:  # one bad request must not stop the server
                await _respond(writer, 500, {"success": False, "error": f"{type(error).__name__}: {error}"})
            finally:
                writer.close()

        server = await asyncio.start_server(connection, host, port)
        if ready:
            ready(server)
        async with server:
            await server.serve_forever()
