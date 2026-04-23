"""In-memory Markdown → Apple-style PDF pipeline.

Ported from huashu-md-to-pdf (花叔 skill, 2025-12-24). The only behavioural
change is the entry point: instead of reading a file and writing a file,
:func:`markdown_to_pdf_bytes` takes the Markdown string and returns the
PDF bytes, so the agent can stream them straight to the HTTP response.

Outline contract (same as upstream — do not change without updating the
system prompt in ``server.py``):

* Main chapters: ``## 1. Title``   (number + dot + space + title)
* Sub chapters:  ``### 1.1 Title`` (two-level numeric)

Any heading that doesn't match gets skipped by the TOC extractor; the
chapter still renders but it won't show up in the cover-page index.
"""

from __future__ import annotations

import io
import re

import markdown2
from weasyprint import CSS, HTML


# ─────────────────────────────────────────────────────────────────────
#  Metadata / TOC extraction
# ─────────────────────────────────────────────────────────────────────

def extract_metadata(md_content: str) -> dict:
    metadata: dict = {
        "title": None,
        "subtitle": None,
        "author": None,
        "date": None,
        "created_for": None,
        "based_on": None,
    }
    h1_match = re.search(r"^# (.+)$", md_content, re.MULTILINE)
    if h1_match:
        metadata["title"] = h1_match.group(1).strip()

    for key, pattern in [
        ("author", r"\*\*创建者\*\*:\s*(.+?)$"),
        ("based_on", r"\*\*基于\*\*:\s*(.+?)$"),
        ("date", r"\*\*最后更新\*\*:\s*(.+?)$"),
    ]:
        m = re.search(pattern, md_content, re.MULTILINE)
        if m:
            metadata[key] = m.group(1).strip()

    for_match = re.search(r"\*\*为谁创建\*\*:\s*(.+?)$", md_content, re.MULTILINE)
    if for_match:
        link_match = re.search(r"\[(.+?)\]\((.+?)\)", for_match.group(1))
        if link_match:
            metadata["created_for"] = link_match.group(1)
            metadata["created_for_url"] = link_match.group(2)
        else:
            metadata["created_for"] = for_match.group(1).strip()

    return metadata


def extract_toc_structure(md_content: str) -> list[dict]:
    toc: list[dict] = []
    for line in md_content.split("\n"):
        m2 = re.match(r"^## (\d+)\.\s+(.+)$", line)
        if m2:
            num = m2.group(1)
            title = re.sub(r"[\U0001F300-\U0001F9FF]", "", m2.group(2).strip()).strip()
            toc.append({
                "level": 2,
                "number": num,
                "title": title,
                "id": f"{num}-{title}".replace(" ", "-").replace(":", "").lower(),
            })
            continue

        m3 = re.match(r"^### (\d+\.\d+)\s+(.+)$", line)
        if m3:
            num = m3.group(1)
            title = re.sub(r"[\U0001F300-\U0001F9FF]", "", m3.group(2).strip()).strip()
            if len(title) > 50:
                title = title[:47] + "..."
            toc.append({
                "level": 3,
                "number": num,
                "title": title,
                "id": f"{num}-{title}".replace(" ", "-").replace(":", "").replace(".", "-").lower(),
            })

    return toc


def generate_toc_html(toc_items: list[dict]) -> str:
    if not toc_items:
        return ""
    parts = []
    for item in toc_items:
        cls = "toc-h2" if item["level"] == 2 else "toc-h3"
        parts.append(
            f'<div class="toc-item {cls}">'
            f'<a href="#{item["id"]}" class="toc-link">'
            f'<span class="toc-number">{item["number"]}</span>'
            f'<span class="toc-title">{item["title"]}</span>'
            f"</a></div>"
        )
    return "\n".join(parts)


