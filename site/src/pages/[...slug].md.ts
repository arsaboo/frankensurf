// Every docs page as raw Markdown at the same address plus ".md" (/docs/read.md), for language
// models and for the "View as Markdown" and "Open in ChatGPT / Claude" buttons on each page.
import { getCollection } from "astro:content";
import { docMarkdown } from "../lib/docmd";

export async function getStaticPaths() {
  return (await getCollection("docs")).map((entry) => ({ params: { slug: entry.id }, props: { entry } }));
}

export function GET({ props }: { props: { entry: Parameters<typeof docMarkdown>[0] } }) {
  return new Response(docMarkdown(props.entry), { headers: { "Content-Type": "text/markdown; charset=utf-8" } });
}
