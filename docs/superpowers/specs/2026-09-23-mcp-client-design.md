# MCP client: shape, constraints, and when to build it

**Date:** 2026-09-23
**Status:** Not a decision. A record of one brainstorm, so the next session
does not repeat it. Nothing here changes the freeze (`docs/STATUS.md`).

## One sentence

bestteam does not get an MCP client now, because the two boxes we would plug
in first are one we already have and one that is forty lines of native tool —
but the constraints below are real, they were checked against the code, and
whoever builds the client later should start from them rather than from a
blank page.

## What was asked, and the two directions it splits into

The request was "prepare the platform for MCP". MCP splits into two
subsystems that share only a name:

- **Client** — our agents call MCP servers someone else runs. This is the
  direction chosen here.
- **Server** — bestteam exposes a deployed team as an MCP tool, so a customer
  reaches it from Claude, ChatGPT or Copilot instead of from our UI. Not
  examined. It is worth its own session: it touches authentication and a
  public API surface, and touches the agent tool layer not at all.

The client direction splits again, by **whose credentials the connection
carries** — which is the whole of the trust question:

| | What it is | Credential | Isolation work |
|---|---|---|---|
| **A1** | Public services, same answer for every customer (holidays, weather, maps, public registries) | Ours, or none | None |
| **A2** | A customer's own system, but an operator fills the form | Theirs, held by us | All of it |
| **B** | A customer's own system, connected self-service | Theirs, held by us | All of it, plus the UI |

**A2 and B differ only by who types the credentials in.** The platform work
is identical. So "B asks too much of the customer" is solved by A2, not by
avoiding B: the customer-facing burden disappears, ours does not shrink.

Stated goal in this session: B eventually, A1 first.

## Why not now

**The two candidate first boxes do not justify the subsystem.**

- *Maps / local business* is already a native tool — `local_business_search`
  (Google Places Text Search, `src/bestteam/tools/google_places.py`). An MCP
  server for it adds no capability.
- *Public holidays* is a small read-only API. As a native tool it is roughly
  forty lines and inherits everything: `REGISTRY` resolution, the wizard's
  tool catalog, deploy validation, the existing test shape. (Note for whoever
  writes it: Australian public holidays are per-state, which is the part that
  matters for scheduling a trade.)

**What MCP is actually worth paying for** is a box of one of two kinds:

1. **Deep** — dozens to hundreds of operations, an API that moves, a vendor
   who maintains the server so we do not (CRM, accounting, job management).
2. **The customer's own system** — i.e. A2/B, where the value is that we
   never write an integration per customer at all.

Neither candidate is either kind.

**A too-simple first box is worse than no box.** A holidays server needs no
credentials, holds no session state and exposes one or two tools. Building
the client against it would fix a shape that has not met the hard cases, and
the shape is the expensive thing to get wrong.

## When to reopen

Any one of these is enough:

- A box of the deep kind appears in a real customer conversation — something
  we would otherwise write and then maintain against someone else's API.
- We commit to A2/B, i.e. connecting a specific customer's own system.
- A vendor we need ships an MCP server and no reasonable plain API.

Before any of those, this does not enter `DECISIONS.md` and does not enter
code.

## The first step, whenever it comes: tools declare their own capability

⚠️ **This lands with the first MCP tool or before it. Never after.**