def create_cover_and_toc(metadata: dict, toc_html: str) -> str:
    title = metadata.get("title") or "文档"
    subtitle = metadata.get("subtitle") or ""
    author = metadata.get("author") or ""
    date = metadata.get("date") or ""
    created_for = metadata.get("created_for") or ""
    created_for_url = metadata.get("created_for_url") or ""
    based_on = metadata.get("based_on") or ""

    toc_section = (
        f'<div class="toc-page"><h2 class="toc-header">目录</h2>'
        f'<div class="toc-content">{toc_html}</div></div>'
        if toc_html
        else ""
    )

    meta_items: list[str] = []
    if subtitle:
        meta_items.append(f'<p class="cover-subtitle">{subtitle}</p>')
    if based_on:
        meta_items.append(f'<p class="cover-based">{based_on}</p>')
    if created_for:
        if created_for_url:
            meta_items.append(
                f'<p class="cover-for">为 <a href="{created_for_url}">{created_for}</a> 用户创建</p>'
            )
        else:
            meta_items.append(f'<p class="cover-for">为 {created_for} 用户创建</p>')
    if author:
        meta_items.append(f'<p class="cover-author">{author}</p>')
    if date:
        meta_items.append(f'<p class="cover-date">{date}</p>')
    meta_html = "\n".join(meta_items)

    footer_items = [x for x in (author, date) if x]
    footer_text = " · ".join(footer_items)

    return f"""
    <div class="apple-cover">
      <div class="cover-top"><div class="cover-badge">R E P O R T</div></div>
      <div class="cover-main">
        <h1 class="cover-title">{title}</h1>
        <div class="cover-divider"></div>
        <div class="cover-meta">{meta_html}</div>
      </div>
      <div class="cover-bottom">
        <div class="cover-footer-line"></div>
        <p class="cover-footer-text">Powered by AI · Data-Driven Analysis{" · " + footer_text if footer_text else ""}</p>
      </div>
    </div>
    {toc_section}
    """


def process_markdown(md_content: str) -> str:
    # strip the first h1 (already on the cover) and any stray metadata lines
    md_content = re.sub(r"^# .+?\n", "", md_content, count=1, flags=re.MULTILINE)
    for pat in [
        r"^\*\*创建者\*\*:.+?$",
        r"^\*\*为谁创建\*\*:.+?$",
        r"^\*\*基于\*\*:.+?$",
        r"^\*\*最后更新\*\*:.+?$",
        r"^\*\*适用场景\*\*:.+?$",
    ]:
        md_content = re.sub(pat, "", md_content, flags=re.MULTILINE)

    md_content = re.sub(r"[\U0001F300-\U0001F9FF]", "", md_content)

    def add_h2_id(match: re.Match) -> str:
        num, title = match.group(1), match.group(2).strip()
        id_str = f"{num}-{title}".replace(" ", "-").replace(":", "").lower()
        return f'\n<div class="chapter-break"></div>\n\n<h2 id="{id_str}">{num}. {title}</h2>\n'

    md_content = re.sub(r"\n## (\d+)\.\s+(.+?)\n", add_h2_id, md_content)

    def add_h3_id(match: re.Match) -> str:
        num, title = match.group(1), match.group(2).strip()
        id_str = f"{num}-{title}".replace(" ", "-").replace(":", "").replace(".", "-").lower()
        return f'\n<h3 id="{id_str}">{num} {title}</h3>\n'

    md_content = re.sub(r"\n### (\d+\.\d+)\s+(.+?)\n", add_h3_id, md_content)

    html = markdown2.markdown(
        md_content,
        extras=[
            "fenced-code-blocks",
            "tables",
            "break-on-newline",
            "code-friendly",
            "cuddled-lists",
            "strike",
            "task_list",
        ],
    )

    html = re.sub(r"<table>", r'<table class="content-table">', html)
    html = re.sub(r"<pre><code", r'<pre class="code-block"><code', html)
    html = re.sub(r"<blockquote>", r'<blockquote class="quote-block">', html)
    return html


# ─────────────────────────────────────────────────────────────────────
#  CSS — Apple-style whitepaper
# ─────────────────────────────────────────────────────────────────────

