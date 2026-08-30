# bestteam — `src/bestteam/tools/` (built-in tools)

Built-in tools clients can attach to any Agent. Root `CLAUDE.md` for the
overview; `docs/DECISIONS.md` for reasoning; the dated specs under
`docs/superpowers/specs/` and git history for per-feature narrative.

| Tool | Env var | Extra dep |
|---|---|---|
| `web_search(query, max_results=5)` | `TAVILY_API_KEY` | `bestteam[tools-search]` |
| `local_business_search(query, max_results=5)` | `GOOGLE_MAPS_API_KEY` | `bestteam[tools-places]` |
| `parse_file(path)` | — | `bestteam[tools-files]`; `.xml` is stdlib |
| `http_get(url, headers_json="{}")` | — | `bestteam[tools-http]` |
| `calculator(expression)` | — | none |
| `email_find(query="")` | `BESTTEAM_EMAIL_BACKEND` + creds | `graph`: `bestteam[tools-email]`; `imap`: none |
| `email_read(message_id)` | (same) | (same) |
| `email_read_attachment(message_id, filename)` | (same) | (same) + `tools-files` |
| `email_draft_reply(message_id, body)` | (same) | (same) |

In YAML, tools are referenced by name and resolved via `tools.REGISTRY`.

`local_business_search` wraps Google Places API (New) Text Search. The query is
one free-text string carrying both business type and area
(`"electrician in Parramatta NSW"`) — no separate lat/lng/radius, matching
`web_search`'s shape. Only 5xx is retried, matching `http_get`.

## Trust boundaries

⚠️ **`parse_file` reads any local path with no sandboxing** and **`http_get`
fetches any URL whose host doesn't resolve to a private/internal address**. Both
are intentionally broad — their purpose is to read files and fetch URLs the agent
is told to. **Callers exposing them to an LLM remain responsible** for
constraining which paths/URLs an agent can be prompted to access, and for
network-layer egress controls.

The SSRF check (`http_client.check_host_allowed`) resolves the host, rejects
private/internal addresses, and **returns the validated IP**; `http_get` then
**pins the connection to that IP** (keeping the hostname for the `Host` header
and TLS SNI/cert), so `httpx` never re-resolves and a DNS-rebinding TOCTOU can't
slip past. Pinning uses the first resolved address, trading happy-eyeballs
failover for that guarantee.

`.xml` parsing uses stdlib `xml.etree.ElementTree`, which **doesn't resolve
external entities (not XXE-vulnerable)** but has **no protection against
entity-expansion ("billion laughs") DoS** — the same unmitigated-DoS posture the
PDF/Word parsers have against decompression bombs, not a new risk class. (The
Excel path is the exception: it refuses a workbook whose archive unpacks past
300 MB or declares more than 5,000,000 cells — deliberately constants, not
settings, like the PBKDF2 count.)

## Email tools (`email_client.py`) — draft-only by design

⚠️ **There is no send verb and no SMTP anywhere.** The worst outcome is a bad
draft a human reviews in their own mail client. This is the containment argument
every other email decision rests on. Spec:
`2026-07-15-email-toolkit-design.md`.

Two backends behind one seam: **Microsoft Graph** (M365, app-only
client-credentials OAuth, `createReply` for correctly threaded drafts) and
**generic IMAP** (stdlib `imaplib`, `BODY.PEEK`/readonly so **reading never marks
messages seen**, MIME reply built with `In-Reply-To`/`References`, APPENDed to the
Drafts folder resolved via `BESTTEAM_IMAP_DRAFTS` → SPECIAL-USE → `"Drafts"`).

The built-in `email_triage_reply` Skill (seeded on backend bootstrap) packages the
triage playbook plus these tools for Team Builder customers.

**Trust boundary**: email bodies — and attachment text — are attacker-controlled
input to the LLM. Mitigations: no send capability exists, the seeded skill
instructs the agent to treat message content as data rather than instructions,
and both body and extracted text are capped at 8,000 characters. For Graph, use
least privilege: `Mail.ReadWrite` **application** permission restricted to the
single mailbox with an Exchange Application Access Policy.

