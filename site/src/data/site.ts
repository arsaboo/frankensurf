// Content for the golden-spec landing page.
// "today" figures are public, mostly vendor-run claims (see `sources`).
// Most Frankensurf figures are measured (scripts/toolbench.py, 6 to 10 Oct 2026); each section
// says "measured" once, in its subtitle or legend, never per row. The rest (the coverage
// heatmap, rung tokens and costs) are goals for the ideal tool.

export const sources = {
  bu: { label: "Browser Use: best browser infrastructure", url: "https://browser-use.com/posts/best-browser-infrastructure-for-ai-agents" },
  kernel: { label: "Kernel: 9 best browsers for AI agents", url: "https://www.kernel.sh/ai-library/best-browsers-for-ai-agents-2026" },
  om2w: { label: "Browser Use: Online-Mind2Web results", url: "https://browser-use.com/posts/online-mind2web-benchmark" },
  tinyfish: { label: "TinyFish: AI browser agents benchmarks", url: "https://www.tinyfish.ai/blog/ai-browser-agents" },
  parallel: { label: "Parallel: 6 search APIs benchmarked", url: "https://parallel.ai/articles/best-ai-search-for-agents" },
  openb: { label: "Openbenchmarks: web search APIs", url: "https://openbenchmarks.com/web-search/best-web-search-api-for-ai-agents" },
  aim: { label: "AIMultiple: agentic search benchmark", url: "https://aimultiple.com/agentic-search" },
  fastcrw: { label: "fastCRW: LLM-ready web data APIs", url: "https://fastcrw.com/blog/best-llm-ready-web-data-apis" },
  signed: { label: "Cloudflare: signed agents", url: "https://blog.cloudflare.com/signed-agents/" },
  cfmd: { label: "Cloudflare: Markdown for Agents", url: "https://developers.cloudflare.com/fundamentals/reference/markdown-for-agents" },
  fcclaude: { label: "Firecrawl: Claude web fetch vs Firecrawl", url: "https://www.firecrawl.dev/blog/claude-web-fetch-vs-firecrawl" },
  gem: { label: "Google: Gemini URL context", url: "https://ai.google.dev/gemini-api/docs/url-context" },
  stealth: { label: "DEV: anti-detect browser benchmark 2026", url: "https://dev.to/ianlpaterson/anti-detect-browser-benchmark-2026-7-stealth-tools-31-cloudflare-targets-651-verdicts-4361" },
  zen: { label: "ZenRows: scraping APIs benchmarked", url: "https://www.zenrows.com/blog/best-web-scraping-apis-benchmarked" },
  unb: { label: "AIMultiple: web unblockers", url: "https://aimultiple.com/web-unblockers" },
  egotok: { label: "ego.app: Playwright MCP token problem", url: "https://lite.ego.app/article/playwright-mcp-token-problem" },
  egodt: { label: "ego.app: DevTools MCP vs Playwright MCP", url: "https://lite.ego.app/article/devtools-mcp-vs-playwright-mcp" },
  omega: { label: "O-mega: Cloudflare blocks AI agents by default", url: "https://o-mega.ai/articles/cloudflare-blocks-ai-agents-by-default-2026" },
  camofox: { label: "GitHub: camofox-browser", url: "https://github.com/jo-inc/camofox-browser" },
  camps: { label: "AgentsCamp: browser agents compared", url: "https://agentscamp.com/guides/comparisons/browser-agents-compared-2026" },
  dapp: { label: "Digital Applied: Playwright vs Stagehand", url: "https://www.digitalapplied.com/blog/browser-automation-ai-agents-playwright-stagehand-2026" },
  fsid: { label: "Frankensurf docs: human handoff", url: "/docs/blocked/handoff/" },
  zyte: { label: "Zyte API docs", url: "https://docs.zyte.com/zyte-api/" },
  apify: { label: "Apify: RAG Web Browser", url: "https://apify.com/apify/rag-web-browser" },
  bench: { label: "Frankensurf tool benchmark, 11 Oct 2026", url: "https://github.com/yail259/frankensurf/tree/main/benchmarks/2026-10-11" },
  searchbench: { label: "Frankensurf search benchmark, 12 Oct 2026", url: "https://github.com/yail259/frankensurf/tree/main/benchmarks/2026-10-12-search" },
} as const;

