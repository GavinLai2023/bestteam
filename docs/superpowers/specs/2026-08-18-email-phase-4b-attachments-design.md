# Email automation Phase 4b — attachments

**Status:** approved 2026-08-18
**Builds on** Phase 4a's filter and budgets, Phase 1's `inbox_events` ledger,
and the draft-only toolkit's existing trust boundaries.

## The problem

The model cannot see attachments. `_extract_text_body` calls
`msg.get_body(preferencelist=("plain", "html"))`, which by construction
returns the body part and nothing else. A supplier sends a quote as a PDF, a
customer sends a spreadsheet of line items, a tenant photographs a broken
boiler and attaches the invoice — and the team drafts a reply having read only
the covering sentence, or worse, "please see attached".

This is the last gap that makes a generic email team look unintelligent to the
customer paying for it.

## What this phase delivers

1. `email_read` reports what a message carries: each attachment's filename,
   type and size.
2. A new `email_read_attachment(message_id, filename)` tool extracts one
   attachment's text on demand.

Explicitly out of scope: images and OCR, archives, any send capability
(Phase 5, undesigned), and any change to the filter or budget logic.

---

## The decision that shapes everything: no path, no disk

`parse_file` (`src/bestteam/tools/file_parser.py`) already parses PDF, XLSX,
DOCX, XML and plain text. It would be the obvious thing to reuse — and reusing
it *as it stands* would be the mistake this phase exists to avoid.

Its own docstring says why:

> This tool reads whatever local path it is given, with no sandboxing — the
> same trust boundary as `http_get` fetching arbitrary URLs. If this tool is
> exposed to an LLM agent, the caller is responsible for constraining which
> paths the agent can be prompted to access.

That contract is fine for knowledge bases, where an operator chooses the
paths. It is exactly wrong for email, where **the filename is chosen by whoever
sent the message**. A tool that took a path — even one derived from an
attachment name — would put attacker-controlled text into a filesystem lookup
in a process that holds the org's mailbox credentials and its Fernet secrets
key.

So:

- **The tool never accepts a path.** Its arguments are a message id (already
  constrained to the run's batch by `make_email_tools`' `allowed_uids`) and an
  attachment filename matched against that message's own MIME parts.
- **No attachment is ever written to disk.** Parsing runs on
  `io.BytesIO` — `pypdf`, `openpyxl` and `python-docx` all accept a file-like
  object. Nothing to purge, nothing to leak, no temp-file lifetime to get
  wrong, and no second copy for Phase 3b's retention sweep to miss.
- `file_parser.py` grows internal `*_bytes` parsers; `parse_file(path)` keeps
  its public signature and becomes a thin wrapper that reads the file and
  delegates. The knowledge-base path is unchanged, **including for a
  mis-encoded file**. Sharing one decoder between the two callers surfaced a
  genuine conflict: an attachment must never fail a customer's run, because a
  sender can name anything `.txt`; a knowledge base must never silently index
  mojibake, because U+FFFD chunks are unsearchable and announce nothing. Rather
  than pick one, `parse_bytes` takes `lenient_text` — off by default, so the
  two entry points agree — and only the attachment path turns it on.
  `parse_file` keeps the strict `UnicodeDecodeError` that lets
  `_load_document_chunks` skip a bad document with a warning. The switch is
  scoped to plain text; the binary parsers raise either way.

Path traversal is not defended against here. It is made **structurally
impossible**: there is no path.

## Attachments are already being downloaded

`_fetch_message` issues `BODY.PEEK[]` — the *entire* message, attachments
included — and `_extract_text_body` then discards everything but the body. So
this phase adds no network cost to `email_read`; the bytes were already in
memory and being thrown away.

It also means one pre-existing weakness is now worth recording rather than
introducing: a 25 MB attachment is already pulled into the poller process on
every read, and always has been. This phase bounds what is *parsed*, not what
is fetched. Fetching individual MIME parts by `BODY.PEEK[2]` would fix it and
is deliberately not attempted here — it would reshape `_fetch_message`, which
every email tool depends on, for a benefit this phase does not need.

## Two tools, not one enriched one

`email_read` gains a manifest. It does **not** inline attachment text.

```
From: alice@example.com
Subject: Quote for the boiler
...
Attachments (2):
  - quote-2026-08.pdf (application/pdf, 84 KB)
  - terms.docx (application/vnd.openxmlformats-...document, 12 KB)
```

`email_read_attachment("42", "quote-2026-08.pdf")` returns the extracted text.

Three reasons this split rather than automatic inlining:

- **Cost.** Phase 4a exists because customers were billed for content they did
  not need. Inlining every attachment would put a 40-page PDF into the context
  of a message the model only needed to acknowledge. On demand, the model pays
  for what it decides to read.
- **Auditability.** Each extraction is its own `tool_completed` event, so the
  trace shows exactly which attachments were opened.
- **Bounded output.** `email_read`'s result stays predictable in size, which
  matters because it is already truncated at 8,000 characters and a large
  inlined attachment would push the actual message body out of that window.

## Limits

Three, all checked **before** any parser sees the bytes:

| Limit | Value | Why |
|---|---|---|
| Per attachment | 10 MB | Above any realistic business document; a parser is a decompressor and an unbounded one is a denial-of-service surface. |
| Per message, total | 25 MB | Bounds a message carrying fifty small attachments, which the per-attachment limit alone does not. |
| Extracted text | 8,000 characters | Matches `_MAX_BODY_CHARS` exactly. A 500-page PDF must not become 2 million tokens of context. Truncation is announced in the returned text, as the body's already is. |

