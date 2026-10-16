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
import re
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

    def __init__(self, web: Runtime, policy: dict | None = None, base_url: str = "", model: str | None = None):
        import os
        self.web = web
        self.policy = dict(policy or {})
        self.base_url = base_url  # for the status URL a crawl answers with
        self.model = model or os.getenv("FRANKENSURF_AGENT_MODEL")  # summary, json and extract (agent mode)
        self.crawls: dict[str, dict] = {}
        self.extracts: dict[str, dict] = {}

    def _needs_model(self):
        if not self.model:
            return 400, {"success": False, "error": "summary, json and extract need a model: start frankensurf "
                         "serve --model <any LiteLLM model>, or set FRANKENSURF_AGENT_MODEL"}
        return None

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
        raw_formats = body.get("formats") or ["markdown"]
        formats = [f for f in raw_formats if isinstance(f, str)]
        json_spec = next((f for f in raw_formats if isinstance(f, dict) and f.get("type") == "json"), None)
        if json_spec is None and "json" in formats:
            json_spec = body.get("jsonOptions") or body.get("extract") or {}
        wants_llm = "summary" in formats or json_spec is not None
        if wants_llm and self._needs_model():
            return self._needs_model()
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
        document = _document(result, url, formats)
        if wants_llm:
            from .agent import on_page
            document.update(await on_page(result, url, summary="summary" in formats, json_output=json_spec is not None,
                                          schema=(json_spec or {}).get("schema"), prompt=(json_spec or {}).get("prompt"),
                                          model=self.model))
        return 200, {"success": True, "data": document}

    async def batch_start(self, body: dict, version: str) -> tuple[int, dict]:
        urls = [u for u in body.get("urls") or [] if isinstance(u, str) and u.startswith(("http://", "https://"))]
        if not urls:
            return 400, {"success": False, "error": "urls must be a list of http(s) URLs"}
        import uuid
        job_id = str(uuid.uuid4())
        job = {"status": "scraping", "total": len(urls), "completed": 0, "data": [], "failed": []}
        self.crawls[job_id] = job
        options = {key: value for key, value in body.items() if key not in ("urls", "url")}

        async def one(url):
            status, out = await self.scrape({**options, "url": url})
            job["completed"] += 1
            if out.get("success"):
                job["data"].append(out["data"])
            else:
                job["failed"].append({"url": url, "error": out.get("error")})

        async def run():
            try:
                await asyncio.gather(*(one(url) for url in urls))
                job["status"] = "completed"
            except Exception as error:
                job.update(status="failed", error=f"{type(error).__name__}: {error}")
        job["task"] = asyncio.get_running_loop().create_task(run())
        return 200, {"success": True, "id": job_id, "url": f"{self.base_url}/{version}/batch/scrape/{job_id}"}

    async def extract_start(self, body: dict, version: str) -> tuple[int, dict]:
        if self._needs_model():
            return self._needs_model()
        urls = [u for u in body.get("urls") or [] if isinstance(u, str) and u.startswith(("http://", "https://"))]
        if not urls:
            return 400, {"success": False, "error": "urls must be a list of http(s) URLs"}
        import uuid
        from .agent import extract
        job_id = str(uuid.uuid4())
        job = {"status": "processing", "data": None}
        self.extracts[job_id] = job

        async def run():
            try:
                done = await extract(self.web, urls, schema=body.get("schema"), prompt=body.get("prompt"),
                                     model=self.model, policy=self.policy)
                job.update(status="completed" if done.get("data") is not None else "failed", data=done.get("data"),
                           frankensurf={"sources": done["sources"], "couldnt_read": done["couldnt_read"],
                                        "llm_cost_usd": done["llm_cost_usd"]},
                           **({"error": done["error"]} if done.get("error") else {}))
            except Exception as error:
                job.update(status="failed", error=f"{type(error).__name__}: {error}")
        job["task"] = asyncio.get_running_loop().create_task(run())
        return 200, {"success": True, "id": job_id, "url": f"{self.base_url}/{version}/extract/{job_id}"}

    def extract_status(self, job_id: str) -> tuple[int, dict]:
        job = self.extracts.get(job_id)
        if job is None:
            return 404, {"success": False, "error": "no extract with that id"}
        return 200, {"success": True, "status": job["status"], "data": job["data"], "expiresAt": None,
                     **({"error": job["error"]} if job.get("error") else {}),
                     **({"frankensurf": job["frankensurf"]} if job.get("frankensurf") else {})}

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

    async def map(self, body: dict, v2: bool = False) -> tuple[int, dict]:
        url = body.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            return 400, {"success": False, "error": "url must be an http(s) URL"}
        from .crawl import site_map
        found = await site_map(self.web, url, limit=max(1, int(body.get("limit") or 5000)),
                               search=body.get("search"), include_subdomains=bool(body.get("includeSubdomains")),
                               use_sitemap=body.get("sitemap") != "skip" and not body.get("ignoreSitemap"),
                               policy_overrides=self.policy)
        links = [{"url": link} for link in found["links"]] if v2 else found["links"]
        return 200, {"success": True, "links": links}

    async def crawl_start(self, body: dict, version: str) -> tuple[int, dict]:
        url = body.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            return 400, {"success": False, "error": "url must be an http(s) URL"}
        import uuid
        from .crawl import crawl
        job_id = str(uuid.uuid4())
        job = {"status": "scraping", "total": 0, "completed": 0, "data": [], "failed": [], "version": version}
        self.crawls[job_id] = job
        formats = [f for f in (body.get("scrapeOptions") or {}).get("formats") or ["markdown"] if isinstance(f, str)]
        depth = body.get("maxDiscoveryDepth", body.get("maxDepth", 2))

        def on_page(page):
            job["completed"] += 1
            if page["status"] == "observed":
                job["data"].append({"markdown": page["markdown"], "metadata": {
                    "title": page["title"], "sourceURL": page["url"], "url": page["final_url"] or page["url"],
                    "statusCode": 200, "frankensurf": {"trace_id": page["trace_id"], "depth": page["depth"]}}}
                    if "markdown" in formats else {"metadata": {"sourceURL": page["url"], "title": page["title"]}})
            else:
                job["failed"].append({"url": page["url"], "failure": page["failure"], "trace_id": page["trace_id"]})

        async def run():
            try:
                done = await crawl(self.web, url, limit=max(1, int(body.get("limit") or 100)),
                                   max_depth=max(0, int(depth if depth is not None else 2)),
                                   include=body.get("includePaths") or None, exclude=body.get("excludePaths") or None,
                                   include_subdomains=bool(body.get("allowSubdomains")),
                                   entire_domain=bool(body.get("crawlEntireDomain") or body.get("allowBackwardLinks")),
                                   use_sitemap=body.get("sitemap") != "skip" and not body.get("ignoreSitemap"),
                                   policy_overrides=self.policy, on_page=on_page)
                job.update(status="completed", total=done["completed"])
            except asyncio.CancelledError:
                job["status"] = "cancelled"
                raise
            except Exception as error:
                job.update(status="failed", error=f"{type(error).__name__}: {error}")
        job["task"] = asyncio.get_running_loop().create_task(run())
        return 200, {"success": True, "id": job_id, "url": f"{self.base_url}/{version}/crawl/{job_id}"}

    def crawl_status(self, job_id: str) -> tuple[int, dict]:
        job = self.crawls.get(job_id)
        if job is None:
            return 404, {"success": False, "error": "no crawl with that id"}
        return 200, {"success": True, "status": job["status"], "total": max(job["total"], job["completed"]),
                     "completed": job["completed"], "creditsUsed": 0, "expiresAt": None,
                     "data": job["data"],  # no "next": everything comes in one page
                     **({"error": job["error"]} if job.get("error") else {}),
                     "frankensurf": {"failed": job["failed"]}}

    async def handle(self, method: str, path: str, body: bytes) -> tuple[int, dict]:
        path = path.split("?", 1)[0].rstrip("/")
        if method == "GET" and path in ("", "/health"):
            return 200, {"success": True, "service": "frankensurf", "compatible_with": "firecrawl v1, v2"}
        extract_path = re.fullmatch(r"/(v1|v2)/extract/([0-9a-f-]{36})", path)
        if extract_path and method == "GET":
            return self.extract_status(extract_path.group(2))
        status_path = re.fullmatch(r"/(v1|v2)/(?:crawl|batch/scrape)/([0-9a-f-]{36})", path)
        if status_path and method == "GET":
            return self.crawl_status(status_path.group(2))
        if status_path and method == "DELETE":
            job = self.crawls.get(status_path.group(2))
            if job is None:
                return 404, {"success": False, "error": "no crawl with that id"}
            job["task"].cancel()
            job["status"] = "cancelled"
            return 200, {"success": True, "status": "cancelled"}
        routes = {"/v1/scrape": self.scrape, "/v2/scrape": self.scrape,
                  "/v1/search": self.search, "/v2/search": lambda b: self.search(b, v2=True),
                  "/v1/map": self.map, "/v2/map": lambda b: self.map(b, v2=True),
                  "/v1/crawl": lambda b: self.crawl_start(b, "v1"), "/v2/crawl": lambda b: self.crawl_start(b, "v2"),
                  "/v1/extract": lambda b: self.extract_start(b, "v1"), "/v2/extract": lambda b: self.extract_start(b, "v2"),
                  "/v1/batch/scrape": lambda b: self.batch_start(b, "v1"),
                  "/v2/batch/scrape": lambda b: self.batch_start(b, "v2")}
        if path not in routes:
            return 404, {"success": False,
                         "error": f"Frankensurf serves scrape, search, map, crawl and extract, not {path}"}
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
                policy: dict | None = None, ready=None, model: str | None = None) -> None:
    async with Runtime(state_dir=state_dir) as web:
        app = FirecrawlCompat(web, policy, base_url=f"http://{host}:{port}", model=model)

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
