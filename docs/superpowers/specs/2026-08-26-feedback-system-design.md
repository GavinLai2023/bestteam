# User feedback: defects and suggestions, from logged-in users and share-link visitors

Date: 2026-08-26. Status: approved design (brainstorming; remaining decisions
delegated to Claude by the user after approving the data model and API
sections).

## Context

The platform has no way for anyone to report a defect or suggest an
improvement from inside the product. The user wants three things, staged:

1. **Feedback entry points** — for logged-in users *and* for anonymous
   visitors who arrived through a share link.
2. **Collection and triage** — the platform operator can see everything in
   one place and work through it.
3. **(Future) a self-closing loop** — the platform categorises feedback
   automatically, drafts an improvement plan, and executes it.

This design delivers 1 and 2 and deliberately leaves 3 unbuilt. The
groundwork 3 needs — a status lifecycle and structured context on every
row — is part of 1+2 anyway; anything more (auto-categorisation fields,
plan linkage) is a later migration.

Decisions taken in brainstorming (first three by the user, rest delegated):

- **All feedback goes to the platform operator**, viewed on an admin-only
  page. It is not org-scoped triage for org admins; `org_id` on a row is
  provenance, not ownership.
- **Triage is manual this phase.** No LLM tagging, no dedup, no
  notifications on new feedback. The admin page filters and sorts; that is
  the whole workflow.
- **Text only.** Type (defect/suggestion) + free text. No screenshots, no
  attachments, no optional contact field.
- Rejected alternatives: reusing `notifications` (alert semantics —
  fingerprint dedup, transition-only raising — not a free-text lifecycle);
  an external form/GitHub issues (loses context, data leaves the platform,
  dead end for phase 3).

## Data model

New table `feedback` (one row per submission), in `db/models.py` with
helpers in a new `db/feedback.py`:

| column | notes |
|---|---|
| `id` | PK |
| `created_at` | naive UTC, like the rest of the schema |
| `org_id` | nullable FK → `organizations`; submitter's org, or the share link's org for visitors; NULL for platform admins |
| `kind` | `defect` \| `suggestion`, CHECK `ck_feedback_kind` |
| `body` | free text, server-enforced ≤ 4000 chars, non-empty after strip |
| `status` | `new` \| `acknowledged` \| `resolved` \| `dismissed`, CHECK `ck_feedback_status`, default `new` |
| `admin_note` | nullable text, written by the operator during triage |
| `submitted_by` | nullable FK → `users`; set for logged-in submitters |
| `share_session_id` | nullable FK → `share_sessions`; set for visitors |
| `context` | JSON dict: client-supplied `page` (route path) and `locale`, plus server-added `share_link_id` for visitor rows; visitors may also carry `run_id` of their latest turn when the client knows it |

Exactly one of `submitted_by` / `share_session_id` is set per row (enforced
in the helpers, not a DB constraint — SQLite CHECK across two FKs buys
little here).

Alembic migration `u8v9w0x1y2z3` (down_revision `t7u8v9w0x1y2`), guarded by
inspection like its neighbours (because `db_session` runs `create_all` at
import, the table may already exist when the migration runs).

No admin delete. Rows are small, text-only, and operator-visible only;
retention/purge is out of scope this phase (org retention covers *runs*,
not feedback — noted as a known limitation).

## API

**`POST /api/feedback`** — any authenticated user (org members and platform
admins). Body `{kind, body, context?}`. Validates kind and body length,
whitelists `context` keys (`page`, `locale`), stamps `org_id` and
`submitted_by` from the session. Returns 201 with the row id.

**`POST /api/share/{token}/feedback`** — the anonymous surface, in
`share_chat.py` beside the other `/api/share/{token}/...` routes. Requires
the same things a message send requires: an active (unrevoked, unexpired)
link resolved via `_resolve_active_link`, and a valid session cookie
resolved via `_resolve_session_from_cookie` — **no session is minted for
feedback alone**; a visitor who never opened the chat has no cookie and
gets 403. Body `{kind, body, context?}`; context whitelist adds `run_id`.
Server stamps `org_id` from the link and `share_session_id` from the
session, and adds `share_link_id` into `context`.