export type SourceKey = keyof typeof sources;

// Tools shown in the logo wall. Logos are each project's official GitHub org avatar
// (or the project's own logo file), stored locally in public/logos.
export const marquee: { name: string; logo: string }[] = [
  { name: "Playwright", logo: "playwright.svg" },
  { name: "Browserbase", logo: "browserbase.png" },
  { name: "Kernel", logo: "kernel.png" },
  { name: "Steel", logo: "steel.png" },
  { name: "Browser Use", logo: "browser-use.png" },
  { name: "Firecrawl", logo: "firecrawl.png" },
  { name: "Exa", logo: "exa.png" },
  { name: "Parallel", logo: "parallel.png" },
  { name: "Brave Search", logo: "brave.png" },
  { name: "Tavily", logo: "tavily.png" },
  { name: "Camoufox", logo: "camoufox.svg" },
  { name: "Bright Data", logo: "brightdata.png" },
  { name: "Scrapfly", logo: "scrapfly.png" },
  { name: "ZenRows", logo: "zenrows.png" },
  { name: "Jina", logo: "jina.png" },
  { name: "Chrome DevTools", logo: "devtools.png" },
  { name: "Anchor", logo: "anchor.png" },
  { name: "Hyperbrowser", logo: "hyperbrowser.png" },
  { name: "Skyvern", logo: "skyvern.png" },
  { name: "Cloudflare", logo: "cloudflare.jpg" },
];

// Problem matrix, measured 6 Oct 2026 (scripts/toolbench.py): pages read right
// out of pages tried, per wall, over 127 pages (78 tuned, 4 logged-in, 45 unseen).
// Single tools read logged-in pages without the owner's session.
export type Cell = { v: number; n: number };
export const walls = ["Static", "JS app", "Cloudflare", "DataDome", "Akamai", "PerimeterX", "Bot checks", "Login"];
export const toolRows: { name: string; cells: Cell[] }[] = [
  { name: "Plain HTTP Only", cells: [{"v": 13, "n": 16}, {"v": 11, "n": 32}, {"v": 3, "n": 12}, {"v": 3, "n": 8}, {"v": 8, "n": 19}, {"v": 4, "n": 9}, {"v": 6, "n": 25}, {"v": 1, "n": 6}] },
  { name: "Jina Reader Only", cells: [{"v": 10, "n": 16}, {"v": 16, "n": 32}, {"v": 3, "n": 12}, {"v": 0, "n": 8}, {"v": 9, "n": 19}, {"v": 5, "n": 9}, {"v": 7, "n": 25}, {"v": 2, "n": 6}] },
  { name: "Headless Chromium Only", cells: [{"v": 12, "n": 16}, {"v": 9, "n": 32}, {"v": 1, "n": 12}, {"v": 0, "n": 8}, {"v": 5, "n": 19}, {"v": 0, "n": 9}, {"v": 4, "n": 25}, {"v": 5, "n": 6}] },
  { name: "Camoufox Only", cells: [{"v": 15, "n": 16}, {"v": 19, "n": 32}, {"v": 6, "n": 12}, {"v": 1, "n": 8}, {"v": 11, "n": 19}, {"v": 9, "n": 9}, {"v": 7, "n": 25}, {"v": 4, "n": 6}] },
  { name: "Scrapling Only", cells: [{"v": 11, "n": 16}, {"v": 26, "n": 32}, {"v": 6, "n": 12}, {"v": 4, "n": 8}, {"v": 14, "n": 19}, {"v": 1, "n": 9}, {"v": 12, "n": 25}, {"v": 5, "n": 6}] },
  { name: "Firecrawl Only", cells: [{"v": 12, "n": 16}, {"v": 23, "n": 32}, {"v": 12, "n": 12}, {"v": 7, "n": 8}, {"v": 17, "n": 19}, {"v": 9, "n": 9}, {"v": 22, "n": 25}, {"v": 0, "n": 6}] },
];
export const usRow: Cell[] = [{"v": 15, "n": 16}, {"v": 29, "n": 32}, {"v": 11, "n": 12}, {"v": 7, "n": 8}, {"v": 14, "n": 19}, {"v": 9, "n": 9}, {"v": 23, "n": 25}, {"v": 5, "n": 6}];
export const cellClass = (c: Cell) => (c.v / c.n >= 0.8 ? "o" : c.v / c.n >= 0.4 ? "p" : "x");

