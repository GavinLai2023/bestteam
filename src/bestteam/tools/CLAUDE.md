# bestteam — `src/bestteam/tools/` (built-in tools)

Directory-scoped notes for the built-in tools clients can attach to agents.
See the root `CLAUDE.md` for project overview, architecture, and commands.

## Built-in tools

Nine ready-made tools clients can attach directly to any Agent:

| Tool | Import | Env var required | Extra dep |
|---|---|---|---|
| `web_search(query, max_results=5)` | `from bestteam import web_search` | `TAVILY_API_KEY` | `pip install 'bestteam[tools-search]'` |
| `local_business_search(query, max_results=5)` | `from bestteam import local_business_search` | `GOOGLE_MAPS_API_KEY` | `pip install 'bestteam[tools-places]'` (httpx) |
| `parse_file(path)` | `from bestteam import parse_file` | — | `pip install 'bestteam[tools-files]'` (PDF/Excel/Word); `.xml` needs no extra (stdlib) |
| `http_get(url, headers_json="{}")` | `from bestteam import http_get` | — | `pip install 'bestteam[tools-http]'` (httpx) |
| `calculator(expression)` | `from bestteam import calculator` | — | none (stdlib only) |
| `email_find(query="")` | `from bestteam import email_find` | `BESTTEAM_EMAIL_BACKEND` + backend creds | `graph`: `pip install 'bestteam[tools-email]'` (httpx); `imap`: none |
| `email_read(message_id)` | `from bestteam import email_read` | (same) | (same) |
| `email_read_attachment(message_id, filename)` | `from bestteam import email_read_attachment` | (same) | (same) + `bestteam[tools-files]` for PDF/Excel/Word attachments |
| `email_draft_reply(message_id, body)` | `from bestteam import email_draft_reply` | (same) | (same) |

### Local business search (`google_places.py`)