### Per-mailbox seam (used by the UI backend for multi-tenancy)

`_ImapBackend(host=, user=, password=, port=, drafts=, restrict_to_public=,
token_provider=)` builds from explicit params, not env; `make_email_tools(backend)`
returns the tools bound to it — **same names and docstrings the model sees for
the env tools**.

`_connect()` always uses a verifying TLS context (`ssl.create_default_context()`
— **imaplib's own fallback does NOT verify the cert**) and a bounded socket
timeout. **There is no verification-off switch, by design**: an internal server
with a private CA must have that CA trusted (point `SSL_CERT_FILE` at the bundle).

`restrict_to_public=True` (set by the per-org store path, where the host is
customer-supplied) additionally re-runs the SSRF check on **every** connect and
pins the socket to the checked IP while keeping the hostname for SNI/cert
(`_PinnedIMAP4_SSL`). It stays `False` for the operator-trusted env path, which
may legitimately point at an internal IMAP server.

⚠️ **Auth strategy: exactly one of `password=` or `token_provider=`, checked in
the constructor rather than at connect time.** With a provider, `_connect()`
authenticates with SASL `XOAUTH2` instead of `login()`. **Everything above
`_connect()` is unchanged and shared by both** — after `AUTHENTICATE` succeeds the
session is an ordinary IMAP session, so `find`/`read`/`draft_reply`/
`summaries_for`/`drafts_with_source_keys`/`check_drafts_writable`, and the whole
of `ui/backend/email_trigger.py` including the UID cursor and event ledger, need
no knowledge of which auth was used. **The token is fetched before the socket is
opened**, so a credential failure leaves no dangling connection and stays
distinguishable from a mailbox-access failure.

`_oauth.MicrosoftClientCredentialsToken` (app-only, scope
`https://outlook.office365.com/.default`) is **stdlib `urllib` only, deliberately
no httpx**: the per-org IMAP path has no third-party HTTP dependency and
`backend-optional-deps` runs without optional extras. ⚠️ **It caches the token
until 60 seconds before expiry** — the poller is long-lived and the token lasts
about an hour, so caching forever would break it. (`_GraphBackend._token()` *does*
cache forever; a pre-existing bug in the unrelated env-only Graph path, recorded
in `docs/STATUS.md`.) Spec: `2026-08-17-email-phase-2-microsoft-oauth-design.md`.

`make_email_tools(backend, allowed_uids=, draft_marker_prefix=)` stamps
`X-BestTeam-Source-Key: <prefix><message_id>` on each draft when a prefix is
given, so a retry can reconcile against the mailbox
(`drafts_with_source_keys`) and recognise a draft really APPENDed but never
recorded. `_draft_impl` only passes the key when asked, so a two-argument
`draft_reply` (Graph, whose `createReply` builds server-side) is unaffected —
**Graph therefore has no marker, a recorded connector-capability gap.**

Ambient run-on-new-mail triggering lives at the UI-backend layer, not here. Tier 2
tools (SQL executor, Python sandbox) and real email *sending* are planned, not
implemented.

### Draft idempotency

With a `draft_marker_prefix`, `email_draft_reply` searches Drafts for the
message's source key **under a process-wide per-key lock**
(`_lock_for_source_key`) before APPENDing, and returns `_DRAFT_ALREADY_EXISTS`
instead of writing a second draft.

**Why a lock and not just a check**: the stale-run watchdog releases a timed-out
run's overlap guard without being able to stop the worker, so the wedged worker
and the retry it enabled can both be drafting for the same message. They are
threads of the **same** process, which is what makes a process-wide lock a
closure rather than a mitigation. **A multi-worker deployment reopens the window;
the email capability is single-instance by design.**

The scan is **advisory** — a failing `drafts_with_source_keys` logs and drafts
anyway. Refusing to draft because a mailbox search misbehaved would be a worse
failure than a rare duplicate.

A skipped draft traces as `outcome: "draft_exists"`, distinct from
`draft_created` so the trace never claims a write that didn't happen — but both
are in `CONFIRMED_DRAFT_OUTCOMES`, so a skipped draft still excludes its message
from the next retry.

### Attachment reading

`email_read` ends with a manifest (filename, declared type, size in KB);
`email_read_attachment(message_id, filename)` extracts exactly one. Spec:
`2026-08-18-email-phase-4b-attachments-design.md`. Reasoning: `docs/DECISIONS.md`.

⚠️ **No path, no disk — this is the decision the whole feature is shaped
around.** `parse_file`'s contract is fine for a knowledge base, where an
*operator* chose the paths. It is exactly wrong for email, where **the filename is
chosen by whoever sent the message**, in a process holding the org's mailbox
credentials and its Fernet secrets key. So the tool takes a message id — already
confined to the run's batch by `allowed_uids`, refused with the same
`_OUT_OF_BATCH` sentinel `email_read` uses — and a name compared for **equality**
against *that message's own* MIME part filenames. Parsing runs on `io.BytesIO`;
nothing is ever written to disk, so there is no temp-file lifetime to get wrong,
nothing to purge, and no second copy for the retention sweep to miss. **Path
traversal is not defended against; it is structurally impossible**, because there
is no path — `../../etc/passwd` is simply a name no MIME part has.

**Two tools, not one enriched one.** `email_read` lists; it never inlines
attachment text. *Cost* — inlining would put a 40-page PDF into the context of a
message the model only had to acknowledge; on demand, the model pays for what it
decides to read. ⚠️ **Be exact about what the monthly cost cap does with that
spend: the cap is evaluated at dispatch, so it stops the *following* run — it
cannot interrupt the run currently reading.** Attachment reading materially raises
per-run variance, so the run that crosses the cap can overshoot further than a
body-only run would. *Auditability* — each extraction is its own `tool_completed`
event. *Bounded output* — an inlined attachment would push the message body out of
the 8,000-character window.

**Three limits, all checked before any parser sees the bytes**: 10 MB per
attachment (a parser is a decompressor, and an unbounded one is a DoS surface),
25 MB per message in total (bounding the fifty-small-files case), and 8,000
characters of extracted text. Truncation is announced in the returned text.
**Breaching a limit returns a plain sentence the model can relay, never an
exception** — an attachment too large to read is a fact about the message, not a
fault in the automation. Every path through `_attachment_impl` returns a string.

**Types** are exactly `SUPPORTED_SUFFIXES`. **Archives are refused, not
expanded** — recursive extraction of an attacker-supplied archive is a zip-bomb
surface. **Dispatch is by filename suffix**, as `parse_file`'s is: a sender can
name a text file `.pdf`, `pypdf` raises, and the tool answers "couldn't be read" —
the correct outcome, and no reason for a content-sniffing layer. The suffix is
taken from the *matched part's* name, the same string `parse_bytes` dispatches on,
so check and parse cannot disagree.

Two sender-controlled channels closed while building it:

- **A filename can carry a literal newline.** RFC 2231 percent-encoding
  (`filename*=utf-8''bad%0Aline.pdf`) and RFC 2047 encoded-words both survive
  `get_filename()` intact — only ordinary header folding is normalised — so a
  sender could forge extra lines into the manifest. ⚠️ **Note the asymmetry:
  `EmailMessage.add_attachment` refuses to *build* such a header, but nothing
  guards the read path**, so "the library would have caught it" is not available
  here. `_one_line()` flattens `\r`/`\n` to spaces in the manifest and in
  `_attachment_impl`'s sentences. It cannot change dispatch — a space is neither a
  dot nor a separator.
- **Attachment text could forge a draft confirmation.** Without its own branch in
  `_redacted_email_tool_data`, an attachment result fell through to the
  `email_draft_reply` prefix matching, so a PDF whose text began "Draft reply
  saved…" would be recorded `outcome: "draft_created"` for that message id. The
  branch returns `{"summary", "message_id", "outcome": "attachment_read"}` and no
  extracted text; **the filename is deliberately not recorded**, being
  sender-chosen and unbounded, where `message_id` is bounded.

  Accepted cost: a refused or unparseable attachment also traces as
  `attachment_read`, so it doesn't force `needs_attention` the way a raised call
  does. Distinguishing it would mean string-matching sentences that embed a
  sender-chosen filename — fragile, and an outcome the sender could steer.

### ⚠️ Six enumerations must all learn a new email tool's name

**A new email tool is not one addition but six, and five of the six fail silently
and open.**

| Site | What breaks if missed |
|---|---|
| `deploy_validation.py::EMAIL_TOOL_NAMES` | **The containment boundary.** `find_email_egress_conflicts` intersects each agent's tools against it, so a missing name lets a pipeline pair the tool with `http_get` — an injected attachment directing mailbox content into a fetched URL. Exfiltration with no send verb involved. |
| `email_tools.py::EMAIL_TOOL_NAMES` + `_fixed_message_tools` | **A cross-tenant read.** `spec_uses_email` gates on the set, so an attachment-only team resolves as "does not use email", no per-org tools are built, and on a deployment that also sets `BESTTEAM_EMAIL_BACKEND` the run falls through to the process-env mailbox. The placeholder set misses the same way: an org with no mailbox gets no override for that one name. (The two frozensets are duplicated **by necessity** — `src/bestteam/` must not import from `ui/backend/`.) |
| `langgraph_adapter.py::_EMAIL_TOOLS_NEEDING_REDACTION` | The generic 200-char `_summarize(result)` — i.e. extracted attachment text — lands in `trace_events`, `runs.output` and the live WebSocket broadcast. |
| `tools/__init__.py` — `REGISTRY` and `__all__` | The only one that fails **loudly**: the name can't be resolved from a pipeline YAML at all. |
| `bestteam/__init__.py` — re-export and `__all__` | `from bestteam import …` fails. |
| `make_email_tools`' returned dict | The per-org scoped path silently lacks the tool while the env path has it. |

The first three rows carry four **structural** tests between them, each asserting
that every key `make_email_tools` returns is a member of the list in question — so
the next email tool cannot repeat this. The failure-path branches that retain a
`message_id` for correlation (`langgraph_adapter.py` and its consumer in
`runtime.py`, which builds `failed_tool_message_ids`) name the tool too: **a name
in one and not the other is half a wire.**

## The parsed-text output contract

`parse_bytes(data, filename) -> str` is the single seam every consumer goes
through — KB ingestion, folder-based KBs, and `email_read_attachment`. **What it
returns is not free-form text**: `core/knowledge_base.py` reads structure back out
of it, so the shape is a contract between the two modules.

| Element | Form | Read by |
|---|---|---|
| Document header | one bracketed line: `[PDF: name — N page(s)]` / `[Word: name]` / `[Excel: name]` / `[CSV: name]` / `[XML: name]` | `_PARSER_HEADER_RE` |
| Section heading | Markdown `# ` … `#### `, deeper levels clamped | `MARKDOWN_HEADING_RE` (**defined here, imported there**) → `_Chunk.heading` |
| Table | `[Table N]` / `[Sheet: name]` / `[CSV: name]` marker, then CSV rows, **one row per line**, in document order, ended by a blank line | `TABLE_MARKER_RE` (**defined here, imported there**), `_chunk_table_block` |
| PDF page break | `PAGE_BREAK` (`\f`) | `_chunk_document` → `_Chunk.page` |
| XML element | `<tag attr="v"> text`, two spaces of indent per level | `_chunk_xml_document` (**indentation IS the tree**) |

`PAGE_BREAK`, `MARKDOWN_HEADING_RE` and `TABLE_MARKER_RE` all live **here, with
the producer that writes them**, and are imported by the consumer. Two copies
could drift, and the drift would be silent — surfacing only as a wrong citation.

**Once a shape is meaningful, the producer owes the consumer an unambiguous
encoding of it.** All three of these are the parser's job, because only the parser
can still tell generated structure from a document's own text:

- **A cell's own line breaks collapse to spaces** (`_one_line`), and **a row with
  no text in any cell is dropped, not rendered**. A newline inside a cell would
  become an extra, shorter row; and in a *one-column* table an empty row renders
  as the empty string — the blank line that ends the block — so a spacer row would
  drop every row after it out of the table and index it as prose with no header
  and no citation.
- **A Normal-styled paragraph whose text begins `# ` … `#### ` is
  backslash-escaped** (`_escape_heading_shaped`), because rendering Word's heading
  styles as `#` lines is exactly what makes that shape ambiguous — unescaped, the
  chunker cites such a paragraph as the section its chunk opens under and cuts a
  boundary at it. Cells need no escaping: a table block is split on the default
  separators and cited by its marker, so nothing reads a heading out of a row.
- **A line that reads like a table marker is escaped the same way**
  (`_escape_marker_shaped`) — the identical defect one shape over. A Word
  paragraph reading `[Table 2]` opened a block that swallowed every paragraph
  after it as rows; a one-column CSV row reading `[CSV: other.csv]` split the
  table and cited the rest as a document the collection doesn't contain. **Word's
  *table* rows are the exception and get none**: `_docx_segments` runs a block to
  the blank line after it and ignores a marker inside one. A sheet and a CSV are
  split marker-to-marker, which is why their rows do need it.

**`.csv` is rendered as a table block, not prose.** Its document header line **is**
the block's marker — a CSV is a single table, so a separate `[Sheet: …]` line
would name the same thing twice. Rows go through `csv.reader`/`csv.writer` rather
than a newline split and a `,`.join, **because both directions matter**: Excel
writes a cell containing a line break as a quoted field across two physical lines
(which would become two rows, every column under the wrong header), and a value
containing a comma has to stay quoted on the way out. **The delimiter is not
sniffed** — a semicolon- or tab-delimited export reads as one field per row.
`_parse_excel_bytes` and the Word-table renderer write their rows through the
same `csv.writer` + `_one_line`, so in all three tabular formats a cell keeps
a comma quoted and a line break collapsed. ⚠️ **Excel fidelity limits: a
formula cell is its *cached* value (`data_only=True` — a workbook written
programmatically that never passed through Excel has none, so its formula
cells ingest as empty), a merged range keeps only its top-left value,
number/date formats are lost, and hidden sheets are indexed like visible
ones.** A password-protected workbook (OLE2 magic) and a standalone chart tab
(`xl/chartsheets/` in the archive — openpyxl's read-only reader crashes inside
`load_workbook` on one, so there is no sheet object to skip) are refused with
customer-readable advice.

`csv.reader`'s default 131,072-character field limit is **raised at import**
(`_CSV_FIELD_LIMIT`): it guards a *streaming* reader's memory and nothing here
streams — the whole document is a string before the reader sees it — so it was
rejecting valid documents (a long notes column) inside the 30 MB upload cap.
Raised once at import rather than set and restored per parse, because the setting
is process-global and ingestion parses on a thread pool. Consequence: an
unbalanced quotation mark no longer raises at any size — the reader swallows the
rest of the file as one field, which is what it always did under the old limit.

**A heavier parser can be swapped in behind this contract without the chunker
changing**: docling's `export_to_markdown()` already produces `#` headings and
in-order content, which is the shape chosen here. The intended next step is a
*triage router* in front of `parse_bytes` — lightweight parsers for documents they
handle well, an out-of-process heavy stack for the ones they don't (scanned PDFs,
multi-column layouts, PDF tables). **None of that exists yet; only the output
contract it would have to satisfy does.**

