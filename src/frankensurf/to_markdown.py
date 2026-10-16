"""HTML to markdown, for agents: headings, paragraphs, lists, links, images, emphasis, code,
tables and quotes, with links and images made absolute. BeautifulSoup only.

Scripts, styles, forms and other things a reader never sees are dropped. The whole page is
converted (menus included); use main_content for the article alone.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

_DROP = {"script", "style", "noscript", "template", "svg", "canvas", "iframe", "object", "embed",
         "head", "form", "button", "input", "select", "textarea", "dialog", "map"}
_BLOCK = {"p", "div", "section", "article", "main", "header", "footer", "nav", "aside", "figure",
          "figcaption", "address", "details", "summary", "dl", "dd", "dt", "center", "body", "html"}
_SPACE = re.compile(r"[ \t\r\n\f\v]+")


def html_to_markdown(html: str, base_url: str = "") -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for node in soup.find_all(string=lambda value: isinstance(value, Comment)):
        node.extract()
    for node in soup.find_all(list(_DROP)):
        node.decompose()
    for node in soup.find_all(attrs={"hidden": True}):
        node.decompose()
    for node in soup.find_all(attrs={"aria-hidden": "true"}):
        node.decompose()
    root = soup.body or soup
    text = _blocks(root, base_url, 0)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n" if text.strip() else ""


def _inline(node, base: str) -> str:
    if isinstance(node, NavigableString):
        return _SPACE.sub(" ", str(node))
    if not isinstance(node, Tag):
        return ""
    name = node.name
    if name == "br":
        return "  \n"
    if name == "img":
        alt = _SPACE.sub(" ", node.get("alt") or "").strip()
        src = node.get("src") or node.get("data-src") or ""
        if not src or src.startswith("data:"):
            return alt
        return f"![{alt}]({urljoin(base, src)})"
    inner = "".join(_inline(child, base) for child in node.children)
    if name == "a":
        href = (node.get("href") or "").strip()
        label = inner.strip()
        if not href or href.startswith(("javascript:", "#")) or not label:
            return inner
        return f"[{label}]({urljoin(base, href)})"
    if name in ("strong", "b"):
        return f"**{inner.strip()}**" if inner.strip() else inner
    if name in ("em", "i"):
        return f"*{inner.strip()}*" if inner.strip() else inner
    if name == "code":
        return f"`{inner.strip()}`" if inner.strip() else ""
    if name in ("del", "s", "strike"):
        return f"~~{inner.strip()}~~" if inner.strip() else ""
    if name in _BLOCK or name in ("ul", "ol", "table", "pre", "blockquote", "hr") or re.fullmatch(r"h[1-6]", name):
        return "\n\n" + _blocks(node, base, 0) + "\n\n"
    return inner


def _blocks(node, base: str, depth: int) -> str:
    out, line = [], []

    def flush():
        text = "".join(line).strip()
        if text:
            out.append(re.sub(r" *\n *", "\n", re.sub(r" {2,}", " ", text)))
        line.clear()

    for child in node.children:
        if isinstance(child, NavigableString):
            line.append(_SPACE.sub(" ", str(child)))
            continue
        if not isinstance(child, Tag):
            continue
        name = child.name
        heading = re.fullmatch(r"h([1-6])", name)
        if heading:
            flush()
            text = _SPACE.sub(" ", "".join(_inline(c, base) for c in child.children)).strip()
            if text:
                out.append("#" * int(heading.group(1)) + " " + text)
        elif name in ("ul", "ol"):
            flush()
            out.append(_list(child, base, depth, ordered=name == "ol"))
        elif name == "pre":
            flush()
            code = child.get_text()
            out.append("```\n" + code.strip("\n") + "\n```")
        elif name == "blockquote":
            flush()
            quoted = _blocks(child, base, depth)
            out.append("\n".join("> " + row if row else ">" for row in quoted.splitlines()))
        elif name == "table":
            flush()
            out.append(_table(child, base))
        elif name == "hr":
            flush()
            out.append("---")
        elif name in _BLOCK or name == "li":
            flush()
            inner = _blocks(child, base, depth)
            if inner:
                out.append(inner)
        else:
            line.append(_inline(child, base))
    flush()
    return "\n\n".join(part for part in out if part.strip())


def _list(node, base: str, depth: int, ordered: bool) -> str:
    rows, number = [], 1
    for item in node.find_all("li", recursive=False):
        nested = [child for child in item.children if isinstance(child, Tag) and child.name in ("ul", "ol")]
        for child in nested:
            child.extract()
        body = _blocks(item, base, depth + 1).replace("\n\n", "\n")
        marker = f"{number}." if ordered else "-"
        indent = "  " * depth
        if body:
            first, *rest = body.splitlines()
            rows.append(f"{indent}{marker} {first}")
            rows.extend(f"{indent}  {row}" for row in rest)
        for child in nested:
            rows.append(_list(child, base, depth + 1, ordered=child.name == "ol"))
        number += 1
    return "\n".join(row for row in rows if row.strip())


def _table(node, base: str) -> str:
    rows = []
    for tr in node.find_all("tr"):
        cells = [_SPACE.sub(" ", "".join(_inline(c, base) for c in cell.children)).strip().replace("|", "\\|")
                 for cell in tr.find_all(["th", "td"], recursive=False)]
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
    lines += ["| " + " | ".join(row) + " |" for row in rows[1:]]
    return "\n".join(lines)