Exceeding a limit returns a plain sentence the model can act on and relay —
never an exception that fails the run. An attachment too large to read is a
fact about the message, not a fault in the automation.

## Types

The set `parse_file` already supports: `.pdf`, `.xlsx`, `.xlsm`, `.docx`,
`.xml`, and the plain-text suffixes. Anything else — images, archives, legacy
`.doc`/`.xls`, executables — returns a sentence naming the type and saying it
cannot be read.

**Archives are refused, not expanded.** Recursive extraction of an
attacker-supplied archive is a zip-bomb surface, and "the invoice was inside a
zip" is not a case worth that risk until a customer actually presents it.

**Dispatch is by filename suffix**, exactly as `parse_file` does. A sender can
of course name a text file `.pdf`; `pypdf` then raises and the tool returns
"couldn't be read". That is the correct outcome and needs no separate
content-sniffing layer.

## Trust boundary, stated plainly

Attachment text is **attacker-controlled input to the LLM**, exactly as the
message body already is. This phase widens the *volume* of such input, not its
class. The containment argument is unchanged and still holds: the toolkit has
no send verb, no SMTP exists anywhere in the process, and the worst outcome of
a successful injection remains a strange draft that a human sees before
anything leaves the building.

`_redacted_email_tool_data` in `adapters/langgraph_adapter.py` must cover the
new tool the way it covers `email_read`: the trace records the outcome and a
length-bounded message id, **never the extracted content**. Missing this would
put attachment text into `trace_events` for every run, which Phase 3b's purge
would then have to remove and which the business-safe trace contract says must
never be there in the first place.

### The exhaustive lists that must all learn the new name

A new email tool is not one addition; it is five, and every one of them is an
enumeration that fails **silently and open** if missed. Phase 4a already lost a
CI job to exactly this class of bug (`tests/test_db.py` asserting an exact
table-name set). The complete set:

| Site | What breaks if missed |
|---|---|
| `ui/backend/deploy_validation.py::EMAIL_TOOL_NAMES` | **The containment boundary.** A workflow could pair `email_read_attachment` with `http_get`, and an injected attachment could direct the model to put mailbox content into a fetched URL. This is the single most important line in the phase. |
| `adapters/langgraph_adapter.py::_EMAIL_TOOLS_NEEDING_REDACTION` | Attachment text lands in `trace_events` on every run. |
| `src/bestteam/tools/__init__.py` — `REGISTRY` and `__all__` | The tool cannot be named in a workflow YAML at all. |
| `src/bestteam/__init__.py` — the re-export and `__all__` | The documented `from bestteam import ...` import fails. |
| `make_email_tools`' returned dict | The per-org scoped path silently lacks the tool while the env-configured path has it. |

`EMAIL_TOOL_NAMES` deserves its own regression test asserting that every key
`make_email_tools` returns appears in it — a structural check, so the next email
tool cannot repeat this.

## Default-on, and why that is defensible

The tool joins `make_email_tools`' dictionary, so every email team gets it on
upgrade, with no per-org switch.

A switch was considered and rejected as YAGNI. Cost is already governed by the
model's own choice — a message with no attachments produces no call — and the
spend it does incur is metered like any other token usage, so Phase 4a's
monthly cost cap sees it with no new accounting. Note precisely what that cap
can and cannot do: it is evaluated at **dispatch**
(`email_trigger.py::_start_triggered_run`, against `spent_this_month`), so it
stops the *following* run — it cannot interrupt the run currently reading a
40-page PDF. Attachment reading materially raises per-run variance, so the run
that crosses the cap can overshoot it by more than a body-only run would. That
is a bounded overshoot on a metered, capped account, not an unbounded one, and
it is the honest version of the claim. The injection surface is the same class
as the body the model already reads. A flag would add a column, a route, a
panel and a migration to protect against a risk the product already accepts one
paragraph earlier.

## Testing

- `tests/test_file_parser.py` (extend) — the `*_bytes` parsers against the
  same fixtures the path-based ones use, proving the refactor changed nothing.
- `tests/test_email_tools.py` (extend) — a multipart fixture with two
  attachments: the manifest in `email_read`; extraction by name; an unknown
  name; an unsupported type; each of the three limits; a `.pdf` whose bytes are
  not a PDF; a message with no attachments at all.
- `tests/test_trace_redaction.py` or wherever `_redacted_email_tool_data` is
  pinned (extend) — the new tool's trace data carries no extracted text.
- `make_email_tools`' batch scoping must be shown to apply: reading an
  attachment of a message outside the run's `allowed_uids` is refused exactly
  as `email_read` refuses it.

## Known limitations this phase accepts

- **No OCR and no image understanding.** A photographed invoice is invisible.
  The most likely next request, and deliberately not started here.
- **Archives are refused.**
- **The whole message is still fetched** (`BODY.PEEK[]`), so a large
  attachment costs memory even when nothing reads it. Pre-existing; recorded,
  not fixed.
- **Extraction is text-only.** A spreadsheet becomes CSV rows and a Word table
  becomes text; layout, formulas and images inside documents are lost.
- **Dispatch trusts the filename suffix**, so a mislabelled file fails at the
  parser rather than being detected up front.
