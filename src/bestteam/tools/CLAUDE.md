# bestteam — `src/bestteam/tools/` (built-in tools)

Directory-scoped notes for the built-in tools clients can attach to agents.
See the root `CLAUDE.md` for project overview, architecture, and commands.

## Built-in tools

Seven ready-made tools clients can attach directly to any Agent:

| Tool | Import | Env var required | Extra dep |
|---|---|---|---|
| `web_search(query, max_results=5)` | `from bestteam import web_search` | `TAVILY_API_KEY` | `pip install 'bestteam[tools-search]'` |
| `parse_file(path)` | `from bestteam import parse_file` | — | `pip install 'bestteam[tools-files]'` (PDF/Excel/Word) |
| `http_get(url, headers_json="{}")` | `from bestteam import http_get` | — | `pip install 'bestteam[tools-http]'` (httpx) |
| `calculator(expression)` | `from bestteam import calculator` | — | none (stdlib only) |
| `email_find(query="")` | `from bestteam import email_find` | `BESTTEAM_EMAIL_BACKEND` + backend creds | `graph`: `pip install 'bestteam[tools-email]'` (httpx); `imap`: none |
| `email_read(message_id)` | `from bestteam import email_read` | (same) | (same) |
| `email_draft_reply(message_id, body)` | `from bestteam import email_draft_reply` | (same) | (same) |

### Email tools (`email_client.py`) — draft-only by design

One configured mailbox per deployment (`BESTTEAM_EMAIL_BACKEND=graph|imap`
plus `BESTTEAM_GRAPH_*` / `BESTTEAM_IMAP_*` — see `.env.example`). Two
backends behind one seam: Microsoft Graph (M365/Exchange Online, app-only
client-credentials OAuth, `createReply` for correctly threaded drafts) and
generic IMAP (stdlib `imaplib`, `BODY.PEEK`/readonly so reading never marks
messages seen, MIME reply built with `In-Reply-To`/`References`, APPENDed to
the Drafts folder resolved via `BESTTEAM_IMAP_DRAFTS` → SPECIAL-USE →
`"Drafts"`). **There is no send verb and no SMTP anywhere** — the worst
outcome is a bad draft a human reviews in their own mail client. Design:
`docs/superpowers/specs/2026-07-15-email-toolkit-design.md`.

The built-in `email_triage_reply` Skill (seeded into the Skills library on
backend bootstrap, `ui/backend/skills.py::seed_default_skills`) packages the
triage playbook + these three tools for Team Builder customers.

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
private/internal address (see `_check_host_allowed` in `http_client.py`). The
SSRF check resolves the host, rejects private/internal addresses, and returns
the validated IP; `http_get` then **pins the connection to that IP** (keeping
the hostname for the `Host` header and TLS SNI/cert), so `httpx` never
re-resolves and a DNS-rebinding TOCTOU can't slip a private address past the
check (CR-023). Pinning uses the first resolved address, trading happy-eyeballs
failover for that guarantee. Both tools are still intentionally broad — their
purpose is to read files / fetch URLs the agent is told to — so callers exposing
them to an LLM agent remain responsible for constraining which paths/URLs the
agent can be prompted to access, and for network-layer egress controls where
internal services are reachable.

**Email trust boundaries**: email bodies are attacker-controlled input to
the LLM (prompt injection). Mitigations: no send capability exists (bounded
blast radius — drafts are human-reviewed), the seeded skill instructs the
agent to treat message content as data rather than instructions, and
`email_read` caps body size. For the Graph backend, use least privilege:
`Mail.ReadWrite` **application** permission restricted to the single mailbox
with an Exchange Application Access Policy. In this SDK layer, credentials
live in env vars (one mailbox per process): `_get_backend()` builds the
backend via `_ImapBackend.from_env()` / `_GraphBackend()`.

**Per-mailbox seam (used by the UI backend for multi-tenancy).**
`_ImapBackend(host=, user=, password=, port=, drafts=, restrict_to_public=)`
builds a backend from explicit params (not env), and `make_email_tools(backend)`
returns the three `email_*` tools bound to it — same names/docstrings the model
sees for the env tools. `_connect()` always uses a verifying TLS context
(`ssl.create_default_context()` — imaplib's fallback does *not* verify the cert)
and a bounded socket timeout. `restrict_to_public=True` (set by the per-org
store path, where the host is customer-supplied) additionally re-runs the SSRF
check on every connect and pins the socket to the checked IP while keeping the
hostname for SNI/cert — closing the DNS-rebinding window (`_PinnedIMAP4_SSL`).
It stays `False` for the operator-trusted env path, which may legitimately point
at an internal IMAP server. TLS is still verified there — an internal server
with a private/self-signed CA must have that CA trusted (point `SSL_CERT_FILE`
at the CA bundle, which `ssl.create_default_context()` honors); there is no
verification-off switch, by design. The UI backend uses this to give each org its own encrypted mailbox
(see `ui/backend/email_tools.py`), overriding the env-based `REGISTRY` tools
by name. With `BESTTEAM_EMAIL_BACKEND` set and more than one org, the UI
backend still refuses to start (`ensure_email_single_org`, CR-031) — the env
path is process-wide/single-mailbox by design; multi-tenant email uses the
per-org store instead.

Tier 2 tools (SQL executor, Python sandbox), real email *sending*, and
ambient run-on-new-mail triggering are planned but not yet implemented.

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
  platform's org scoping (see root `docs/DECISIONS.md`) governs its own DB
  rows, not what a custom tool's internal connection can reach, so never
  let one org's tool function read another customer's data. On a shared
  multi-org deployment, don't wire a custom tool to one customer's backend
  at all until per-org credentials (secrets store sub-project) exist.
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
