# Changelog

Notable changes to **bestteam**, newest first.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/), with Python
package versions written in [PEP 440](https://peps.python.org/pep-0440/) form
(`0.1.0b1`) and git tags in the SemVer form (`v0.1.0-beta.1`).

> **On history before this file.** `docs/STATUS.md` was the running record of
> completed work up to this release and stays the reference for *why* each
> piece is the way it is. This file starts here and is the customer- and
> operator-facing view from now on: what changed, and what an operator has to
> do about it. Splitting the two properly is Stage 1 work, not a beta blocker.

## [Unreleased]

### Security

- **A customer's team can no longer carry `parse_file`.** The tool reads any
  path on the server with no sandbox, and organisation isolation covers
  database rows, not the container's disk. Deploying an org team with it --
  from the wizard or the admin config page -- is now refused with a message
  pointing at knowledge bases, and the Solution Architect is no longer shown
  the name. Nothing already deployed is affected; the SDK and YAML pipelines
  keep the tool. No operator action.

## [0.1.0b3] — 2026-08-31

Third beta build. Named from a green `main` (`ee88a5a`) — the `backend-full`
and `e2e-full` suites that run only on `main` both passed.

Upgrading from `0.1.0b2`: run `alembic upgrade head` (three migrations,
`t7u8v9w0x1y2`, `u8v9w0x1y2z3`, `v9w0x1y2z3a4`). Nothing is backfilled and the
upgrade changes no existing behaviour — the new pause switch defaults to *not*
paused, and the new grounding policy defaults to the 0.1.0b2 one. Two
knowledge-base variables are worth setting before the first customer:
`BESTTEAM_KB_DEFAULT_EMBEDDING_MODEL` (without it, document search is
keyword-only) and `BESTTEAM_KB_DEFAULT_RERANK_MODEL`; `check-env` now names
both. `docs/BETA_NOTES.md` is still the one page to hand a beta customer.

### Added

- **The platform interviews the customer** — a sixth wizard step between
  "your challenge" and "your documents": up to four clarifying questions the
  analyst judges would most change the team's design, one answer box each.
  Answering is optional — **Skip these questions** instead records the
  assumption the analyst made, prefixed `Assumed:`, where the customer can
  read and correct it. The Confirm page asks the still-open ones again, and
  those answers ride that page's one "Update the team" action.
- **Pause a live team** — My Teams gains a reversible off switch for a
  deployed team. Previously the only lifecycle verb was deleting a
  never-deployed draft, and the only "off" in the product suspended the whole
  organisation. A paused team is refused at all five entry points: Run a
  team, `POST /api/runs`, automatic runs, turning automatic runs on, and the
  anonymous share chat. Pausing switches automatic runs off only when the
  trigger names that team; resuming deliberately does not switch them back
  on. Migration `v9w0x1y2z3a4`, `server_default="1"` — an upgrade pauses
  nothing. Deleting a live team stays deferred.
- **Report a defect or make a suggestion from inside the product** — a
  Feedback item in the nav for members and in the share-chat header for
  anonymous visitors (five per visitor per UTC day), and a fifth admin page
  that collects them with a status lifecycle and a private operator note.
  Bodies render as plain text only. Migration `u8v9w0x1y2z3`.
- **Retry a failed upload** — "My documents" re-runs a failed or interrupted
  ingestion over the files the server already holds, rather than asking the
  customer to upload documents it still has. Nothing is re-metered: a failed
  attempt was never billed, and unchanged documents still reuse their
  existing chunks and embeddings.
- **Grounding can refuse, per agent** — `grounding_policy: observe | retry |
  refuse` on an agent that has a knowledge base. `observe` stays the default
  and is byte-identical to 0.1.0b2 (record only); `retry` gives the agent one
  more turn to cite what it actually retrieved; `refuse` replaces an
  ungrounded answer. Opt-in per agent — nothing changes until it is set.
- **Health metrics an outside watcher can read** — `python -m
  ui.backend.admin check-health` reports poll lag, oldest unprocessed
  message, 24-hour done/failed counts and detection-to-draft latency per
  organisation, exiting 1 on a fault so cron can page on it. It is
  deliberately the *only* watcher for a stalled poller: in-app alerts are
  delivered **by** the poll loop, so one about the loop being wedged could
  never leave the process. `docs/deployment.md` ("Watching the watcher") says
  how to schedule it.
- **A backlog alert for mail nothing is failing on** — raised once when the
  oldest unprocessed message outlives `BESTTEAM_BACKLOG_ALERT_MINUTES`
  (default 30), and cleared once when it drains. Covers the case where a
  budget cap paused dispatch and no run ever *failed*, so no existing alert
  would fire.
- **A second process refuses to start** — the backend takes an exclusive lock
  on the database file before its startup sweeps, so `uvicorn --workers N` or
  a second replica fails with a clear error instead of two pollers racing
  over one mailbox and releasing each other's claims. The OS drops the lock
  on any exit, so a crash never blocks the next start.
- **`check-env` covers more** — organisations that no retention policy covers
  (they keep run history forever), an unset knowledge-base embedding default
  (WARN: customers silently get keyword-only search), a `fake:` one (FAIL),
  and a missing rerank default once embedding is actually on.
- **`docs/VPS_SETUP_RUNBOOK.md`** — a step-by-step Chinese runbook for
  standing this up on a DigitalOcean box, from a bare server to HTTPS.

### Changed

- **Language, password and log out became one account menu** under the
  username, instead of a form control and two buttons sitting in the nav row.
  Feedback stays in the row: it is an invitation, and an invitation behind a
  menu is not one.
- **Retrieval quality now has release gates that run against real models** —
  a hand-run suite over two golden sets (one of them deliberately hard:
  answers in table cells, facts buried late in long handbooks,
  near-identical sibling documents, and Chinese queries against
  English-only documents) under a real embedding model and a real
  cross-encoder reranker. It needs `BESTTEAM_LIVE_EVAL=1` and an API key, so
  CI stays free and no ordinary test run pays for inference.
- All seven `CLAUDE.md` files — what a coding agent reading this repository
  loads before it starts — were cut to invariants only, 315 KB down to
  205 KB.

### Fixed

- **A team that reads the mailbox could be shared anonymously** — Share
  rendered on every deployed team, including one holding email tools, so
  whoever held the link could ask that team to read the organisation's inbox
  back to them. Minting such a link is refused, and so is every link already
  minted, with the same 404 an unusable link gets — a distinguishable message
  would itself tell a prober what the team can reach.
- **A spreadsheet or Word-table cell containing a comma or a line break
  shifted every column after it** — `.xlsx`/`.xlsm` and `.docx` tables now
  quote cells the way the `.csv` path already did, so the same table reads
  the same whichever format it arrives in.
- **A workbook that would exhaust the server is refused with a readable
  reason** — over 300 MB unpacked (a decompression bomb) or over five
  million declared cells (one stray formatted cell far down a sheet). A byte
  stream that is not a workbook at all now says so instead of raising a
  zip error.
- **A citation was matched by parsing its label** — a document whose filename
  contained `, p.` or ` § ` was misread by the grounding check, which now
  compares against what the search tool actually reported, and a heading
  containing `[` or `]` no longer truncates its own citation tag. Latent
  while grounding only observed; load-bearing now that `refuse` can act on
  the result.
- **`check-health` paged for deliberately suspended organisations** — it
  filtered on the trigger being enabled but not on the organisation being
  active, so a suspended org's frozen poll timestamp reported a stalled
  poller three intervals later. Its exit code is the pager, so this was a
  false page rather than merely wrong text.
- **`check-health`'s 24-hour window loaded an organisation's whole terminal
  history** and filtered in Python; the bound is now in SQL, with index
  `t7u8v9w0x1y2`.
- **`docs/email-smoke-test.md`** still described ambient triggering as
  planned rather than built.

## [0.1.0b2] — 2026-08-24

Second beta build. Named from a green `main` (`9afba75`) — all seven CI jobs,
including the `backend-full` and `e2e-full` suites that run only on `main`.

Upgrading from `0.1.0b1`: run `alembic upgrade head` (one migration,
`s6t7u8v9w0x1`). Nothing is backfilled and no configuration changes.
`docs/BETA_NOTES.md` is still the one page to hand a beta customer.

### Added

- **A knowledge-base agent searches before it answers, and its citations
  are checked** — an agent that has a knowledge base is now made to use a
  tool on its first model call (as a team manager already was), and each of
  its turns ends with a `grounding_checked` trace event: how many
  `[source: …]` tags the answer carries, how many name a passage the agent
  actually retrieved, and which do not. Nothing is retried or refused; the
  event is there to be read. No configuration, no migration.
- **Restore the previous upload** — "Restore previous upload" on "My
  documents" (`POST /api/org/knowledge-bases/{name}/restore`) makes the
  upload before the current one live again, reusing every chunk and embedding
  (nothing re-embedded or billed). One upload back; restoring again undoes it.
- **Search evidence in a run's trace keeps resolving** — a knowledge-base
  generation that some run's trace references is no longer deleted when newer
  uploads push it out of the keep window: its text rows are kept (vectors
  and files are not) until the run's content is purged by retention.
  Migration `s6t7u8v9w0x1` adds `run_knowledge_generations`; run
  `alembic upgrade head`. Nothing is backfilled.
- **Remove one document from a collection** — each row of "My documents"
  lists its documents (name, size, whether it could be read) with a Remove
  per document; `DELETE /api/org/knowledge-bases/{name}/documents/{filename}`
  builds a new generation from the live one minus that file, reusing every
  other document's chunks and embeddings (nothing re-embedded or billed).
  Refused while an upload is processing and for the last document (delete
  the collection instead). Dropping one document no longer means a `replace`
  upload of everything to keep.
- **Retrieval hits carry their scores and identity** — `search_hits()`
  returns each chunk with its RRF score, each retrieval leg's raw score
  (which leg found it) and the rerank score; chunks from an uploaded
  collection carry `chunk_id` / `document_id` / `ingestion_job_id`. A run's
  knowledge-base `tool_completed` event now records the **ingestion job the
  collection was built from** and a bounded per-hit list of ids and scores
  (never text), so a trace says which generation of a collection answered
  and why each hit ranked where it did. "Try a search" returns the same.

### Changed

- `KnowledgeBase.search_hits()` is the abstract method subclasses implement;
  `search()` and `query()` derive from it. A custom knowledge base exposing
  only `search()` still works as an agent tool (its trace reports no scores).

### Fixed

- `AGENTS.md` was a stale fork of `CLAUDE.md` and `docs/KNOWLEDGE_BASES.md`
  contradicted itself about re-embedding on document changes; both corrected.

## [0.1.0b1] — 2026-08-22

First beta build. Named from a green `main` — all six CI jobs, including the
`backend-full` and `e2e-full` suites that run only on `main`.

Hand every beta customer `docs/BETA_NOTES.md`; it is the one page describing
the deliberate limits of this build. Operators should read
`docs/deployment.md` §0 before the first customer is provisioned.

### Added

- **Team Builder wizard** — a five-stage guided flow (your challenge → your
  documents → meet your team → confirm → go live) that turns a non-technical
  description of a problem into a deployed multi-agent team. Includes an
  optional interview-recording upload that is transcribed and used to
  pre-fill the first stage.
- **Email automation, draft-only** — a customer connects one mailbox, and a
  deployed team runs automatically on new mail, leaving reply *drafts* for a
  human to review. There is no send verb anywhere in the product. Microsoft
  365 (OAuth over IMAP) and generic IMAP are both supported.
- **Attachment reading**, as text only: `.pdf`, `.docx`, `.xlsx`/`.xlsm`,
  `.xml` and plain text, bounded at 10 MB per attachment, 25 MB per message
  and 8,000 characters of extracted text.
- **Inbound mail filtering** — a sender allow/block list, a subject block
  list, and a bulk-mail check over the standard list headers, evaluated
  before any model is involved. Nothing is deleted: a filtered message is
  recorded and can be released with one click.
- **Per-org budgets** — a daily message cap and a monthly spend cap that
  pause automation, alert once, and resume automatically at the period roll.
- **Knowledge bases** ("My documents") — BM25, vector and hybrid retrieval
  with opt-in query expansion and reranking, plus a "Try a search" panel that
  shows what an agent would retrieve before a team is built on the documents.
- **Run history and monitoring** — every run's trace persisted and browsable,
  filterable by team, trigger and status, with cooperative cancellation.
- **Retention controls** — a per-org run-history retention period, a JSON
  export, and an immediate purge. A purge clears run *content* and keeps the
  accounting, so history cannot be used to hide spend.
- **Alerts** — in-app notifications plus one optional webhook per
  organisation, raised on a health transition rather than per occurrence.
- **Admin surfaces** — Accounts (orgs and members), Advanced (raw config),
  Memory, and Trace, including a diagnostic re-run that repeats a run with
  full prompt/tool visibility.
- **Bilingual interface** — English and Chinese, switchable at any time,
  English by default; light and dark follow the operating system.
- **Operator tooling** — `python -m ui.backend.admin` for orgs, users,
  mailboxes and an environment checklist (`check-env`), Docker Compose
  packaging, and backup/restore scripts.

### Known limitations

These are deliberate and documented, not defects. The full list with
reasoning is in `docs/STATUS.md`; the customer-facing subset is in
`docs/BETA_NOTES.md`. The ones that most often surprise:

- **One member per organisation.** A second person needs a second
  organisation. Lifting this needs per-org roles, not a bigger database.
- **Single process, one SQLite file.** `uvicorn --workers N` and multi-host
  replicas are unsupported, and at most four things run at once across the
  whole server. See `docs/DECISIONS.md`, "Beta runs single-process on
  SQLite", for the upgrade triggers.
- **Microsoft 365 has never been verified against a live tenant.** Every test
  for it runs against fakes. Run `docs/email-smoke-test.md` §9 with the
  customer before go-live if their mailbox is on M365.
- **Spend figures are estimates**, derived from an operator-maintained price
  list that no provider invoice is reconciled against. Quote them as "at
  least".
- **No OCR**, no PDF table structure, no `.pptx`.
- **Erasure by data subject is not offered** — retention bounds history by
  age instead. `docs/BETA_NOTES.md` says this to the customer plainly.
- **Live run state is lost on restart.** History survives; a run that was
  in flight is swept to `failed` at startup rather than showing as running
  forever.

[Unreleased]: https://github.com/GavinLai2023/bestteam/compare/v0.1.0-beta.3...HEAD
[0.1.0b3]: https://github.com/GavinLai2023/bestteam/releases/tag/v0.1.0-beta.3
[0.1.0b2]: https://github.com/GavinLai2023/bestteam/releases/tag/v0.1.0-beta.2
[0.1.0b1]: https://github.com/GavinLai2023/bestteam/releases/tag/v0.1.0-beta.1
