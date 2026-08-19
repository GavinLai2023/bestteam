# bestteam beta — what to expect

*One page for beta customers and the operator running their instance. Every
item here is a known, deliberate limit of the beta build — not a bug to
report — and each links to where it is decided. Last revised 2026-08-19.*

## Your account and organisation

- **One organisation, one member, one mailbox, one automated team.** Your
  organisation has exactly one login; a second person needs a second
  organisation. One mailbox can be connected and one team can run
  automatically on it. (`docs/DECISIONS.md`, "one member per org";
  `docs/deployment.md` §4.)
- **Accounts are created by the operator**, never self-registered, and a
  forgotten password is reset by the operator (Accounts page) — there is no
  self-service reset in the beta.
- **Five wrong passwords in 15 minutes lock the username for the rest of the
  window** (20 per network address). Wait it out or ask the operator; there is
  no unlock command because the lock is that short.

## What the automation does — and does not — do

- **It drafts; it never sends.** An automated team reads mail and leaves
  reply *drafts* in your mailbox for you to review and send. There is no send
  action in the product at all. Attachments are read as text only (PDF, Word,
  Excel, XML, plain text; 10 MB each) — a photographed invoice or a zip is
  invisible to it.
- **It checks the mailbox every ~2 minutes and handles up to 20 messages per
  run, at most 50 automatic runs per day per organisation** unless the operator
  raises the caps. Bulk mail (list headers, auto-replies) is skipped by default;
  you can block or allow senders and subjects on the Activity page's
  Automations tab.
- **At most four things run at once on the whole server** (automatic runs,
  manual runs and share-link chats share the pool). A fifth waits — this is
  fine for a beta and is the first thing that changes after it.
- **Microsoft 365 mailboxes are supported but were not yet verified against a
  live tenant** at the time of writing. If yours is on M365, the operator runs
  the smoke test in `docs/email-smoke-test.md` §9 with you before go-live.

## Your documents ("My documents" / knowledge bases)

- Upload PDF, Word, Excel, XML or plain-text files; tens to a few hundred
  documents per collection is the tested range. Chinese and English work;
  Japanese and Korean text is indexed too. Scanned images are not read (no OCR).
- Indexing is asynchronous: a collection shows *processing* and then either
  *ready* or a list of files that failed with a reason. "Try a search" shows
  you exactly what an agent would retrieve before you hand the collection to a
  team.
- Uploading again with the same collection name **replaces** its contents; the
  old version keeps serving until the new one is ready.

## Cost, usage and limits you control

- Every model call is metered per organisation; the Activity page shows spend
  as an **estimate** from the operator's price list (embedding token counts are
  ±30 %). Models the operator has not priced show as unpriced, not as free.
- You can set a monthly spend cap and a daily message cap for automation on
  the Activity page's Automations tab; hitting one pauses automatic runs until
  the period rolls over and tells you once.

## Your data

- Everything lives on your instance's server (a single database, backed up
  nightly by the operator). Nothing is shared between organisations.
- **Run history is kept forever by default.** You can set a retention period
  on the Activity page's Data tab; a purge removes the content of old runs
  (inputs, outputs, traces, drafts' payloads) but keeps that they happened and
  what they cost. You can export your organisation's data from the same tab.
- **There is no "delete everything about this email address."** The address
  only ever appears inside free text the model may have paraphrased, so an
  exact erasure cannot be promised; retention is the tool for bounding history.
- Per-user memory (the assistant remembering facts about you across runs) is
  **off** in the beta unless the operator enables it.

## What we watch, and what we do not

- The operator sees the instance's logs and, if configured, a report of
  unhandled errors and failed runs — identifiers and names only, never your
  documents, prompts or mail bodies. There is no analytics or telemetry beyond
  that.

## Reporting a problem

Send the operator the time, the page, and (for a run) the run id from the
Activity page. That is what the logs are keyed on.