// Bento chip logos, from public/logos: each project's own logo or GitHub org avatar, and line
// icons for ideas that aren't products. Crawl4AI and fastCRW use the icons from their own
// sites (crawl4ai.com, fastcrw.com): Crawl4AI's GitHub owner avatar is a personal photo.
export const chipLogos: Record<string, string> = {
  "Markdown for Agents": "cloudflare.jpg", "Firecrawl": "firecrawl.png", "Jina Reader": "jina.png",
  "Kernel": "kernel.png", "Browserbase": "browserbase.png", "Steel": "steel.png", "Anchor": "anchor.png",
  "Hyperbrowser": "hyperbrowser.png", "Browser Run": "cloudflare.jpg", "Parallel": "parallel.png",
  "Exa": "exa.png", "Brave": "brave.png", "Tavily": "tavily.png", "Playwright": "playwright.svg",
  "DevTools MCP": "devtools.png", "Stagehand": "browserbase.png", "Browser Use": "browser-use.png",
  "Skyvern": "skyvern.png", "Camoufox": "camoufox.svg", "Bright Data": "brightdata.png",
  "Scrapfly": "scrapfly.png", "ZenRows": "zenrows.png", "Claude": "claude.png", "GPT": "openai.png",
  "Gemini": "gemini.png", "Open-weight": "open-weight.svg", "Zyte": "zyte.png", "Apify": "apify.png",
  "Patchright": "patchright.png", "nodriver": "nodriver.png", "Web Bot Auth": "web-bot-auth.svg",
  "Saved sessions": "saved-sessions.svg", "Human handoff": "human-handoff.svg",
  "Crawl4AI": "crawl4ai.svg", "fastCRW": "fastcrw.png",
};

// Each tool's own docs page, keyed by the names the marquee and bento chips use. Integration
// pages link on to the provider. Tools Frankensurf pairs with rather than runs (Stagehand,
// DevTools MCP) have no page.
const integration = (slug: string) => `/docs/integrations/${slug}/`;
export const docsFor: Record<string, string> = {
  "Firecrawl": integration("firecrawl"), "Jina Reader": integration("jina-reader"), "Jina": integration("jina-reader"),
  "Crawl4AI": integration("crawl4ai"), "fastCRW": integration("fastcrw"), "Markdown for Agents": "/docs/read/markdown/",
  "Kernel": integration("kernel"), "Steel": integration("steel"), "Anchor": integration("anchor"),
  "Browserbase": integration("browserbase"), "Hyperbrowser": integration("hyperbrowser"),
  "Browser Run": integration("cloudflare-browser-run"), "Cloudflare": integration("cloudflare-browser-run"),
  "Parallel": integration("parallel"), "Exa": integration("exa"), "Brave": integration("brave"),
  "Brave Search": integration("brave"), "Tavily": integration("tavily"), "Playwright": integration("local-chromium"),
  "Skyvern": integration("skyvern"), "Browser Use": integration("browser-use"), "Camoufox": integration("camoufox"),
  "Patchright": integration("patchright"), "nodriver": integration("nodriver"), "Bright Data": integration("bright-data"),
  "ZenRows": integration("zenrows"), "Zyte": integration("zyte"), "Scrapfly": integration("scrapfly"),
  "Apify": integration("apify"), "Web Bot Auth": "/docs/blocked/signed-requests/",
  "Saved sessions": "/docs/read/signed-in/", "Human handoff": "/docs/blocked/handoff/",
  "Claude": "/docs/agents/", "GPT": "/docs/agents/", "Gemini": "/docs/agents/", "Open-weight": "/docs/agents/",
};

