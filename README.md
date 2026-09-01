# BestTeam

> Intent in, BestTeam out.

**GitHub:** https://github.com/GavinLai2023/bestteam

A commercial multi-agent framework that wraps [LangGraph](https://github.com/langchain-ai/langgraph) behind a clean, business-friendly API. Clients define agents and pipelines in a few lines of Python or a YAML file — the framework handles all orchestration complexity.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  Layer 1 · SDK          src/bestteam/               │
│  Agent / Team / Pipeline / ToolKit / Memory         │
│  ↕ EngineAdapter (swap LangGraph ↔ CrewAI)         │
├─────────────────────────────────────────────────────┤
│  Layer 2 · CLI          bestteam init/run/graph     │
│  Scaffold projects, run YAML pipelines, render DAG  │
├─────────────────────────────────────────────────────┤
│  Layer 3 · UI           ui/backend + ui/frontend    │
│  FastAPI + WebSocket backend · React live dashboard │
└─────────────────────────────────────────────────────┘
```

## Quick start

```bash
# 1. Install (-c requirements.lock pins the exact versions CI and the Docker image use)
pip install -c requirements.lock -e ".[tools]"                        # core SDK + built-in tools
pip install -c requirements.lock -e ".[tools,ui]"                     # + monitoring dashboard
pip install -c requirements.lock -e ".[tools,ui,providers-openai]"    # + real openai: models & interview transcription

# 2. Set environment variables (copy .env.example → .env)
export TAVILY_API_KEY=tvly-...
export OPENAI_API_KEY=sk-...

# 3. Scaffold a new project
bestteam init my_project
cd my_project

# 4. Run a pipeline
bestteam run pipeline.yaml "Review this Python function for bugs"

# 5. Visualise the agent graph
bestteam graph pipeline.yaml
```

### Updating the lockfile

`requirements.lock` is a [`uv pip compile`](https://docs.astral.sh/uv/pip/compile/)
constraints file over every extra CI and the Dockerfile install, resolved for
all platforms and every supported Python (`--universal`). `pyproject.toml`
keeps floating ranges because `bestteam` is also a library; the lock is what
makes a CI run or an image rebuild reproduce the versions the last one ran on.
Regenerate it whenever you change a dependency in `pyproject.toml`
(`tests/test_packaging.py` fails if a declared dependency is missing from the
lock or pinned outside its specifier), and deliberately, in its own commit,
when you want to pick up newer upstream versions:

```bash
# after editing pyproject.toml -- keeps existing pins, adds/removes what changed
uv pip compile pyproject.toml --universal --python-version 3.10 --extra ui --extra dev --extra tools --extra test --extra interview --extra providers-openai --extra providers-deepseek -o requirements.lock
# to move every pin to the newest allowed version
uv pip compile ... --upgrade -o requirements.lock
```

## SDK usage

```python
from bestteam import Agent, Team, CollaborationMode, Pipeline
from bestteam import web_search, calculator

researcher = Agent(
    name="researcher",
    role="Research Analyst",
    goal="Find the latest AI news and crunch key statistics",
    model="openai:gpt-4o-mini",
    tools=[web_search, calculator],
)

writer = Agent(
    name="writer",
    role="Content Writer",
    goal="Turn findings into a polished brief",
    model="openai:gpt-4o-mini",
)

team = Team(name="brief_team", agents=[researcher, writer],
            mode=CollaborationMode.SEQUENTIAL)

result = Pipeline(name="research_brief", steps=[team]).run(
    "What are the biggest AI breakthroughs this week?"
)
print(result.output)
```

## YAML pipeline

```yaml
name: code_review
agents:
  - name: reviewer
    role: Senior Code Reviewer
    goal: Find correctness bugs and code smells
    model: "openai:gpt-4o-mini"
    tools: [web_search]
  - name: fixer
    role: Senior Engineer
    goal: Produce corrected, production-ready code
    model: "openai:gpt-4o-mini"
teams:
  - name: qa_team
    agents: [reviewer, fixer]
    mode: sequential
pipeline:
  steps: [qa_team]
```

```bash
bestteam run code_review.yaml "def divide(a, b): return a / b"
```

## Built-in tools

| Tool | Description | Requires |
|---|---|---|
| `web_search(query)` | Tavily web search, LLM-optimised output | `TAVILY_API_KEY` |
| `local_business_search(query)` | Google Places lookup — name, address, rating, review count, price level | `GOOGLE_MAPS_API_KEY` |
| `parse_file(path)` | Extract text from PDF, Excel, Word (incl. tables), XML, or plain text | `pip install 'bestteam[tools-files]'` |
| `http_get(url)` | HTTP GET with optional JSON headers; HTML comes back as readable text, long bodies are truncated; blocks requests to private/internal addresses | `pip install 'bestteam[tools-http]'` |
| `calculator(expr)` | Safe arithmetic via AST — prevents hallucination | — |
| `email_find` · `email_read` · `email_read_attachment` · `email_draft_reply` | Read a mailbox and write a **draft** reply — see below | IMAP or Microsoft Graph credentials |

`parse_file` reads any local path it's given, with no sandboxing — if you
expose it to an agent, constrain which paths the agent can be prompted to
access.

## Knowledge bases

Connect agents to a client's documents — point at a folder and the framework
parses, chunks, and indexes it in memory. Three index types, all in-process
with no external vector store to run:

| `type` | Retrieval |
|---|---|
| `local_folder` (default) | BM25 keyword search — no API key required |
| `vector` | Embedding cosine similarity |
| `hybrid` | BM25 + vector, RRF-fused |

```yaml
knowledge_bases:
  - name: product_docs
    path: ./docs/product

agents:
  - name: support_agent
    role: Support Specialist
    goal: Answer customer questions using the product documentation
    model: "openai:gpt-4o-mini"
    tools: [product_docs]
```

All three types support opt-in query expansion (`query_expansion_model`) and
opt-in reranking (`rerank_model`).

Requires `pip install 'bestteam[tools-rag]'`. See
`ui/backend/pipelines/knowledge_base_demo.yaml` for a runnable example.

## Email automation

An agent team can work a real mailbox: find messages, read them, read their
attachments, and leave a reply **in the Drafts folder** for a human to send.
Either an IMAP server or Microsoft 365 / Exchange Online via Graph
(`Mail.ReadWrite` application permission) can back it.

**There is no send verb, and no SMTP anywhere in the process.** That is the
containment argument for feeding an LLM mail that arbitrary strangers wrote:
the worst outcome of a successful prompt injection is a strange draft that a
person reads before anything leaves the building. Deploy validation refuses to
pair an email tool with an egress tool such as `http_get` for the same reason.

Beyond the tools, the UI adds an opt-in autonomous mode per organisation — a
poller watches the mailbox and runs that organisation's deployed team on new
mail:

- **Header rules decide before any model is involved** — a sender
  blocklist/allowlist, a subject blocklist, and a bulk-mail check
  (`Auto-Submitted`, `Precedence`, `List-Id`, `List-Unsubscribe`), so a
  newsletter never reaches a billable run. Two pattern forms only
  (`someone@example.com`, `*@example.com`); no regular expressions. A
  filtered message is still recorded, stays visible, and is released back into
  the queue with one click.
- **Two per-organisation budgets** — a daily message cap and a monthly spend
  cap. Breaching either pauses dispatch, raises one alert for the period, and
  resumes automatically when the period rolls over. Both default to off. The
  deployment-wide rail `BESTTEAM_TRIGGER_DAILY_CAP` still applies on top.
- **Attachments read on demand** — `email_read` lists what a message carries
  (filename, type, size) and `email_read_attachment(message_id, filename)`
  extracts one, so the model pays only for what it decides to open. The tool
  takes no path and nothing is written to disk: extraction runs on the bytes
  already in memory, matched against that message's own MIME parts. Limits are
  10 MB per attachment, 25 MB per message and 8,000 characters of text; the
  readable types are exactly `parse_file`'s. No OCR, and archives are refused.

See `ui/backend/CLAUDE.md` and `docs/deployment.md` for credentials, per-org
mailboxes, and the trigger's environment variables.

## Web UI

```bash
# Terminal 1 — backend.
# BESTTEAM_SECRET_KEY is mandatory: the service refuses to start without a
# real one. Generate it with:
#   python -c "import secrets; print(secrets.token_hex(32))"
#
# BESTTEAM_DEMO_PIPELINES=1 exposes the bundled example pipelines in
# ui/backend/pipelines/ so the dropdown has something to pick on a fresh
# database. It is off by default (those are demo fixtures, not tenant data) —
# leave it unset on a real deployment. See docs/deployment.md.
BESTTEAM_SECRET_KEY=... BESTTEAM_DEMO_PIPELINES=1 python -m uvicorn ui.backend.main:app --port 8000

# Terminal 2 — frontend
cd ui/frontend && npm run dev
```

Open **http://localhost:5173**. The UI is two things: a live dashboard, where
you pick a pipeline, enter an input, and watch agents hand off over WebSocket;
and a guided **Team Builder** wizard that walks a non-technical customer from a
description of their problem to a deployed team.

## Project structure

```
src/bestteam/
├── core/          Agent, Team, Pipeline, Memory, ToolKit, TraceEvent
├── adapters/      EngineAdapter ABC + LangGraphAdapter
├── tools/         Built-in tools (web_search, parse_file, http_get,
│                  calculator, local_business_search, email toolkit)
└── cli/           Typer CLI (init / run / graph)

ui/
├── backend/       FastAPI + WebSocket monitoring backend
│   └── pipelines/ Example YAML pipelines
└── frontend/      React + Vite live dashboard

examples/          Runnable demo scripts
tests/             pytest suite
```

## Environment variables

See `.env.example` for the full list. Copy it to `.env` — it is git-ignored.

## Roadmap

- [x] Local-folder knowledge base (BM25 keyword search)
- [x] Semantic/vector knowledge base (in-memory cosine similarity, optional embedding cache)
- [x] Hybrid knowledge base (BM25 + vector, RRF-fused), with opt-in query expansion and reranking
- [x] Email automation — mailbox triage, draft-only replies, attachment reading, pre-LLM filtering and per-org budgets
- [x] Persisted run history (runs, trace events, usage) with per-org retention
- [ ] External vector stores (Chroma / FAISS / Pinecone)
- [ ] DMS connectors (SharePoint / Confluence / Google Drive)
- [ ] SQL executor (read-only, SQLAlchemy)
- [ ] Python code sandbox (subprocess-isolated)
- [ ] CrewAI adapter
- [ ] Rehydrating live run state after a restart (Redis / Postgres)