`deploy_validation.EGRESS_TOOL_NAMES` is a closed set of two literal names,
`{"http_get", "web_search"}`. `find_email_egress_conflicts` intersects each
agent's tool names against it and refuses a pipeline that pairs a mailbox with
an egress tool anywhere (`docs/DECISIONS.md`, "Email and egress tools are
refused per PIPELINE") — because mail is attacker-controlled input and an
injected instruction reaches the egress agent's prompt as ordinary text.

A name-based allowlist answers "is this one of the two tools we know about",
not "can this tool send data out". An MCP tool is by construction a name
nobody listed. It would pass the check silently.

**Today there is no hole**, and the reason is worth recording so nobody
"fixes" it twice: `core/loader.py:171` raises `Unknown tool '<name>'` for any
name outside `REGISTRY` plus the pipeline's knowledge-base tools, so no
deployed pipeline can hold a tool the check has not heard of. The hole opens
the moment a second source of tool names exists. MCP is exactly that.

**The inversion**: every tool declares what it can do, and an undeclared tool
is treated as egress. The attachment point already exists —
`__bestteam_tool_kind__`, the marker attribute carrying `"knowledge_base"`
and `"delegate"` today, read by `_has_knowledge_base_tool` and the adapter's
trace branches. A capability declaration belongs on the same attribute or
beside it, so there is one place a tool says what it is.

Not designed here: whether the declaration is a single kind or a set of
capabilities, and who writes it for a tool discovered at runtime from a
server. Both depend on the first real box.

## Constraints already checked against the code

Collected so the next session does not re-derive them.

**Tool calls are synchronous.** `adapters/langgraph_adapter.py:820` executes
`result = tool_fn(**call["args"])` — a plain callable, keyword arguments, in
the calling thread. MCP tools are async and live on a session. That needs a
sync wrapper *and* an answer for the session's lifetime: per run is simple and
pays connection setup every run; process-wide is cheaper and becomes shared
mutable state across orgs, which is the thing this codebase has been careful
about. Note also that a manager's delegations run on a `ThreadPoolExecutor`,
so the wrapper must be safe to call from several threads at once.

**There is no generic custom-tool registry in the UI layer.** `REGISTRY` is a
literal dict in `src/bestteam/tools/__init__.py`, and three call sites read it
directly to build catalogs (`ui/backend/builder.py:217`,
`ui/backend/crud.py:119`, plus `ui/backend/knowledge_bases.py` for collision
checks). Knowledge bases are the only by-name tool source that was ever added
alongside it, and they were special-cased at each site rather than generalised.
MCP would be the second — the point at which extracting a real registry is
justified by two implementations rather than invented ahead of one, which is
the standard this repo already applied to `MailboxConnector`
(`docs/DECISIONS.md`, "OAuth over IMAP … not a Graph connector").

**Adding one tool means touching six places, and five fail silently.** The
table is in `src/bestteam/tools/CLAUDE.md` ("Six enumerations must all learn a
new email tool's name"). An MCP server contributes tools in batches and at
runtime, so "enumerate the new name in six places" does not survive contact
with it. Whatever replaces it has to be derived from the declaration above,
not maintained by hand.

**External results already flow into the customer's trace.** A tool that is
neither an email tool nor a knowledge-base tool takes the generic branch:
`_summarize(result)`, 200 characters, into `trace_events`, `runs.output` and
the live WebSocket broadcast. So a box's return value reaches the database and
the customer's screen by default. Decide per capability, not per tool
instance, whether that stays true.

**Data flows outward even in A1.** A public box still receives whatever the
agent sends it to do its job, which on a triage team is customer mail content.
"No credentials" is not "no exposure" — picking a box includes reading what it
does with what we send.

## The B groundwork already exists

Per-org credentials, encrypted at rest, fetched per run: `OrgEmailCredential`
(`ui/backend/db/models.py:400`), Fernet tokens via `secret_store`, one row per
org, read on the run path in `ui/backend/email_tools.py`. A2/B needs the same
shape for a different secret. **Nothing in this document is on B's critical
path** — not building the client now does not make B later any harder.

## Rejected here, so it is not re-proposed

- **Abstracting the tool layer now, for symmetry.** One implementation does
  not determine an interface; the repo has ruled this way once already.
- **Building the client against a trivial box to "have it ready".** The shape
  would be fixed by the easiest case and would have to be redone by the first
  hard one.
- **Treating A1 as a prerequisite for B.** It is not. They share no
  infrastructure that A1 would build first.

## Left open

- Transport: a local process we spawn on the VPS (stdio) versus a remote HTTPS
  endpoint. Different operational surfaces entirely — the first is software we
  now run and update, the second is egress plus availability.
- A server exposing thirty tools: which of them a customer's agent gets, and
  who chooses.
- Failure behaviour when a box is down or slow, in a run the customer is
  watching.
- Whether an MCP-derived tool may sit in a pipeline with mailbox tools at all,
  which is the capability declaration's first real question.
