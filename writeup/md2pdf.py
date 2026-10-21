"""Render a Markdown file to PDF with headless Chrome: Times New Roman 12 pt, US Letter, no header/footer, no byline.

usage: python writeup/md2pdf.py writeup/draft.md writeup/out.pdf
Handles the subset the write-up uses: # headings, paragraphs, **bold**, *italic*, `code`, pipe tables, nested bullet
and numbered lists, > quotes. Local file only; nothing is fetched.
"""

from __future__ import annotations

import html
import re
import subprocess
import sys
import tempfile
from pathlib import Path

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CSS = """
@page { size: Letter; margin: 1in; }
body { font-family: "Times New Roman", Times, serif; font-size: 12pt; line-height: 1.35; color: #000; }
h1 { font-size: 16pt; margin: 0 0 10pt 0; }
h2 { font-size: 13.5pt; margin: 16pt 0 6pt 0; }
h3 { font-size: 12pt; margin: 12pt 0 4pt 0; }
p { margin: 0 0 7pt 0; }
table { border-collapse: collapse; margin: 4pt 0 9pt 0; width: 100%; font-size: 10.5pt; page-break-inside: avoid; }
th, td { border: 0.5pt solid #000; padding: 2pt 4pt; vertical-align: top; text-align: left; }
th { font-weight: bold; }
code { font-family: "Courier New", Courier, monospace; font-size: 10.5pt; }
ul, ol { margin: 0 0 7pt 0; padding-left: 20pt; }
li { margin: 0 0 3pt 0; }
blockquote { margin: 4pt 0 6pt 16pt; font-style: italic; }
"""


def inline(s: str) -> str:
    s = html.escape(s, quote=False)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<![*\w])\*([^*\n]+)\*(?![*\w])", r"<i>\1</i>", s)
    return s


def convert(md: str) -> str:
    lines = md.splitlines()
    out: list[str] = []
    stack: list[tuple[str, int]] = []  # (tag, indent)
    i = 0

    def close_lists(to_indent: int = -1):
        while stack and stack[-1][1] > to_indent:
            out.append(f"</li></{stack.pop()[0]}>")

    while i < len(lines):
        line = lines[i]
        if not line.strip():
            close_lists()
            i += 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            close_lists()
            n = len(m.group(1))
            out.append(f"<h{n}>{inline(m.group(2))}</h{n}>")
            i += 1
            continue
        if line.lstrip().startswith("|"):
            close_lists()
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            body = [r for r in rows if not all(re.fullmatch(r":?-{2,}:?", c) for c in r)]
            out.append("<table><tr>" + "".join(f"<th>{inline(c)}</th>" for c in body[0]) + "</tr>")
            for r in body[1:]:
                out.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>")
            out.append("</table>")
            continue
        if line.startswith(">"):
            close_lists()
            out.append(f"<blockquote>{inline(line.lstrip('> ').strip())}</blockquote>")
            i += 1
            continue
        m = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)$", line)
        if m:
            indent = len(m.group(1))
            tag = "ol" if m.group(2)[0].isdigit() else "ul"
            if stack and stack[-1][1] == indent:
                out.append("</li>")
            elif stack and stack[-1][1] > indent:
                close_lists(indent)
                out.append("</li>")
            if not stack or stack[-1][1] < indent:
                out.append(f"<{tag}>")
                stack.append((tag, indent))
            out.append(f"<li>{inline(m.group(3))}")
            i += 1
            continue
        if stack and line.startswith(" "):  # continuation inside a list item
            out.append(" " + inline(line.strip()))
            i += 1
            continue
        close_lists()
        para = [line.strip()]
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(r"^(#|\||>|\s*([-*]|\d+\.)\s)", lines[i]):
            para.append(lines[i].strip())
            i += 1
        out.append(f"<p>{inline(' '.join(para))}</p>")
    close_lists()
    return "\n".join(out)


def main():
    src, dst = Path(sys.argv[1]), Path(sys.argv[2]).resolve()
    md = src.read_text()
    m = re.search(r"^#\s+(.*)$", md, re.M)
    title = html.escape(re.sub(r"[*`]", "", m.group(1))) if m else src.stem
    doc = (f"<!doctype html><html><head><meta charset='utf-8'><title>{title}</title><style>{CSS}</style></head>"
           f"<body>{convert(md)}</body></html>")
    with tempfile.TemporaryDirectory() as d:
        page = Path(d) / "doc.html"
        page.write_text(doc)
        subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-pdf-header-footer", "--no-first-run",
                        f"--user-data-dir={d}/profile", f"--print-to-pdf={dst}", page.as_uri()],
                       check=True, capture_output=True, timeout=120)
    print("wrote", dst)


if __name__ == "__main__":
    main()
