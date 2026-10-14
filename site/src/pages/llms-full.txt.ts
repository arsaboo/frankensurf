// /llms-full.txt: every docs page as Markdown in one file, in sidebar-agnostic id order, so a model
// can take in the whole documentation in one fetch (https://llmstxt.org).
import { getCollection } from "astro:content";
import { docMarkdown, docPath } from "../lib/docmd";

export async function GET() {
  const pages = (await getCollection("docs")).sort((a, b) => a.id.localeCompare(b.id));
  const body = pages
    .map((page) => `<!-- https://frankensurf.dev${docPath(page.id)} -->\n${docMarkdown(page)}`)
    .join("\n---\n\n");
  return new Response(body, { headers: { "Content-Type": "text/plain; charset=utf-8" } });
}
