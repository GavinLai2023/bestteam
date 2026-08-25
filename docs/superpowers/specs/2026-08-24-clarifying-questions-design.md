# Clarifying questions — the platform interviews the customer

**Date:** 2026-08-24
**Status:** approved (decisions delegated; the shape below was fixed by the
2026-08-23 ruling recorded in `docs/STATUS.md`'s roadmap)

## Problem

`Requirements.clarifying_questions` exists end to end but is inert in four
ways: the Business Analyst prompt only asks when the description is "too
vague" (1-2 questions max); `IntentPage` jumps straight to Documents, so the
questions first surface on Confirm *after* the team is designed; the Confirm
banner is read-only, so answers arrive as free text with no link to the
question they answer; and each regeneration overwrites the list, so an
unanswered question can vanish or be re-asked forever.

## Fixed shape (2026-08-23 ruling — not up for redesign)

- A **batch** of questions, **one input box each**, answers stored **paired
  with their question**, the batch **skippable as a whole**.
- Asked **after Intent, before the team is designed**, and **again on the
  Confirm page when a change is requested**.
- The Solution Architect does **not** ask (that would need a `Specification`
  schema change — rejected as the largest piece).
- Governing constraint: the platform promises "intent in, best AI team out",
  so every question is work pushed back onto the customer. Skipping must
  always be possible, and a skipped question must make the analyst **write
  the assumption it made instead into `constraints`**, where the customer can
  see it.

## Design

### 1. Data model — no schema change, answers paired in two places

`Requirements.clarifying_questions` stays `List[str]`: it is the analyst's
*output* (what to ask). Answers are customer input and live elsewhere:

- **SDK**: `core/requirements.py` gains
  `class QuestionAnswer(BaseModel): question: str; answer: str = ""` and
  `generate_requirements(..., answers: Optional[Sequence[QuestionAnswer]] = None)`.
  When given, the human message gains a block *after* `current` and *before*
  `feedback`:

  ```
  The customer was asked these clarifying questions:
  Q: <question>
  A: <answer>
  ```

  A blank answer renders as
  `A: (not answered — make your best assumption, add it to constraints
  prefixed "Assumed:", and remove the question)`. So one rendering carries
  the whole skip ruling; there is no separate "skip mode" parameter.
- **Backend**: each answer round appends one `feedback_history` entry
  `{stage: "clarifying", answers: [{question, answer}], skipped: bool, at}` —
  the durable paired record (`feedback_history` is already a schemaless JSON
  column; no migration).

Existing stored sessions validate unchanged — nothing about the
`Requirements` shape moves.

### 2. Analyst prompt (`_ANALYST_SYSTEM_PROMPT`)

Two changes:

- **Asking policy** replaces the "too vague → 1-2 questions" rule: list up to
  4 short questions whose answers would most change what team should be
  built (volumes, tools, languages, tone, approval steps…). Every question
  costs the customer effort — ask only what genuinely matters, and leave the
  list empty if the description already covers it.
- **Answer folding contract**: when shown questions with answers, treat each
  answer as the customer's own words and fold it into the requirements,
  removing that question from `clarifying_questions`. Where an answer is
  marked "(not answered)", follow its instruction (assumption into
  `constraints` prefixed `Assumed:`, question removed). Never re-ask a
  question that has been answered or assumed; keep a question only while it
  is genuinely still open.

Combined with passing `current=` (which the answers path always does), this
closes the "each regeneration overwrites the list" defect: open questions
persist, resolved ones are retired.

### 3. Backend API

**`POST /{session_id}/requirements`** (`RequirementsRequest`) gains
`answers: Optional[List[QuestionAnswer]]`:

- `answers` requires `model` (the analyst runs) and a stored
  `requirements_json` (400 otherwise); combining `answers` with a literal
  `requirements` payload is a 400.
- The analyst is called with `current=` the stored requirements and
  `answers=` the submitted pairs (blanks included — a blank here *is* the
  skip, per the ruling). One `feedback_history` "clarifying" entry is
  appended (`skipped` = every answer blank). Result stored as usual.

**`POST /{session_id}/refine`** (`RefineRequest`) gains the same optional
`answers`. Blank answers are **filtered out** before the analyst sees them:
on Confirm there is no skip button — an unanswered question simply stays
open (it is in `current`, and the prompt keeps open questions). The analyst
now runs when `feedback` is non-empty **or** any non-blank answer exists;
the architect always runs, as today. A "clarifying" history entry is
appended only when non-blank answers were sent. Nothing else about
`/refine`'s single-transaction, nothing-persisted-until-both-stages-succeed
behaviour changes.

Why answers ride `/refine` instead of a second call: the Confirm page's one
hard-won rule (2026-08-23) is **one action** — a separate "submit answers"
endpoint would recreate exactly the understanding-ahead-of-the-team split
that `/refine` was built to remove.

### 4. Wizard flow — a sixth step

`WizardProgress.STEPS` becomes
`['intent', 'questions', 'documents', 'preview', 'confirm', 'deploy']`, with
`questions` unlocked by `Boolean(session)` (same as documents). New route
`/wizard/:sessionId/questions` → `QuestionsPage`.

- **IntentPage** already awaits `submitRequirements`; it now uses the
  result: questions present → navigate to `questions`, none (or the
  requirements call failed — the existing best-effort contract) →
  `documents` as today.
- **QuestionsPage**: renders each `clarifying_questions` entry with a
  2-row textarea; a hint says a blank question gets a sensible assumption
  the customer will see in the summary. Two actions, mirroring
  DocumentsPage: **Skip these questions** (secondary, always enabled, sends
  every answer blank) and **Continue** (primary, enabled once at least one
  answer is non-blank). Both call the requirements endpoint with the full
  paired batch, then navigate to `documents`. While in flight the page
  disables itself and raises `setNavBusy` (the Confirm-page rule: an
  analyst call is long enough for the customer to wander). Revisiting when
  no questions remain shows a short "no open questions" card with Continue.
- **ConfirmPage**: the read-only questions banner becomes the same
  question-with-input list (answers drafted in local state, keyed off
  `requirements_json` so a refresh resets them). The answers travel with
  the page's **one existing action** — `updateTeam` adds
  `answers: <non-blank pairs>` to the `/refine` payload when any exist. No
  new buttons.

### 5. Deterministic E2E support (`fake-architect:`)

`_fake_architect_requirements` today returns `clarifying_questions=[]`, so
every existing wizard E2E flows exactly as before (IntentPage goes straight
to Documents). Two additions, both message-driven and stateless:

- An intent containing the literal marker **`[interview me]`** makes the
  canned Requirements carry two fixed questions.
- A prompt containing the answers block ("The customer was asked these
  clarifying questions") returns the folded shape: no questions; each
  answered pair appends `The customer clarified: <answer>` to
  `constraints`; any "(not answered" line appends a fixed
  `Assumed: replies can go out within one business day.` constraint.

`_FakeArchitectStructuredResult` therefore inspects its invoke messages for
the Requirements schema (it stays canned for `Specification`). One new E2E
test drives the marker flow: intent with marker → questions page → answer
one, leave one blank → Continue → Documents; the Confirm page's constraints
show both the folded answer and the `Assumed:` line.

### 6. Out of scope (deliberate)

- Architect-stage questions (`Specification` schema change) — rejected in
  the ruling.
- A deploy-time gate or warning on open questions.
- Rendering "clarifying" `feedback_history` entries anywhere in the UI
  (ConfirmPage's history filter stays `stage === 'solution'`).
- Any change to the plain `submit_requirements` generate path (no
  `current=` there — its one caller generates from scratch by design).

## Error handling

- Answers path validation failures are plain 400s with actionable text.
- Analyst/model failures surface through the existing `_call_model`
  400/502 translation; QuestionsPage shows the banner-plus-retry pattern
  every other wizard page uses. A customer can always leave via Skip →
  actually via the nav (Skip itself needs the model); if the analyst is
  down, the step's error banner offers retry and the step bar (not busy
  once the request fails) still allows moving on to Documents — the same
  degradation IntentPage's best-effort contract established.

## Testing

- **SDK** (`tests/test_requirements.py`): answers block rendering (order:
  current → answers → feedback), blank-answer instruction text,
  `QuestionAnswer` defaults; existing tests untouched.
- **Backend** (`tests/test_builder_api.py`): answers-path validations (no
  stored requirements → 400, `answers`+`requirements` → 400, `answers`
  without `model` → 400); happy path stores regenerated requirements and
  appends a paired, `skipped`-flagged history entry; `/refine` runs the
  analyst on answers-only input, filters blanks, and appends history only
  when non-blank answers exist.
- **Fake architect** (`tests/test_fake_architect_model.py`): marker
  triggers questions; answers block folds; default stays question-free.
- **Frontend** (vitest): `QuestionsPage.test.tsx` (render, skip, continue
  gating, error banner, no-questions card), `ConfirmPage.test.tsx`
  (inputs render, non-blank answers ride `refineTeam`, blanks filtered),
  `WizardProgress` six steps, `IntentPage` navigation fork.
- **E2E** (`tests/e2e/test_wizard_full.py`): one new marker-driven test;
  all six existing tests must stay green (run the full tier locally —
  e2e-smoke deselects wizard tests).