### Plain-text encodings

`_decode_text` tries UTF-8, then whatever a BOM declares, then GB18030 — because a
plain-text document a customer produces is very often not UTF-8. Chinese Windows'
Notepad and Excel's "CSV (comma delimited)" both write GBK; Excel's "Unicode text"
writes UTF-16 with a BOM; Excel's "CSV UTF-8" writes a UTF-8 BOM, read with
`utf-8-sig` so it doesn't survive as the document's first character (and hence
part of its first heading or column name).

⚠️ **The BOMs are tested longest-first, because one is a prefix of another.** A
UTF-32 LE BOM begins with the two bytes of a UTF-16 LE BOM, and a UTF-32 file read
as UTF-16 **does not fail** — it decodes into the same text with a NUL between
every character, indexed without complaint.

⚠️ **GB18030 needs a guard**: it is permissive enough that a Western-encoded
document whose high bytes form valid pairs decodes into Chinese nonsense, so its
result is accepted **only when it contains a run of two or more adjacent CJK
characters**. The run is the whole point — Latin-1 Spanish, Portuguese, German and
Swedish spell each accented letter with a single high byte, which pairs with the
ASCII byte after it into one lone Han character, so "contains CJK" is satisfied by
ordinary Western prose (`El señor Muñoz` → `El se馉r Mu馉z`). Chinese text comes
in runs; that accident does not. It refuses the genuine GB18030 document with no
two adjacent Chinese characters — the same trade as before: a loud refusal the
customer can act on beats a silent one nobody notices.

