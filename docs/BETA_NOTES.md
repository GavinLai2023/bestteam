# bestteam beta — what to expect

*One page for beta customers and the operator running their instance. Every
item here is a known, deliberate limit of the beta build — not a bug to
report — and each links to where it is decided. Last revised 2026-09-02.*

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
- **Microsoft 365 mailboxes are supported** — the connection was verified
  against a live tenant on 2026-08-31. If yours is on M365, your IT grants the
  app permission once (the operator sends the steps; that is usually the slow
  part), and the operator walks the connection with you before go-live
  (`docs/email-smoke-test.md` §9).

## Your documents ("My documents" / knowledge bases)

- **Formats**: `.pdf`, `.docx`, `.xlsx`/`.xlsm`, `.csv`, `.xml`, `.txt`,
  `.md`, `.json`, `.yaml`/`.yml`, `.log`. Chinese and English work; Japanese
  and Korean text is indexed too. Plain-text files are read as UTF-8 or
  GB18030, so a document saved from Chinese Notepad or Excel is fine.
- **Limits**: 10 files per upload, 10 MB per file, and **30 documents per
  collection**. Ask your operator if you need more — the caps bound indexing
  cost, they are not a technical limit.
- **Uploading again adds or replaces — you choose.** Re-using a collection
  name asks whether to add to what is there or replace it. Documents whose
  contents have not changed are not re-indexed, so adding one file to a
  collection of twenty costs one file's work. The old version keeps serving
  until the new one is ready.
- **There is no way to remove a single document** from a collection. To drop
  one, upload the ones you want to keep and choose "replace everything".
- Indexing is asynchronous: a collection shows *processing* and then either
  *ready* or a list of files that failed with a reason. "Try a search" shows
  you exactly what an agent would retrieve before you hand the collection to a
  team.
- **Scanned images are not read** (there is no OCR), a **PDF's tables** come
  out as reading-order text rather than rows and columns, and **`.pptx` is not
  supported at all**. If your answers live in any of those, say so — it
  changes what we can promise.

## The interface

- **English and Chinese**, switchable from the top bar at any time; the choice
  sticks in that browser. English is the default. The whole customer-facing
  app is translated, including the team-building wizard.
- **Light and dark** follow your operating system's setting. There is no
  in-app toggle in the beta.
- The Chinese and dark-mode layouts were walked page by page before this build
  was named, but far less has run over them than over the English light-mode
  ones. A layout that looks wrong is worth reporting, not working around.

## Cost, usage and limits you control

- Every model call is metered per organisation; the Activity page shows spend
  as an **estimate** from the operator's price list (embedding token counts are
  ±30 %). Models the operator has not priced show as unpriced, not as free.
- You can set a **daily message cap** for automation on your email team's own
  page (open it from My teams), below where the mailbox is connected; reaching
  it pauses automatic runs until tomorrow and tells you once. A monthly spend
  cap is enforced too, but it has no setting in the product — ask the operator
  if you want one.

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
- **If you report a bad answer, your operator can re-run it.** A diagnostic
  re-run repeats one of your runs with the same input and records what the
  team was actually asked and what it replied at each step, which is what
  makes a vague "it got this wrong" fixable. It is a **new** run — your
  original is left untouched — it costs the usual model spend, and it is not
  offered for automatic email runs, which would risk a second draft in your
  mailbox. Only your operator can start one.

## Reporting a problem

Send the operator the time, the page, and (for a run) the run id from the
Activity page. That is what the logs are keyed on.
