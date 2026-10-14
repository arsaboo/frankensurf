// A docs page as plain Markdown for language models: the title, the description, then the body
// with MDX imports and layout-only component tags removed (prose, tables and code stay).
export function docMarkdown(entry: { data: { title: string; description?: string }; body?: string }) {
  const { title, description } = entry.data;
  const body = (entry.body ?? "")
    .replace(/^import .*$/gm, "")
    .replace(/^\s*<\/?(Tabs|TabItem|Steps|CardGrid|LinkCard|Card|Aside|WeeklyBenchmark)\b[^>]*>\s*$/gm, "")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
  return `# ${title}\n\n${description ? description + "\n\n" : ""}${body}\n`;
}

// Where a docs entry lives: "docs" -> /docs/, "docs/read" -> /docs/read/; its Markdown twin drops the slash.
export const docPath = (id: string) => `/${id}/`;
export const docMarkdownPath = (id: string) => `/${id}.md`;
