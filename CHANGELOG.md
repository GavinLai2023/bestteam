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

[Unreleased]: https://github.com/GavinLai2023/bestteam/compare/v0.1.0-beta.1...HEAD
[0.1.0b1]: https://github.com/GavinLai2023/bestteam/releases/tag/v0.1.0-beta.1