export const layers: {
  key: string; name: string;
  parts: { name: string; take: string; src: SourceKey; kind: "oss" | "managed" | "standard" }[];
}[] = [
  {
    key: "provider", name: "Model providers",
    parts: [
      { name: "Claude", take: "Native web fetch as a cheap first try. Documented: no JS rendering, only URLs already in the conversation.", src: "fcclaude", kind: "managed" },
      { name: "GPT", take: "Web search and native computer use, 93.0% on Online-Mind2Web. Search is Responses API only.", src: "om2w", kind: "managed" },
      { name: "Gemini", take: "URL context for static pages. Capped at 20 URLs and 34 MB per page.", src: "gem", kind: "managed" },
      { name: "Open-weight", take: "Any OpenAI-compatible endpoint. No native browsing, so Frankensurf supplies all of it.", src: "camps", kind: "standard" },
    ],
  },
  {
    key: "fetch", name: "Fetch + extract",
    parts: [
      { name: "Markdown for Agents", take: "Accept: text/markdown, about 80% fewer tokens where sites opt in.", src: "cfmd", kind: "standard" },
      { name: "Jina Reader", take: "Instant reads of cooperative pages.", src: "fastcrw", kind: "managed" },
      { name: "Firecrawl", take: "Crawl, map and schema extraction in one call.", src: "fastcrw", kind: "managed" },
      { name: "Crawl4AI", take: "Apache-2.0 self-hosted extraction, 59.95% truth recall.", src: "fastcrw", kind: "oss" },
      { name: "fastCRW", take: "Challenger: 63.74% recall on 819 labelled URLs.", src: "fastcrw", kind: "managed" },
    ],
  },
  {
    key: "runtime", name: "Browser runtimes",
    parts: [
      { name: "Kernel", take: "Apache-2.0, sub-150 ms cold starts, #1 ComputeSDK throughput.", src: "kernel", kind: "oss" },
      { name: "Steel", take: "Self-host or managed on one codebase.", src: "kernel", kind: "oss" },
      { name: "Anchor", take: "Managed auth and a hardened Chromium fork.", src: "kernel", kind: "managed" },
      { name: "Browserbase", take: "Session replay, logs, mature managed platform.", src: "kernel", kind: "managed" },
      { name: "Hyperbrowser", take: "Concurrency-first fleets.", src: "kernel", kind: "managed" },
      { name: "Browser Run", take: "Edge snapshots, PDFs and markdown.", src: "kernel", kind: "managed" },
    ],
  },
  {
    key: "search", name: "Search",
    parts: [
      { name: "Parallel", take: "91% SimpleQA, 348 ms mean, 74% BrowseComp.", src: "parallel", kind: "managed" },
      { name: "Exa", take: "Leads hit@5 at 80.9% in one verified benchmark.", src: "openb", kind: "managed" },
      { name: "Brave", take: "Independent index, 669 ms in AIMultiple's test.", src: "aim", kind: "managed" },
      { name: "Tavily", take: "Answer synthesis as a fallback route.", src: "openb", kind: "managed" },
    ],
  },
  {
    key: "control", name: "Control + agent loop",
    parts: [
      { name: "Playwright", take: "Deterministic workhorse for known flows.", src: "dapp", kind: "oss" },
      { name: "Stagehand", take: "Pairs with Frankensurf: act, extract and observe for pages an agent must click through. Not a fetch provider.", src: "dapp", kind: "oss" },
      { name: "Skyvern", take: "Vision-based forms with no selectors.", src: "kernel", kind: "oss" },
      { name: "DevTools MCP", take: "Pairs with Frankensurf: an agent drives Chrome directly, about 10x fewer tokens than snapshots. Not a fetch provider.", src: "egodt", kind: "oss" },
      { name: "Browser Use", take: "Autonomous loop, 97% Online-Mind2Web.", src: "om2w", kind: "oss" },
    ],
  },
  {
    key: "stealth", name: "Stealth",
    parts: [
      { name: "Camoufox", take: "Firefox fork, C++-level fingerprint spoofing.", src: "camofox", kind: "oss" },
      { name: "Patchright", take: "Patched Playwright on system Chrome.", src: "stealth", kind: "oss" },
      { name: "nodriver", take: "CDP-direct, 0 of 31 Cloudflare targets blocked in one test. AGPL-3.0.", src: "stealth", kind: "oss" },
    ],
  },
  {
    key: "unblock", name: "Unblockers",
    parts: [
      { name: "Bright Data", take: "97.9% on complex sites, 100% on PerimeterX.", src: "unb", kind: "managed" },
      { name: "ZenRows", take: "99% in its own test. Results swing by corpus.", src: "zen", kind: "managed" },
      { name: "Zyte", take: "Browser rendering behind Zyte's anti-ban network.", src: "zyte", kind: "managed" },
      { name: "Scrapfly", take: "92.6% DataDome, 90.7% Cloudflare, vendor-run.", src: "unb", kind: "managed" },
      { name: "Apify", take: "The RAG Web Browser actor: one page as HTML or markdown, no per-site scrapers.", src: "apify", kind: "managed" },
    ],
  },
  {
    key: "identity", name: "Identity",
    parts: [
      { name: "Web Bot Auth", take: "Every plain request signed, verified by Cloudflare, AWS, Akamai and Vercel. Checked against Cloudflare's verifier.", src: "signed", kind: "standard" },
      { name: "Saved sessions", take: "Your own signed-in Chrome, registered once. Agents read as you, never seeing a password or cookie. Other tools stay logged out.", src: "kernel", kind: "managed" },
      { name: "Human handoff", take: "Sends CAPTCHAs, 2FA and sign-ins to you, then resumes in the same tab.", src: "fsid", kind: "oss" },
    ],
  },
];

