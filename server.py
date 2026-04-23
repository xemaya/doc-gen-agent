"""Document-generation agent — accepts a task description, returns a
professional Apple-style whitepaper PDF in one HTTP round-trip.

Protocol (deliberately *not* /chat):

  POST /invoke    application/json
                  {"task": "...", "title"?, "author"?, "subtitle"?}
                  → 200 application/pdf with binary PDF bytes
                  → 4xx/5xx application/json {"code": "...", "message": "..."}

  GET  /health    worker-pool liveness probe

This agent is consumed through the platform byte-passthrough proxy at
POST /api/v1/shops/{shopId}/invoke (ShopInvokeController, 2026-04-23).
The proxy mirrors our status + Content-Type back to the caller, so the
PAT caller gets the PDF bytes directly — no SSE framing, no base64.

The OpenRouter API key is pulled from SSM on process start using the
ECS task role; nothing sensitive lives in the image or agent.yaml.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import quote

import boto3
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from openai import AsyncOpenAI

from doc_pipeline import markdown_to_pdf_bytes


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("doc-gen-agent")


# ── Secrets: fetch once at startup via IAM task role ──────────────────
REGION = os.environ.get("AWS_REGION", "us-east-1")
OPENROUTER_KEY_PARAM = os.environ.get(
    "OPENROUTER_KEY_PARAM", "/a2h/agents/doc-gen/openrouter-api-key"
)


def _fetch_openrouter_key() -> str:
    ssm = boto3.client("ssm", region_name=REGION)
    resp = ssm.get_parameter(Name=OPENROUTER_KEY_PARAM, WithDecryption=True)
    return resp["Parameter"]["Value"]


try:
    OPENROUTER_API_KEY = _fetch_openrouter_key()
    log.info("fetched OpenRouter key from SSM %s", OPENROUTER_KEY_PARAM)
except Exception as ex:
    log.error("failed to fetch %s from SSM: %s", OPENROUTER_KEY_PARAM, ex)
    raise


LLM = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)
MODEL_ID = os.environ.get("A2H_MODEL_ID", "openai/gpt-5.4")
MAX_TOKENS = int(os.environ.get("A2H_MAX_TOKENS", "8000"))


# SYSTEM_PROMPT forces the exact outline shape that huashu's TOC extractor
# relies on ("## 1. Title" / "### 1.1 Subtitle"). If the LLM drifts, the PDF
# still renders but the table-of-contents page comes out empty.
SYSTEM_PROMPT = """你是一名资深白皮书撰稿人。根据用户给定的题目，写一份结构严谨、内容详实的中文白皮书。

**必须严格遵循的输出格式**（否则无法生成 PDF 目录）：

1. 文档第一行是 h1 主标题：`# 标题`
2. 主章节必须是：`## 1. 章节名`、`## 2. 章节名` …（数字 + 点 + 空格 + 标题，编号不可省略，不要用 emoji）
3. 子章节必须是：`### 1.1 小节名`、`### 1.2 小节名` …（两级数字编号）
4. 至少 5 个主章节，每个主章节至少 2 个子章节
5. 内容使用标准 Markdown：段落、无序/有序列表、表格、引用（>）、代码块（```）
6. 不要使用 emoji，不要在标题里加符号装饰
7. 每个子章节正文至少 2-3 段，体现专业深度

输出只返回 Markdown 正文，不要外包裹 ```markdown 代码块，不要任何解释性前后话。"""


app = FastAPI(title="doc-gen-agent")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/invoke")
async def invoke(request: Request) -> Response:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")

    task = (body.get("task") or "").strip()
    if not task:
        raise HTTPException(status_code=400, detail="'task' is required")

    title = body.get("title") or None
    author = body.get("author") or None
    subtitle = body.get("subtitle") or None

    log.info("doc-gen start: task_len=%d title=%s", len(task), title)

    try:
        md = await _generate_markdown(task)
    except Exception as ex:
        log.exception("LLM call failed")
        return JSONResponse(
            status_code=502,
            content={"code": "LLM_ERROR", "message": str(ex)},
        )
    log.info("markdown generated: chars=%d", len(md))

    try:
        pdf_bytes = markdown_to_pdf_bytes(
            md_content=md,
            title=title,
            author=author,
            subtitle=subtitle,
        )
    except Exception as ex:
        log.exception("PDF render failed")
        return JSONResponse(
            status_code=500,
            content={"code": "PDF_RENDER_ERROR", "message": str(ex)},
        )
    log.info("pdf generated: bytes=%d", len(pdf_bytes))

    # HTTP headers are latin-1 by default, so CJK titles can't go in
    # filename="...". RFC 5987 filename*=UTF-8'' handles it, with an ASCII
    # filename= fallback for ancient clients.
    safe_name = (title or "document").replace('"', "'").replace("\\", "")
    ascii_fallback = "document.pdf"
    utf8_encoded = quote(f"{safe_name}.pdf", safe="")
    content_disposition = (
        f'attachment; filename="{ascii_fallback}"; '
        f"filename*=UTF-8''{utf8_encoded}"
    )
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": content_disposition,
            "X-Doc-Length": str(len(pdf_bytes)),
        },
    )


async def _generate_markdown(task: str) -> str:
    resp = await LLM.chat.completions.create(
        model=MODEL_ID,
        max_tokens=MAX_TOKENS,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": task},
        ],
    )
    content = resp.choices[0].message.content or ""
    if not content.strip():
        raise RuntimeError("LLM returned empty content")
    return content
