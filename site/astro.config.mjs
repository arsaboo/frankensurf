import { defineConfig } from "astro/config";
import starlight from "@astrojs/starlight";

// The landing page lives in src/pages; Starlight serves the docs under /docs
// (its content sits in src/content/docs/docs/ so every slug starts with docs/).
export default defineConfig({
  site: "https://frankensurf.dev",
  prefetch: { prefetchAll: false, defaultStrategy: "hover" },
  server: { host: true, port: 4322 },
  // Polling sees edits made from Windows through the \\wsl share, which inotify misses.
  vite: { server: { watch: { usePolling: true, interval: 300 } } },
  integrations: [
    starlight({
      title: "Frankensurf",
      description: "Documentation for Frankensurf: one call that gets your agent through any site, on any model.",
      logo: { dark: "./src/assets/stitched-wave-mint.svg", light: "./src/assets/stitched-wave-ink.svg", replacesTitle: false },
      favicon: "/favicon-stitched-wave.svg",
      head: [{ tag: "link", attrs: { rel: "apple-touch-icon", sizes: "180x180", href: "/apple-touch-icon.png" } }],
      social: [{ icon: "github", label: "GitHub", href: "https://github.com/yail259/frankensurf" }],
      customCss: ["./src/styles/docs.css"],
      components: { PageTitle: "./src/components/PageTitle.astro" },
      expressiveCode: {
        themes: ["github-dark-dimmed", "github-light"],
        styleOverrides: {
          borderRadius: "10px",
          borderColor: "var(--sl-color-hairline-light)",
          codeFontFamily: "var(--sl-font-mono)",
          codeFontSize: "0.85rem",
          frames: {
            editorBackground: "var(--sl-color-gray-6)",
            terminalBackground: "var(--sl-color-gray-6)",
            editorActiveTabIndicatorTopColor: "var(--sl-color-accent)",
            terminalTitlebarDotsForeground: "var(--sl-color-gray-4)",
            frameBoxShadowCssValue: "none",
          },
        },
      },
      editLink: { baseUrl: "https://github.com/yail259/frankensurf/edit/main/site/" },
      lastUpdated: false,
      // Grouped by what you are trying to do, after the Firecrawl docs: get started, the core
      // calls (each with its features nested), getting past walls, integrations, then reference.
      sidebar: [
        { label: "Get started", items: [
          { label: "Introduction", slug: "docs" },
          { label: "Install", slug: "docs/install" },
          { label: "Add to your agent", slug: "docs/agents" },
          { label: "Agent frameworks", slug: "docs/frameworks" },
        ] },
        { label: "Core calls", items: [
          { label: "Read", collapsed: false, items: [
            { label: "Read a page", slug: "docs/read" },
            { label: "Markdown", slug: "docs/read/markdown" },
            { label: "JavaScript pages", slug: "docs/read/javascript" },
            { label: "Page data", slug: "docs/read/page-data" },
            { label: "Images", slug: "docs/read/images" },
            { label: "PDFs", slug: "docs/read/pdfs" },
            { label: "Batch read", slug: "docs/read/batch" },
            { label: "Lightning mode", slug: "docs/read/lightning" },
            { label: "Signed-in pages", slug: "docs/read/signed-in" },
            { label: "Profiles", slug: "docs/read/profiles" },
            { label: "Site modules", slug: "docs/read/site-modules" },
            { label: "Main content", slug: "docs/read/main-content" },
            { label: "Structured fields", slug: "docs/read/structured" },
          ] },
          { label: "Search", slug: "docs/search" },
          { label: "Watch", slug: "docs/watch" },
          { label: "Paginate", slug: "docs/paginate" },
          { label: "Act", slug: "docs/act" },
        ] },
        { label: "When a site blocks you", items: [
          { label: "How escalation works", slug: "docs/blocked/escalation" },
          { label: "Your real browser", slug: "docs/blocked/real-browser" },
          { label: "More than one connection", slug: "docs/blocked/egress" },
          { label: "Human handoff", slug: "docs/blocked/handoff" },
          { label: "Signed requests", slug: "docs/blocked/signed-requests" },
          { label: "Paid tools and budgets", slug: "docs/blocked/paid-tools" },
        ] },
        { label: "Integrations", items: [
          { label: "Overview", slug: "docs/integrations" },
          { label: "Fetch and render", collapsed: true, items: [
            "docs/integrations/http", "docs/integrations/jina-reader", "docs/integrations/local-chromium",
            "docs/integrations/steel", "docs/integrations/crawl4ai", "docs/integrations/browserbase",
            "docs/integrations/hyperbrowser", "docs/integrations/browserless", "docs/integrations/kernel",
            "docs/integrations/anchor", "docs/integrations/cloudflare-browser-run",
          ] },
          { label: "Stealth", collapsed: true, items: [
            "docs/integrations/camoufox", "docs/integrations/scrapling", "docs/integrations/patchright", "docs/integrations/nodriver",
          ] },
          { label: "Scrapers and unblockers", collapsed: true, items: [
            "docs/integrations/firecrawl", "docs/integrations/zenrows", "docs/integrations/fastcrw", "docs/integrations/scrapfly",
            "docs/integrations/bright-data", "docs/integrations/zyte", "docs/integrations/apify",
          ] },
          { label: "Agents", collapsed: true, items: ["docs/integrations/browser-use", "docs/integrations/skyvern"] },
          { label: "Search", collapsed: true, items: [
            "docs/integrations/searxng", "docs/integrations/duckduckgo", "docs/integrations/bing",
            "docs/integrations/exa", "docs/integrations/brave", "docs/integrations/tavily", "docs/integrations/parallel",
          ] },
        ] },
        { label: "Reference", items: [
          { label: "Python", slug: "docs/reference/python" },
          { label: "Command line", slug: "docs/reference/cli" },
          { label: "MCP tools", slug: "docs/reference/mcp" },
          { label: "Settings", slug: "docs/reference/settings" },
          { label: "Errors", slug: "docs/reference/errors" },
          { label: "Receipts", slug: "docs/reference/receipts" },
          { label: "Environment and files", slug: "docs/reference/environment" },
        ] },
        { label: "More", items: [
          { label: "Benchmarks", slug: "docs/more/benchmarks" },
            { label: "Weekly benchmark", slug: "docs/more/weekly" },
          { label: "Repairing failed extracts", slug: "docs/more/repair" },
          { label: "Writing a plugin", slug: "docs/more/plugins" },
          { label: "Code map and boundaries", slug: "docs/more/code-map" },
        ] },
      ],
    }),
  ],
});