// Measured 11 Oct 2026 with v0.35 on 150 sites in 15 categories chosen before any
// was read and read once (held-out set 7; benchmarks/2026-10-11/README.md).
export const stats = [
  { value: "96.0%", label: "of 150 fresh sites read right", note: "Firecrawl Only 84.7%" },
  { value: "−74%", label: "dead ends vs Firecrawl Only", note: "6 vs 23 of 150 sites" },
  { value: "¼", label: "the cost of Firecrawl Only", note: "$0.98 vs $4.28 per 1k pages" },
  { value: "99%", label: "of sites any tool could read", note: "Firecrawl Only 87%" },
];

// Share of sites failed (percent) on the same 150 fresh sites: today = Firecrawl
// alone; target = Frankensurf with paid fallbacks.
export const failures: { label: string; today: number; target: number; src: SourceKey }[] = [
  { label: "All 150 sites", today: 15.3, target: 4.0, src: "bench" },
  { label: "US retail (10)", today: 30.0, target: 10.0, src: "bench" },
  { label: "UK and EU retail (10)", today: 10.0, target: 10.0, src: "bench" },
  { label: "Australian and NZ retail (10)", today: 20.0, target: 10.0, src: "bench" },
  { label: "Asian and other retail (10)", today: 10.0, target: 0.0, src: "bench" },
  { label: "News (11)", today: 9.1, target: 0.0, src: "bench" },
  { label: "Classifieds (6)", today: 16.7, target: 0.0, src: "bench" },
  { label: "Developer sites (11)", today: 9.1, target: 0.0, src: "bench" },
  { label: "Jobs (11)", today: 9.1, target: 0.0, src: "bench" },
  { label: "Social (10)", today: 20.0, target: 10.0, src: "bench" },
  { label: "Government (11)", today: 9.1, target: 0.0, src: "bench" },
  { label: "Cars, property, travel, media, food (50)", today: 18.0, target: 4.0, src: "bench" },
];