_APPLE_CSS = """
@page {
    size: A4;
    margin: 2.5cm 2cm 2cm 2cm;

    @top-left {
        content: string(doc-title);
        font-size: 8.5pt;
        color: #86868b;
        font-family: -apple-system, BlinkMacSystemFont, 'PingFang SC', 'Noto Sans CJK SC', sans-serif;
    }

    @top-right {
        content: counter(page);
        font-size: 8.5pt;
        color: #86868b;
        font-family: -apple-system, BlinkMacSystemFont, 'PingFang SC', 'Noto Sans CJK SC', sans-serif;
    }
}

@page:first {
    margin: 0;
    @top-left { content: none; }
    @top-right { content: none; }
}

@page:nth(2) {
    @top-left { content: none; }
    @top-right { content: none; }
}

* {
    margin: 0;
    padding: 0;
    box-sizing: border-box;
}

body {
    font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Text', 'PingFang SC', 'Noto Sans CJK SC', 'Noto Sans', sans-serif;
    font-size: 11pt;
    line-height: 1.7;
    color: #1d1d1f;
    background: white;
    -webkit-font-smoothing: antialiased;
}

.apple-cover {
    background: linear-gradient(160deg, #0a0a0a 0%, #1a1a2e 40%, #16213e 70%, #0f3460 100%);
    page-break-after: always;
    padding: 0 70px;
    min-height: 297mm;
    overflow: hidden;
}

.cover-top {
    padding-top: 70px;
    padding-bottom: 250px;
}

.cover-badge {
    display: inline-block;
    font-size: 9pt;
    font-weight: 600;
    letter-spacing: 3px;
    color: rgba(255,255,255,0.5);
    border: 1px solid rgba(255,255,255,0.2);
    padding: 6px 16px;
    border-radius: 4px;
}

.cover-main {
    text-align: left;
    max-width: 90%;
    padding-bottom: 180px;
}

.cover-title {
    font-size: 32pt;
    font-weight: 700;
    color: #ffffff;
    margin-bottom: 0;
    letter-spacing: -0.5px;
    line-height: 1.4;
    string-set: doc-title content();
}

.cover-divider {
    width: 60px;
    height: 3px;
    background: linear-gradient(90deg, #06c, #5ac8fa);
    margin: 28px 0;
    border-radius: 2px;
}

.cover-subtitle {
    font-size: 15pt;
    color: rgba(255,255,255,0.7);
    margin-bottom: 12px;
    line-height: 1.6;
}

.cover-meta {
    font-size: 12pt;
    color: rgba(255,255,255,0.45);
    line-height: 2;
    margin-top: 8px;
}

.cover-based { font-size: 11pt; color: rgba(255,255,255,0.45); margin-bottom: 8px; }
.cover-for   { font-size: 13pt; color: rgba(255,255,255,0.7); font-weight: 500; margin-bottom: 8px; }
.cover-for a { color: #5ac8fa; text-decoration: none; }
.cover-author{ font-size: 11pt; color: rgba(255,255,255,0.45); margin-bottom: 8px; }
.cover-date  { font-size: 11pt; color: rgba(255,255,255,0.45); font-weight: 500; }

.cover-bottom { padding-top: 0; }
.cover-footer-line {
    height: 1px;
    background: linear-gradient(90deg, rgba(255,255,255,0.15) 0%, rgba(255,255,255,0) 100%);
}
.cover-footer-text {
    font-size: 9pt;
    color: rgba(255,255,255,0.3);
    margin-top: 12px;
    letter-spacing: 0.5px;
}

.toc-page {
    padding: 60px 50px;
    page-break-after: always;
    min-height: 100vh;
}
.toc-header {
    font-size: 28pt;
    font-weight: 600;
    color: #1d1d1f;
    margin-bottom: 32px;
}
.toc-content { column-count: 2; column-gap: 40px; }
.toc-item { break-inside: avoid; margin-bottom: 6px; }
.toc-h2 { margin-top: 14px; margin-bottom: 4px; }
.toc-h2 .toc-link { font-size: 11.5pt; font-weight: 600; color: #1d1d1f; }
.toc-h2 .toc-number { color: #06c; font-weight: 700; margin-right: 8px; }
.toc-h3 { margin-left: 16px; }
.toc-h3 .toc-link { font-size: 10pt; font-weight: 400; color: #424245; }
.toc-h3 .toc-number { color: #86868b; margin-right: 6px; font-size: 9.5pt; }
.toc-link { display: block; text-decoration: none; padding: 4px 0; }
.toc-number { font-feature-settings: "tnum"; }

.chapter-break { page-break-before: always; height: 0; }

h2 {
    font-size: 22pt;
    font-weight: 600;
    color: #1d1d1f;
    margin-top: 0;
    margin-bottom: 28px;
    padding-bottom: 12px;
    border-bottom: 2px solid #d2d2d7;
    page-break-after: avoid;
}
h3 {
    font-size: 17pt;
    font-weight: 600;
    color: #1d1d1f;
    margin-top: 36px;
    margin-bottom: 18px;
    page-break-after: avoid;
}
h4 {
    font-size: 13pt;
    font-weight: 600;
    color: #424245;
    margin-top: 24px;
    margin-bottom: 12px;
    page-break-after: avoid;
}

p { margin-bottom: 16px; }
ul, ol { margin-left: 24px; margin-bottom: 20px; }
li { margin-bottom: 10px; }

.code-block {
    background: #f5f5f7;
    border: 1px solid #d2d2d7;
    border-radius: 8px;
    padding: 20px;
    margin: 24px 0;
    overflow-x: auto;
    font-family: 'SF Mono', 'Monaco', monospace;
    font-size: 10pt;
    line-height: 1.6;
    page-break-inside: avoid;
}
.code-block code { background: none; padding: 0; color: #1d1d1f; }
code {
    background: #f5f5f7;
    padding: 3px 6px;
    border-radius: 4px;
    font-family: 'SF Mono', monospace;
    font-size: 10pt;
    color: #d70050;
    font-weight: 500;
}

.content-table {
    width: 100%;
    border-collapse: collapse;
    margin: 28px 0;
    font-size: 10.5pt;
}
.content-table thead { background: #f5f5f7; }
.content-table th {
    padding: 14px 16px;
    text-align: left;
    font-weight: 600;
    border-bottom: 2px solid #d2d2d7;
}
.content-table td {
    padding: 12px 16px;
    border-bottom: 1px solid #d2d2d7;
    color: #424245;
    page-break-inside: avoid;
}

.quote-block {
    border-left: 3px solid #06c;
    padding-left: 20px;
    margin: 24px 0;
    color: #424245;
    page-break-inside: avoid;
}

strong { color: #1d1d1f; font-weight: 600; }
a { color: #06c; text-decoration: none; }
hr { border: none; border-top: 1px solid #d2d2d7; margin: 36px 0; }

p, li, .quote-block { orphans: 3; widows: 3; }
h2, h3, h4 { page-break-after: avoid; }
.code-block, .content-table, .quote-block { page-break-inside: avoid; }
"""


# ─────────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────────

def markdown_to_pdf_bytes(
    md_content: str,
    title: str | None = None,
    author: str | None = None,
    subtitle: str | None = None,
) -> bytes:
    """Render the given Markdown (huashu outline convention) to PDF bytes."""
    if not md_content or not md_content.strip():
        raise ValueError("md_content is empty")

    metadata = extract_metadata(md_content)
    if title:
        metadata["title"] = title
    if author:
        metadata["author"] = author
    if subtitle:
        metadata["subtitle"] = subtitle

    toc_structure = extract_toc_structure(md_content)
    toc_html = generate_toc_html(toc_structure)
    body_html = process_markdown(md_content)

    full_html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>{metadata.get("title") or "文档"}</title>
</head>
<body>
{create_cover_and_toc(metadata, toc_html)}
<div class="content">{body_html}</div>
</body>
</html>"""

    buf = io.BytesIO()
    HTML(string=full_html).write_pdf(buf, stylesheets=[CSS(string=_APPLE_CSS)])
    return buf.getvalue()