A document in none of those encodings is still **reported rather than indexed as
mojibake**, as a `ConfigurationError` naming the file and saying to re-save as
UTF-8. `lenient_text` (the attachment path) is unchanged in contract — it still
never raises — but is now the **last resort rather than the first**, so a GBK
attachment arrives as its own text instead of U+FFFDs.

### Two notes on the bytes refactor

- ⚠️ **`parse_file`'s "bytes and path agree" tests are now tautological.** Both
  entry points execute the same `parse_bytes` code, so an assertion comparing them
  verifies the plumbing, not the parsing. Demonstrated empirically: a mutation
  breaking `_decode_text`'s newline translation left the agreement test green on
  Windows, where the file on disk genuinely contains `\r\n`. The guard that
  actually holds the property asserts on literal bytes. **General lesson: once two
  entry points are expressed in terms of each other, a test comparing them can no
  longer pin the shared code.**
- **Peak memory for a parsed file is now the whole file**, where
  `openpyxl(read_only=True)` previously streamed from disk. Inherent to parsing
  from bytes; bounded for attachments by the 10 MB limit, **unbounded for a
  knowledge-base workbook**. Recorded, not worked around.

## Connecting to external systems (ERP / order databases)

A customer's order data lives in a real system, not a folder of text files.
⚠️ **Don't model this as a `knowledge_base`.** KBs do semantic/keyword retrieval
over unstructured documents; structured business records (an order, looked up by
ID) want an exact parameterized lookup. That's a `tool`.