// Head to head, measured 11 Oct 2026 on held-out set 7 (150 sites chosen before
// any was read, read once): each tool alone and Frankensurf the same day
// (benchmarks/2026-10-11/toolbench-heldout7-*.jsonl). all = sites read right;
// reach = share of the 146 sites at least one tool read; cost per 1,000 pages
// read right. Frankensurf is the stitched router with paid fallbacks. ZenRows
// and Scrapfly are left out: their plans had run out of credits. Every compared
// tool is also one of Frankensurf's own providers, so each is labelled
// "<Tool> Only" (Frankensurf against that provider used on its own), never as a
// rival (owner, 6 Oct 2026).
// Search, judged blind (benchmarks/2026-10-13-search): useful results in the top five.
export const searchStats = [
  { value: "35 vs 5", label: "queries where free search beat Firecrawl's search", note: "the rest level · blind set 6" },
  { value: "4.67 / 5", label: "useful results with paid search allowed", note: "Exa Only 4.66 · level, for less" },
  { value: "$0", label: "per 1,000 searches, free", note: "with your own search engine · no Docker" },
];

export const usVersus = { all: [144, 150], unseen: 98.6, cost: "$0.98" };
// The same router with no paid tools at all, on the same sites.
export const usFree: [number, number] = [132, 150];
export const versus: { name: string; logo: string; all: [number, number]; unseen: number; cost: string }[] = [
  { name: "Zyte", logo: "zyte.png", all: [129, 150], unseen: 88.4, cost: "not reported" },
  { name: "Firecrawl", logo: "firecrawl.png", all: [127, 150], unseen: 87.0, cost: "$4.28" },
  { name: "Jina Reader", logo: "jina.png", all: [110, 150], unseen: 75.3, cost: "free" },
  { name: "Scrapling", logo: "scrapling.png", all: [103, 150], unseen: 70.5, cost: "free" },
  { name: "Camoufox", logo: "camoufox.svg", all: [103, 150], unseen: 70.5, cost: "free" },
  { name: "Plain HTTP", logo: "http.svg", all: [73, 150], unseen: 50.0, cost: "free" },
  { name: "Headless Chromium", logo: "chromium.png", all: [61, 150], unseen: 41.8, cost: "free" },
  { name: "Patchright", logo: "patchright.png", all: [58, 150], unseen: 39.7, cost: "free" },
];

// How the stack changes itself. These describe shipped behaviour, so they carry no label.
export const ledger = [
  { when: "10 Oct 2026", layer: "Router", change: "Slow reads raced after 3 s, paid tools after the first wall, wrong URLs found through the site's own search", delta: "−1.4 s", detail: "Paid fallbacks on 40 held-out sites, one read at a time: median 5.1 → 3.6 s, 36 → 37 read" },
  { when: "6 Oct 2026", layer: "Providers", change: "Kernel, Anchor, Zyte, Apify, fastCRW, Tavily, Parallel, Patchright and nodriver bolted on", delta: "+9", detail: "Patchright, nodriver and self-hosted fastCRW read live pages; the rest are tested against their published APIs" },
  { when: "6 Oct 2026", layer: "Router", change: "A rendered second opinion catches pages whose results never loaded", delta: "+13 pts", detail: "Free tools only, on held-out sites: 34.8% → 47.8% of pages read right" },
  { when: "5 Oct 2026", layer: "Checks", change: "Challenge, sign-in and empty pages stop counting as success", delta: "82/82", detail: "Site benchmark from 77 to 82 of 82 pages read right, with no false successes" },
];

// share: measured share of the 113 pages the stitched router read right on
// 6 Oct 2026, by the rung that served them (p50 measured per rung). tokens and
// cost per 1k pages remain targets.
export const rungs = [
  { id: "T0", name: "Content negotiation", p50: "1.3 s", tokens: "1.5–3k", cost: "$0.05", share: 39 },
  { id: "T1", name: "Light fetch + extract", p50: "—", tokens: "2–4k", cost: "$0.30", share: 1 },
  { id: "T2", name: "Managed browser", p50: "7.6 s", tokens: "3–6k", cost: "$1.80", share: 15 },
  { id: "T3", name: "Stealth + signed identity", p50: "10.8 s", tokens: "3–6k", cost: "$3.50", share: 23 },
  { id: "T4", name: "Unblocker network", p50: "6.8 s", tokens: "3–6k", cost: "$6.00", share: 22 },
  { id: "T5", name: "Human handoff", p50: "when asked", tokens: "<1k", cost: "n/a", share: 0 },
];