Rate limit: **5 feedback rows per session per UTC day**, a module constant,
checked by counting today's `feedback` rows for the session before insert.
This is a plain count-then-insert (a near-simultaneous pair could reach 6);
unlike chat turns, nothing is billed per submission, so the guarded-UPDATE
machinery `try_consume_turn` needs is not warranted. Over the cap → 429.

**`GET /api/admin/feedback`** — platform admin only (`is_admin` and
`org_id IS NULL`, the same gate as the other admin endpoints). Query params
`status`, `kind`, `org_id`, `limit` (default 100), `offset`; newest first.
Each row is returned with a resolved `org_name`, `username` (or null), and
a `source` discriminator (`user` / `visitor`).

**`PATCH /api/admin/feedback/{id}`** — platform admin only. Body may set
`status` (validated against the four values; no transition rules — the
operator may move a row anywhere) and/or `admin_note`. Unknown id → 404.

The two authenticated endpoints live in a new `ui/backend/feedback_api.py`
(one router), mounted in `main.py`; the share endpoint lives in
`share_chat.py` because it shares that module's link/session/cookie
helpers.

## Frontend

**`FeedbackModal`** (new component, used from both surfaces): a small modal
with a defect/suggestion toggle, a textarea with a 4000-char counter, and a
submit button; success collapses to a brief thank-you state, errors (429
included) show inline. It posts to a URL passed in by the caller, so the
same component serves both audiences. Plain-text everywhere; bilingual via
i18n (English default), British spelling in the English strings.

**Logged-in entry**: a "Feedback" item in `Layout`'s top nav, visible to
every authenticated user (customers and admins), opening the modal with
`page` = current route and `locale` = active language.

**Visitor entry**: a "Feedback" button in `ShareChatPage`'s header, same
modal, posting to `/api/share/{token}/feedback`, additionally passing the
`run_id` of the latest assistant turn when the page knows it.

**Admin triage page**: new `FeedbackPage` at `/feedback`, the fifth
admin-only page, registered exactly like Memory/Trace (cosmetic
`RequireAdmin` route gate; the backend enforces admin regardless). A table
(created, kind, status, org, source, body excerpt) with status and kind
filters, newest first; clicking a row expands it to the full body, the
context dict, a status dropdown, and an admin-note field that saves via
PATCH. **Body and note render as plain text, never markdown** — visitor
text is untrusted.

## Abuse and trust boundaries

- The share endpoint is unreachable without a signed session cookie, which
  only exists after opening a valid link — the same containment as chat.
- Per-session daily cap (5) bounds a hostile visitor to noise, not volume;
  the 4000-char cap bounds row size.
- Feedback text is displayed to the operator as inert plain text. When
  phase 3 puts an LLM in front of this table, that text becomes untrusted
  *model input* and needs the same treatment as inbound email — a problem
  explicitly deferred with phase 3.
- Cross-org concerns don't arise: no org-facing read surface exists.

## Testing

- Backend (pytest, `integration` marker, existing client fixtures):
  - `POST /api/feedback`: happy path both kinds; kind/body validation;
    context whitelist (unknown keys dropped); 401 unauthenticated.
  - Share endpoint: happy path stamps org/session/link; 403 without a
    session cookie; 404 revoked/expired link; 429 at the cap; body cap.
  - Admin endpoints: list filters and ordering; enrichment fields; PATCH
    status/note; 403 for org members; 404 unknown id.
  - Migration covered by the existing `test_migrations.py` pattern.
- Frontend (vitest): `FeedbackModal` (toggle, counter, submit, error and
  thank-you states), `FeedbackPage` (render, filter, expand, PATCH), nav
  entry visibility.
- No new e2e cases; the four local gates (lint, build, test, e2e smoke)
  run before push.

## Documentation to update

- `docs/ADMIN_MANUAL.md` — the admin page count ("four" → "five") and a
  section for the Feedback page.
- Root `CLAUDE.md` (admin pages list), `ui/backend/CLAUDE.md`,
  `ui/backend/db/CLAUDE.md`, `ui/frontend/CLAUDE.md` — the new surfaces.
- `docs/STATUS.md` — done column + the phase-3 item under next steps.

## Explicitly not in this phase

LLM categorisation/dedup, notifications on new feedback, screenshots or
attachments, contact fields, org-admin visibility, feedback retention or
erasure, admin delete, and everything in phase 3 (auto-triage, improvement
plans, self-execution).