**SDK-level pattern (works today):** a plain Python function shaped like a
built-in tool — one string argument in, a string out, and a `__doc__` describing
what it does. **That docstring is sent to the model as the tool's description, so
write it for the LLM, not just for a human reader.** Register it via
`load_pipeline(path, toolkits=[...])` or as `extra_tools={...}`, then reference it
by name in an agent's `tools:`.

**Wiring it into a UI-backend-deployed pipeline needs one small addition.**
`ui/backend/` special-cases only one kind of standalone by-name tool today:
knowledge bases, via `load_knowledge_base_tools()`, called from `_get_pipeline()`,
`builder.py` and `crud.py`. There is **no generic custom-tool registry in the UI
layer yet** — add a parallel loader of the same shape and merge its output into
`extra_tools` at those same three call sites. A small extension, but **a real code
change, not configuration.**

Security boundaries:

- Use a **read-only** DB role or API token for the tool's internal connection.
- ⚠️ **Tenant isolation is the tool function's responsibility.** The platform's
  org scoping governs its own DB rows, **not** what a custom tool's internal
  connection can reach. On a shared multi-org deployment, don't wire a custom tool
  to one customer's backend at all until per-org credentials exist.
- **A normal "not found" should be a returned string, not a raised exception**:
  `_run_agent`'s tool loop catches exceptions and turns them into generic error
  text — right for genuine failures, worse than a clean "no order found" for an
  expected empty result.

A generic authenticated REST connector is a reasonable next step but is a bigger
platform feature (self-service config, credential storage) and isn't designed.
