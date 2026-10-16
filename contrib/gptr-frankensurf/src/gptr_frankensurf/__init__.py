"""Frankensurf search as a GPT Researcher retriever plugin.

    pip install "gptr-frankensurf @ git+https://github.com/yail259/frankensurf#subdirectory=contrib/gptr-frankensurf"
    export RETRIEVER=frankensurf

Results are Frankensurf's checked search results (each one checked for topic, kind of page and
date), free sources only unless FRANKENSURF_GPTR_PAID=1. GPT Researcher scrapes them as usual; point
its scraper at `frankensurf serve` too (SCRAPER=firecrawl, FIRECRAWL_SERVER_URL) and the pages come
through Frankensurf as well.
"""
from __future__ import annotations

import logging
import os

from frankensurf.agent_tools import Tools, _runner
from gpt_researcher.retrievers.base import BaseRetriever

log = logging.getLogger(__name__)


class FrankensurfSearch(BaseRetriever):
    # Results are links for GPT Researcher's scraper; their text isn't included.
    requires_scraping = True

    def __init__(self, query, query_domains=None):
        self.query = query
        self.query_domains = [domain for domain in (query_domains or []) if domain]
        policy = {}
        if os.getenv("FRANKENSURF_GPTR_PAID") == "1":
            policy["allow_paid_fallbacks"] = True
            if os.getenv("FRANKENSURF_GPTR_MAX_COST"):
                policy["max_cost_usd"] = float(os.environ["FRANKENSURF_GPTR_MAX_COST"])
        self.tools = Tools(state_dir=os.getenv("FRANKENSURF_STATE") or None, **policy)

    def _search(self, site, limit):
        return _runner()(self.tools.search(self.query, site=site, limit=limit))["results"]

    def search(self, max_results=7):
        try:
            # Frankensurf's search takes one site, so a domain filter runs once per domain.
            sites = self.query_domains or [None]
            results, seen = [], set()
            for site in sites:
                for item in self._search(site, max_results):
                    url = item.get("url")
                    if url and url not in seen:
                        seen.add(url)
                        results.append({"href": url, "body": item.get("snippet") or item.get("title") or ""})
            return results[:max_results]
        except Exception as error:  # the contract: one failing retriever must not abort a research run
            log.warning("Frankensurf search failed for %r: %s", self.query, error)
            return []
