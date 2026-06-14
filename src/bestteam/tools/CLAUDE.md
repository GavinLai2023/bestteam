# bestteam — `src/bestteam/tools/` (built-in tools)

Directory-scoped notes for the built-in tools clients can attach to agents.
See the root `CLAUDE.md` for project overview, architecture, and commands.

## Built-in tools

Four ready-made tools clients can attach directly to any Agent:

| Tool | Import | Env var required | Extra dep |
|---|---|---|---|
| `web_search(query, max_results=5)` | `from bestteam import web_search` | `TAVILY_API_KEY` | `pip install 'bestteam[tools-search]'` |
| `parse_file(path)` | `from bestteam import parse_file` | — | `pip install 'bestteam[tools-files]'` (PDF/Excel/Word) |
| `http_get(url, headers_json="{}")` | `from bestteam import http_get` | — | `httpx` (indirect dep via FastAPI) |
| `calculator(expression)` | `from bestteam import calculator` | — | none (stdlib only) |

**Code usage:**
```python
from bestteam import Agent, calculator, web_search
agent = Agent(..., tools=[web_search, calculator])
```

**YAML usage** — tools are referenced by name; loader resolves them via `tools.REGISTRY`:
```yaml
agents:
  - name: researcher
    role: Research Analyst
    goal: Find the latest news on a topic
    model: "openai:gpt-4o-mini"
    tools: [web_search, calculator]
```

**Trust boundaries**: `parse_file` reads any local path it's given (no
sandboxing) and `http_get` fetches any URL whose host doesn't resolve to a
private/internal address (see `_check_host_allowed` in `http_client.py`).
Both are intentional — the tools' purpose is to read files / fetch URLs the
agent is told to — but callers exposing these tools to an LLM agent are
responsible for constraining which paths/URLs the agent can be prompted to
access.

Tier 2 tools (SQL executor, Python sandbox) and email integration are planned but not yet implemented.
