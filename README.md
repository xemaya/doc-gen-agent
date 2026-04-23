# doc-gen-http — Invoke-only Document Generator Agent

A single-shot HTTP agent: POST a task description, get back a PDF. No
chat loop, no SSE — designed for the platform byte-passthrough proxy at
`POST /api/v1/shops/{shopId}/invoke` (added 2026-04-23).

## What's inside

| File | What it does |
|---|---|
| `server.py` | FastAPI: `POST /invoke` (→ PDF bytes) + `GET /health`. Fetches OpenRouter key from SSM at startup. |
| `doc_pipeline.py` | Markdown → Apple-style PDF (ported from huashu-md-to-pdf, now memory-to-memory). |
| `agent.yaml` | Agent Protocol v2 manifest. No platform tools, egress = openrouter.ai + SSM. |
| `Dockerfile` | Base image + pango/cairo/harfbuzz + Noto CJK fonts (weasyprint native deps). |
| `requirements.txt` | openai, markdown2, weasyprint. (boto3 / fastapi / uvicorn come from the base image.) |

## LLM

- Provider: OpenRouter (`base_url=https://openrouter.ai/api/v1`)
- Model: `openai/gpt-5.4` (override with `A2H_MODEL_ID` env)
- API key: SSM parameter `/a2h/agents/doc-gen/openrouter-api-key`
  fetched via the shared `shopdiy-worker-task-role`. No secrets in
  the image or in `agent.yaml`.

## Protocol

Request:

```http
POST /invoke HTTP/1.1
Content-Type: application/json

{
  "task": "写一份关于 AI Agent 架构演进的白皮书，面向资深工程师",
  "title": "AI Agent 白皮书",
  "author": "花叔",
  "subtitle": "2026 版"
}
```

Response (success):

```http
HTTP/1.1 200 OK
Content-Type: application/pdf
Content-Disposition: attachment; filename="AI Agent 白皮书.pdf"
X-Doc-Length: 284371

<binary PDF>
```

Response (error):

```http
HTTP/1.1 502 Bad Gateway
Content-Type: application/json

{"code": "LLM_ERROR", "message": "..."}
```

## Outline contract

The system prompt forces strict numbered headings so the TOC extractor
works:

- Main chapters: `## 1. 章节名` / `## 2. 章节名` …
- Sub chapters:  `### 1.1 小节名` / `### 1.2 小节名` …

If the LLM drifts and drops the numbers, the PDF still renders but the
TOC page comes out empty.

## Deploying this agent

```bash
# 1. Put the OpenRouter key into SSM (once per environment).
aws ssm put-parameter \
  --name /a2h/agents/doc-gen/openrouter-api-key \
  --value "sk-or-..." \
  --type SecureString \
  --overwrite \
  --region us-east-1

# 2. Push this template to a public repo your PAT can read.
cp -r kit-v2/templates/doc-gen-http ~/work/doc-gen-agent && cd ~/work/doc-gen-agent
git init && git add . && git commit -m "init"
git remote add origin https://github.com/you/doc-gen-agent.git
git push -u origin main

# 3. Create a new shop on shopdiy (UI or API) — note its shopId.

# 4. Submit the agent:
a2h-shopdiy agent:submit \
  --shop <shopId> \
  --source https://github.com/you/doc-gen-agent.git \
  --version 1.0.0
```

## Calling it via the shop proxy

Once the pool has at least one RUNNING worker, call the shopdiy proxy
with your PAT:

```bash
curl -X POST "https://shopdiy.a2hmarket.ai/findu-diy-shop/api/v1/shops/<shopId>/invoke" \
  -H "Authorization: Bearer shopdiy_pat_..." \
  -H "Content-Type: application/json" \
  -d '{"task": "写一份大模型 Agent 2026 综述"}' \
  --max-time 360 \
  --output output.pdf
```

The proxy mirrors `application/pdf` back to you — `output.pdf` is the
real thing.

## Local dev

```bash
cd kit-v2/templates/doc-gen-http

# On macOS weasyprint needs pango:
brew install pango

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Point to SSM or override the key:
export AWS_REGION=us-east-1
# or skip SSM locally by monkey-patching _fetch_openrouter_key before import:
#   export OPENROUTER_API_KEY=... and edit server.py to read from env

.venv/bin/uvicorn server:app --host 0.0.0.0 --port 8080
```

Then:

```bash
curl -X POST http://localhost:8080/invoke \
  -H "Content-Type: application/json" \
  -d '{"task": "写一份 hello world 测试文档", "title": "测试"}' \
  --output test.pdf
```
