# bestteam

> 把复杂留给自己，把简单留给客户。

A commercial multi-agent framework that wraps [LangGraph](https://github.com/langchain-ai/langgraph) behind a clean, business-friendly API. Clients define agents and workflows in a few lines of Python or a YAML file — the framework handles all orchestration complexity.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  Layer 1 · SDK          src/bestteam/               │
│  Agent / Team / Workflow / ToolKit / Memory         │
│  ↕ EngineAdapter (swap LangGraph ↔ CrewAI)         │
├─────────────────────────────────────────────────────┤
│  Layer 2 · CLI          bestteam init/run/graph     │
│  Scaffold projects, run YAML workflows, render DAG  │
├─────────────────────────────────────────────────────┤
│  Layer 3 · UI           ui/backend + ui/frontend    │
│  FastAPI + WebSocket backend · React live dashboard │
└─────────────────────────────────────────────────────┘
```

## Quick start

```bash
# 1. Install
pip install -e ".[tools]"          # core SDK + built-in tools
pip install -e ".[tools,ui]"       # + monitoring dashboard

# 2. Set environment variables (copy .env.example → .env)
export TAVILY_API_KEY=tvly-...
export OPENAI_API_KEY=sk-...

# 3. Scaffold a new project
bestteam init my_project
cd my_project

# 4. Run a workflow
bestteam run workflow.yaml "Review this Python function for bugs"

# 5. Visualise the agent graph
bestteam graph workflow.yaml
```

## SDK usage

```python
from bestteam import Agent, Team, CollaborationMode, Workflow
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

result = Workflow(name="research_brief", steps=[team]).run(
    "What are the biggest AI breakthroughs this week?"
)
print(result.output)
```

## YAML workflow

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
workflow:
  steps: [qa_team]
```

```bash
bestteam run code_review.yaml "def divide(a, b): return a / b"
```

## Built-in tools

| Tool | Description | Requires |
|---|---|---|
| `web_search(query)` | Tavily web search, LLM-optimised output | `TAVILY_API_KEY` |
| `parse_file(path)` | Extract text from PDF, Excel, or plain text | `pip install 'bestteam[tools-files]'` |
| `http_get(url)` | HTTP GET with optional JSON headers | — |
| `calculator(expr)` | Safe arithmetic via AST — prevents hallucination | — |

## Monitoring dashboard

```bash
# Terminal 1 — backend
python -m uvicorn ui.backend.main:app --port 8000

# Terminal 2 — frontend
cd ui/frontend && npm run dev
```

Open **http://localhost:5173**, pick a workflow, enter an input, and watch agents hand off live over WebSocket.

## Project structure

```
src/bestteam/
├── core/          Agent, Team, Workflow, Memory, ToolKit, TraceEvent
├── adapters/      EngineAdapter ABC + LangGraphAdapter
├── tools/         Built-in tools (web_search, parse_file, http_get, calculator)
└── cli/           Typer CLI (init / run / graph)

ui/
├── backend/       FastAPI + WebSocket monitoring backend
│   └── workflows/ Example YAML workflows
└── frontend/      React + Vite live dashboard

examples/          Runnable demo scripts
tests/             pytest suite (42 tests)
```

## Environment variables

See `.env.example` for the full list. Copy it to `.env` — it is git-ignored.

## Roadmap

- [ ] Vector memory (Chroma / FAISS / Pinecone)
- [ ] SQL executor (read-only, SQLAlchemy)
- [ ] Python code sandbox (subprocess-isolated)
- [ ] CrewAI adapter
- [ ] Persistent run state (Redis / Postgres)