Wraps the Google Places API (New) Text Search endpoint
(`places:searchText`). The query is a single free-text string that should
include both the business type and an area (e.g. `"electrician in
Parramatta NSW"`) — there's no separate lat/lng/radius parameter, matching
`web_search`'s single-query-string shape. Returns name, address, Google
rating, review count, and price level (`$`–`$$$$`) per result, formatted as
plain text for the model to compare. Requires `GOOGLE_MAPS_API_KEY` with the
Places API (New) enabled on the project; billed per Google's Places API
pricing. Only server errors (5xx) are retried, matching `http_get`.

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
triage playbook + these four tools for Team Builder customers.

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
internal services are reachable. `.xml` parsing uses stdlib
`xml.etree.ElementTree`, which doesn't resolve external entities (not
XXE-vulnerable) but has no protection against entity-expansion ("billion
laughs") DoS — the same unmitigated-DoS posture the PDF/Excel/Word parsers
already have against decompression bombs, not a new risk class for this
tool's trust boundary.

**Email trust boundaries**: email bodies — and, since Phase 4b, the text
extracted from attachments — are attacker-controlled input to the LLM (prompt
injection). Mitigations: no send capability exists (bounded blast radius —
drafts are human-reviewed), the seeded skill instructs the agent to treat
message content as data rather than instructions, and `email_read` caps body
size (`email_read_attachment` caps extracted text at the same 8,000
characters — see "Attachment reading" below). For the Graph backend, use least privilege:
`Mail.ReadWrite` **application** permission restricted to the single mailbox
with an Exchange Application Access Policy. In this SDK layer, credentials
live in env vars (one mailbox per process): `_get_backend()` builds the
backend via `_ImapBackend.from_env()` / `_GraphBackend()`.

**Per-mailbox seam (used by the UI backend for multi-tenancy).**
`_ImapBackend(host=, user=, password=, port=, drafts=, restrict_to_public=,
token_provider=)` builds a backend from explicit params (not env), and `make_email_tools(backend)`
returns the four `email_*` tools bound to it — same names/docstrings the model
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
verification-off switch, by design.

**Auth strategy: exactly one of `password=` or `token_provider=`**, checked in
the constructor rather than at connect time. With a provider, `_connect()`
authenticates with SASL `XOAUTH2` instead of `login()` — the multi-tenant path
for Microsoft 365 / Exchange Online, which no longer accepts basic auth.
Everything *above* `_connect()` is unchanged and shared by both: after
`AUTHENTICATE` succeeds the session is an ordinary authenticated IMAP session,
so `find`/`read`/`draft_reply`/`summaries_for`/`drafts_with_source_keys`/
`check_drafts_writable` — and the whole of `ui/backend/email_trigger.py`,
including the UID cursor and Phase 1's event ledger — need no knowledge of
which auth was used. The token is fetched *before* the socket is opened, so a
credential failure leaves no dangling connection and stays distinguishable from
a mailbox-access failure.

The provider is `bestteam.tools._oauth.MicrosoftClientCredentialsToken`
(app-only client credentials, scope `https://outlook.office365.com/.default`).
It is **stdlib `urllib` only, deliberately no httpx**: the per-org IMAP path has
no third-party HTTP dependency and `backend-optional-deps` runs without optional
extras (the httpx note on `tools-email` in `pyproject.toml` still means "Graph
backend only"). It caches the token until 60 seconds before expiry — the poller
is a long-lived process and the token lasts about an hour, so caching forever
would break it. (`_GraphBackend._token()` *does* cache forever; that is a
pre-existing bug in the unrelated env-only Graph path, recorded in
`docs/STATUS.md`.) Design:
`docs/superpowers/specs/2026-08-17-email-phase-2-microsoft-oauth-design.md`.

The UI backend uses this to give each org its own encrypted mailbox
(see `ui/backend/email_tools.py`), overriding the env-based `REGISTRY` tools
by name. With `BESTTEAM_EMAIL_BACKEND` set and more than one org, the UI
backend still refuses to start (`ensure_email_single_org`, CR-031) — the env
path is process-wide/single-mailbox by design; multi-tenant email uses the
per-org store instead.

`make_email_tools(backend, allowed_uids=, draft_marker_prefix=)` also
stamps `X-BestTeam-Source-Key: <prefix><message_id>` on each draft when a
prefix is given, so a retry can reconcile against the mailbox
(`_ImapBackend.drafts_with_source_keys`) and recognise a draft that was really
APPENDed but never recorded. `_draft_impl` only passes the key when one was
asked for, so a two-argument `draft_reply` (the Graph backend, whose
`createReply` builds the draft server-side, and any custom backend) is
unaffected -- Graph therefore has no marker, recorded as a connector-capability
gap. `_ImapBackend.check_drafts_writable()` resolves and SELECTs the drafts
folder without writing, backing the mailbox-connection check.

Ambient run-on-new-mail triggering exists at the UI-backend layer (an opt-in
per-org poller -- see `ui/backend/email_trigger.py` and `ui/backend/CLAUDE.md`),
not in this SDK layer. Tier 2 tools (SQL executor, Python sandbox) and real
email *sending* are planned but not yet implemented.

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

Register it exactly like any other tool: pass it via `load_pipeline(path,
toolkits=[...])` (see `core/loader.py`), or directly as `extra_tools={"lookup_order":
lookup_order}` to `validate_specification()`/`generate_specification()`, then
reference `lookup_order` by name in an agent's `tools:` list.

**Wiring it into a UI-backend-deployed pipeline needs one small addition.**
The pattern above is the whole story for the CLI/SDK (`load_pipeline()`) path.
`ui/backend/` only special-cases one kind of standalone, by-name-referenced
tool today: knowledge bases, via `ui/backend/knowledge_bases.py::load_knowledge_base_tools()`,
called from `main.py::_get_pipeline()`, `builder.py`, and `crud.py` to build
the `extra_tools` dict passed into `_build_pipeline()`. There's no generic
"custom tool" registry in the UI layer yet — to make `lookup_order` resolvable
in a pipeline deployed through the UI, add a parallel loader (same shape as
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

## Draft idempotency (email Phase 3a)

When `make_email_tools` is given a `draft_marker_prefix`, `email_draft_reply`
searches Drafts for the message's source key **under a process-wide per-key
lock** (`_lock_for_source_key`) before APPENDing, and returns
`_DRAFT_ALREADY_EXISTS` instead of writing a second draft.

Why a lock and not just a check: the stale-run watchdog releases a timed-out
run's overlap guard without being able to stop the worker, so the wedged worker
and the retry it enabled can both be drafting for the same message. They are
threads of the **same** process, which is what makes a process-wide lock a
closure rather than a mitigation. A multi-worker deployment reopens the window;
the email capability is single-instance by design.

The scan is advisory -- a failing `drafts_with_source_keys` logs and drafts
anyway. Refusing to draft because a mailbox search misbehaved would be a worse
failure than a rare duplicate.

A skipped draft is reported to the trace as `outcome: "draft_exists"`, distinct
from `draft_created` so the trace never claims a write that did not happen, but
both are in `automation_results.CONFIRMED_DRAFT_OUTCOMES` so a skipped draft
still excludes its message from the next retry.

## Attachment reading (email Phase 4b)

`email_read` now ends with a manifest of what the message carries — each
attachment's filename, declared type and size in KB — and
`email_read_attachment(message_id, filename)` extracts the text of exactly one
of them. Spec:
`docs/superpowers/specs/2026-08-18-email-phase-4b-attachments-design.md`.

**No path, no disk.** This is the decision the whole feature is shaped around.
`parse_file`'s own docstring — the contract the Trust boundaries note above
restates — says it reads whatever local path it is given with no sandboxing,
and that the caller is responsible for constraining which paths an agent can be
prompted to access. That contract is
fine for a knowledge base, where an *operator* chose the paths. It is exactly
wrong for email, where **the filename is chosen by whoever sent the message**,
in a process that holds the org's mailbox credentials and its Fernet secrets
key. So the tool takes a message id — already confined to the run's batch by
`make_email_tools`' `allowed_uids`, refused with the same `_OUT_OF_BATCH`
sentinel `email_read` uses — and a name compared for equality against *that
message's own* MIME part filenames. Parsing runs on `io.BytesIO`; nothing is
ever written to disk, so there is no temp-file lifetime to get wrong, nothing
to purge and no second copy for Phase 3b's retention sweep to miss. Path
traversal is not defended against; it is made **structurally impossible**,
because there is no path. `../../etc/passwd` is simply a name no MIME part has.

`file_parser.py` carries this: the parsers are `*_bytes` functions behind
`parse_bytes(data, filename)`, and `parse_file(path)` keeps its signature and
becomes a thin wrapper that reads the file and delegates. The knowledge-base
path is unchanged, including for a mis-encoded file. The two callers want
opposite things from a bad decode — an attachment must not fail a customer's
run, a knowledge base must not silently index mojibake — so `parse_bytes` takes
`lenient_text` (off by default, so both entry points agree) and only the
attachment path passes it. `parse_file` therefore still raises
`UnicodeDecodeError`, and `LocalFolderKnowledgeBase` still skips the document
with a warning. The switch is scoped to plain text; the binary parsers raise
either way.

`file_parser.py` also owns `PAGE_BREAK` (`\f`), the delimiter
`_parse_pdf_bytes` joins a PDF's pages with. It is public and lives with the
producer that writes it, because the consumer -- `core/knowledge_base.py`,
which chunks a PDF per page to cite an exact `p.N` -- has to split on exactly
what was written; a bare literal on each side could drift.

**Two tools, not one enriched one.** `email_read` lists; it never inlines
attachment text. Cost — Phase 4a exists because customers were billed for
content they did not need, and inlining would put a 40-page PDF into the
context of a message the model only had to acknowledge; on demand, the model
pays for what it decides to read. That spend is metered like any other token
usage, but be exact about what Phase 4a's monthly cost cap does with it: the
cap is evaluated at **dispatch** (`email_trigger.py::_start_triggered_run`),
so it stops the *following* run — it cannot interrupt the run that is
currently reading. Attachment reading materially raises per-run variance, so
the run that crosses the cap can overshoot it further than a body-only run
would. Auditability — each extraction is its own `tool_completed` event, so
the trace shows which attachments were opened.
Bounded output — `email_read` is already truncated at 8,000 characters, and an
inlined attachment would push the actual message body out of that window.

**Three limits, all checked before any parser sees the bytes**: 10 MB per
attachment (a parser is a decompressor, and an unbounded one is a
denial-of-service surface), 25 MB for a message's attachments in total (which
bounds the fifty-small-files case the per-attachment limit does not), and 8,000
characters of extracted text — `_MAX_BODY_CHARS`, the same cap the body has, so
a 500-page PDF cannot become two million tokens of context. Truncation is
announced in the returned text. Breaching a limit returns a plain sentence the
model can relay, never an exception: an attachment too large to read is a fact
about the message, not a fault in the automation. Every path through
`_attachment_impl` returns a string for the same reason.

**Types**: exactly the set `parse_file` already supported (`SUPPORTED_SUFFIXES`
— `.pdf`, `.xlsx`, `.xlsm`, `.docx`, `.xml` and the plain-text suffixes).
Anything else returns a sentence naming the type and listing what can be read.
**Archives are refused, not expanded** — recursive extraction of an
attacker-supplied archive is a zip-bomb surface, and "the invoice was inside a
zip" is not worth that risk until a customer actually presents it. **Dispatch
is by filename suffix**, as `parse_file`'s is: a sender can name a text file
`.pdf`, `pypdf` then raises, and the tool answers "couldn't be read" — the
correct outcome, and no reason for a content-sniffing layer. The suffix checked
is taken from the *matched part's* name, the same string `parse_bytes`
dispatches on, so the check and the parse cannot disagree.

**The trust boundary is unchanged in class.** Attachment text is
attacker-controlled input to the LLM, exactly as the body already is; this
widens the *volume* of such input, not its kind. The containment argument still
holds unaltered: there is no send verb, no SMTP exists anywhere in the process,
and the worst outcome of a successful injection is a strange draft a human sees
before anything leaves the building.

Two sender-controlled channels were closed while building it:

- **A filename can carry a literal newline.** RFC 2231 percent-encoding
  (`filename*=utf-8''bad%0Aline.pdf`) and RFC 2047 encoded-words both survive
  `get_filename()` intact — only ordinary header folding is normalised — so a
  sender could have forged extra lines into the manifest block. Note the
  asymmetry, because "the library would have caught it" is not an argument
  available here: `EmailMessage.add_attachment` *refuses to build* such a
  header, but nothing guards the read path. `_one_line()` now flattens `\r`/`\n`
  to spaces in the manifest and in `_attachment_impl`'s sentences. It cannot
  change dispatch — a space is neither a dot nor a separator.
- **Attachment text could have forged a draft confirmation.** Without its own
  branch in `_redacted_email_tool_data` (`adapters/langgraph_adapter.py`), an
  attachment result fell through to the `email_draft_reply` prefix matching, so
  a PDF whose extracted text began "Draft reply saved…" would have been
  recorded with `outcome: "draft_created"` for that message id. The branch now
  returns `{"summary", "message_id", "outcome": "attachment_read"}` and no
  extracted text; the filename is deliberately *not* recorded, being
  sender-chosen and unbounded, where `message_id` is bounded by
  `_bounded_message_id`.

  Accepted cost: a refused or unparseable attachment also traces as
  `attachment_read`, so it does not force `needs_attention` the way a raised
  call does. Distinguishing it would mean string-matching sentences that embed
  a sender-chosen filename — fragile, and an outcome the sender could steer.
  Deliberately not done.

### The enumerations that must all learn a new email tool's name

A new email tool is not one addition but six, and five of the six fail
**silently and open**:

| Site | What breaks if missed |
|---|---|
| `ui/backend/deploy_validation.py::EMAIL_TOOL_NAMES` | **The containment boundary.** `find_email_egress_conflicts` intersects each agent's tools against it, so a missing name lets a pipeline pair the tool with `http_get` — an injected attachment directing mailbox content into a fetched URL, exfiltration with no send verb involved. |
| `ui/backend/email_tools.py::EMAIL_TOOL_NAMES` + `_fixed_message_tools` | **A cross-tenant read.** `spec_uses_email` gates on the set, so an attachment-only team resolved as "does not use email", no per-org tools were built, and on a deployment that also sets `BESTTEAM_EMAIL_BACKEND` the run fell through to the process-env mailbox. The placeholder set has the same shape of miss: an org with no mailbox, or an undecryptable key, got no override for that one name, so it fell through to the env tool. (The two frozensets are duplicated by necessity — `src/bestteam/` must not import from `ui/backend/`.) |
| `adapters/langgraph_adapter.py::_EMAIL_TOOLS_NEEDING_REDACTION` | The generic 200-character `_summarize(result)` — i.e. extracted attachment text — lands in `trace_events`, `runs.output` and the live WebSocket broadcast. |
| `tools/__init__.py` — `REGISTRY` and `__all__` | The only one that fails *loudly*: the name cannot be resolved from a pipeline YAML at all. |
| `bestteam/__init__.py` — re-export and `__all__` | `from bestteam import email_read_attachment` fails. |
| `make_email_tools`' returned dict | The per-org scoped path silently lacks the tool while the env path has it. |

The first three rows carry four **structural** tests between them (the second
row has one per site), each asserting that every key `make_email_tools` returns
is a member of the list in question — so the next email tool cannot repeat
this. The failure-path branches that retain a `message_id` for correlation
(`langgraph_adapter.py` and its consumer in `ui/backend/runtime.py`, which
builds `failed_tool_message_ids`) name the tool too, so a raised attachment
read escalates its message to a human: a name in one and not the other is half
a wire.

## The parsed-text output contract

`parse_bytes(data, filename) -> str` is the single seam every consumer goes
through — knowledge-base ingestion (`ui/backend/ingestion.py`), folder-based
knowledge bases (`core/knowledge_base.py`), and `email_read_attachment`. What it
returns is not free-form text: `core/knowledge_base.py` reads structure back out
of it, so the shape is a contract between the two modules.

| Element | Form | Read by |
|---|---|---|
| Document header | one bracketed line, `[PDF: name — N page(s)]` / `[Word: name]` / `[Excel: name]` / `[XML: name]` | `_PARSER_HEADER_RE` (`_has_extractable_text`, and the heading reader, which must not let it shadow a section) |
| Section heading | Markdown `# ` … `#### `, deeper levels clamped | `_MARKDOWN_HEADING_RE` → `_Chunk.heading` |
| Table | `[Table N]` / `[Sheet: name]` marker, then CSV rows, **one row per line**, in document order, ended by a blank line | `_TABLE_MARKER_RE`, `_chunk_table_block` (repeats marker + header row) |
| PDF page break | `PAGE_BREAK` (`\f`) | `_chunk_document` → `_Chunk.page` |
| XML element | `<tag attr="v"> text`, two spaces of indent per level | `_chunk_xml_document` (indentation *is* the tree) |

Two consequences worth stating outright. A cell's own line breaks are collapsed
to spaces (`_one_line`) because a newline inside a cell would silently become an
extra, shorter row on the chunker's side. And **a heavier parser can be swapped
in behind this contract without the chunker changing**: docling's
`export_to_markdown()` already produces `#` headings and in-order content, which
is the shape chosen here. The intended next step is a *triage router* in front
of `parse_bytes` — lightweight parsers for documents they handle well, an
out-of-process heavy stack for the ones they don't (scanned PDFs, multi-column
layouts, PDF tables). None of that exists yet; only the output contract it would
have to satisfy does.

### Two notes on the bytes refactor

- **`parse_file`'s "bytes and path agree" tests are now tautological.** Both
  entry points execute the same `parse_bytes` code, so an assertion comparing
  them verifies the plumbing, not the parsing. It was demonstrated empirically:
  a mutation that broke `_decode_text`'s newline translation left the agreement
  test green on Windows, where the file on disk genuinely contains `\r\n`. The
  guard that actually holds the property asserts on literal bytes. General
  lesson for this module: once two entry points are expressed in terms of each
  other, a test comparing them can no longer pin the shared code.
- **Peak memory for a parsed file is now the whole file**, where
  `openpyxl(read_only=True)` previously streamed from disk. Inherent to parsing
  from bytes; bounded for attachments by the 10 MB limit, unbounded for a
  knowledge-base workbook. Recorded, not worked around.
