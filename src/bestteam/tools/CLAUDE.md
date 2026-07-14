# bestteam — `src/bestteam/tools/` (built-in tools)

Directory-scoped notes for the built-in tools clients can attach to agents.
See the root `CLAUDE.md` for project overview, architecture, and commands.

## Built-in tools

Four ready-made tools clients can attach directly to any Agent:

| Tool | Import | Env var required | Extra dep |
|---|---|---|---|
| `web_search(query, max_results=5)` | `from bestteam import web_search` | `TAVILY_API_KEY` | `pip install 'bestteam[tools-search]'` |
| `parse_file(path)` | `from bestteam import parse_file` | — | `pip install 'bestteam[tools-files]'` (PDF/Excel/Word) |
| `http_get(url, headers_json="{}")` | `from bestteam import http_get` | — | `pip install 'bestteam[tools-http]'` (httpx) |
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

## Connecting to external systems (ERP / order databases)

A common production question: a customer's order/inventory data lives in a
real system (ERP, order management database, internal API) — not a folder of
text files. **Don't model this as a `knowledge_base`.** Knowledge bases do
semantic/keyword retrieval over unstructured documents (policies, manuals);
structured business records (an order, looked up by ID) want an exact,
parameterized lookup instead. That's a `tool`, not a knowledge base.

**SDK-level pattern (works today, no platform changes needed):** write a
plain Python function with the same shape as a built-in tool — one string
argument in, a string out, and a `__doc__` describing what it does and its
argument(s). That docstring is sent to the model as the tool's description
exactly like `make_knowledge_base_tool()` does for knowledge bases, so write
it for the LLM, not just for a human reader:

```python
def lookup_order(order_id: str) -> str:
    """Look up an order's status, items, and shipping date by order ID.

    Args:
        order_id: The order number to look up.
    """
    row = my_erp_client.get_order(order_id)
    if row is None:
        return f"No order found with ID '{order_id}'"
    return f"Order {row.id}: {row.item}, ${row.price}, received {row.received_date}"
```

Register it exactly like any other tool: pass it via `load_workflow(path,
toolkits=[...])` (see `core/loader.py`), or directly as `extra_tools={"lookup_order":
lookup_order}` to `validate_specification()`/`generate_specification()`, then
reference `lookup_order` by name in an agent's `tools:` list.

**Wiring it into a UI-backend-deployed workflow needs one small addition.**
The pattern above is the whole story for the CLI/SDK (`load_workflow()`) path.
`ui/backend/` only special-cases one kind of standalone, by-name-referenced
tool today: knowledge bases, via `ui/backend/knowledge_bases.py::load_knowledge_base_tools()`,
called from `main.py::_get_workflow()`, `builder.py`, and `crud.py` to build
the `extra_tools` dict passed into `_build_workflow()`. There's no generic
"custom tool" registry in the UI layer yet — to make `lookup_order` resolvable
in a workflow deployed through the UI, add a parallel loader (same shape as
`load_knowledge_base_tools`) and merge its output into `extra_tools` at those
same three call sites. This is a small, well-understood extension, not a
platform redesign — but it is a real code change, not just configuration.

**Security boundaries** (same spirit as the Trust Boundaries note above):
- Use a read-only DB role or read-only API token for the connection the tool
  function uses internally.
- Tenant/customer isolation is the tool function's responsibility — the
  platform has no multi-tenancy concept (see root `docs/DECISIONS.md`), so
  never let one deployment's tool function read another customer's data.
- A normal "not found" result should be a returned string, not a raised
  exception: `_run_agent`'s tool-calling loop (`adapters/langgraph_adapter.py`)
  catches exceptions and turns them into generic error text fed back to the
  model, which is the right behavior for genuine failures but a worse user
  experience than a clean "no order found" message for an expected empty
  result.

A generic, reusable authenticated REST API connector tool (configure a base
URL + auth once, call it with a path/params) is a reasonable next step for
covering many integrations without bespoke code per customer, but that's a
bigger platform feature (self-service config, credential storage) and isn't
designed here.
