# Clarifying Questions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Activate the analyst's clarifying questions: a skippable batch step
after Intent (answers paired with questions; a skip makes the analyst record
its assumptions into `constraints`), and answerable questions on the Confirm
page riding its one "Update the team" action.

**Architecture:** No `Requirements` schema change. The SDK's
`generate_requirements` gains an `answers` parameter whose rendering carries
the skip ruling ("blank answer → assume, record `Assumed:` in constraints,
retire the question"); the backend threads it through the existing
requirements and refine endpoints and stores paired answers in
`feedback_history`; the wizard grows a sixth step (`questions`) between
Intent and Documents.

**Tech Stack:** Python/pydantic/LangChain (SDK), FastAPI (backend),
React + TypeScript + vitest (frontend), Playwright (e2e).

**Spec:** `docs/superpowers/specs/2026-08-24-clarifying-questions-design.md`

## Global Constraints

- Run everything through `./.venv/Scripts/python.exe`.
- Code comments in English; UI copy bilingual (en + zhCN, English default);
  British English spelling in customer-facing copy.
- Every test file carries a `pytestmark` (`unit`/`integration`/`e2e`).
- No new dependencies.
- e2e-smoke deselects wizard tests → the full e2e tier must be run locally
  before pushing (ports 8000/5173 free, serial, no `-n auto` on e2e).
- All four local gates before pushing: frontend lint + build + vitest,
  backend pytest, e2e.

---

### Task 1: SDK — `QuestionAnswer` + `answers` rendering + analyst prompt

**Files:**
- Modify: `src/bestteam/core/requirements.py`
- Modify: `src/bestteam/__init__.py` (export `QuestionAnswer` beside `Requirements`)
- Test: `tests/test_requirements.py`

**Interfaces:**
- Produces: `class QuestionAnswer(BaseModel): question: str; answer: str = ""`
  and `generate_requirements(..., answers: Optional[Sequence[QuestionAnswer]] = None)`.
  Rendering order in the human message: intent/as-is → `current` block →
  answers block → `feedback` block.

- [ ] **Step 1: Write failing tests** (append to `tests/test_requirements.py`, reusing its `_RecordingModel` pattern):

```python
def _recording_model(seen_messages):
    class _RecordingModel(_FakeAnalystChatModel):
        def with_structured_output(self, schema, **kwargs):
            def _invoke(messages):
                seen_messages.append(messages)
                return self.responses[0]

            return RunnableLambda(_invoke)

    return _RecordingModel(responses=[Requirements(summary="Updated summary")])


def test_generate_requirements_renders_answers_paired_with_questions():
    from bestteam import QuestionAnswer

    seen_messages = []
    model = _recording_model(seen_messages)
    current = Requirements(clarifying_questions=["How many emails per day?"])

    generate_requirements(
        model,
        "Help with support.",
        "",
        current=current,
        answers=[QuestionAnswer(question="How many emails per day?", answer="About 40")],
    )

    content = seen_messages[0][1].content
    assert "The customer was asked these clarifying questions:" in content
    assert "Q: How many emails per day?" in content
    assert "A: About 40" in content


def test_generate_requirements_marks_blank_answers_for_assumption():
    from bestteam import QuestionAnswer

    seen_messages = []
    model = _recording_model(seen_messages)

    generate_requirements(
        model,
        "Help with support.",
        "",
        current=Requirements(clarifying_questions=["Which mailbox provider?"]),
        answers=[QuestionAnswer(question="Which mailbox provider?", answer="   ")],
    )

    content = seen_messages[0][1].content
    assert "A: (not answered" in content
    assert '"Assumed:"' in content


def test_generate_requirements_puts_answers_between_current_and_feedback():
    from bestteam import QuestionAnswer

    seen_messages = []
    model = _recording_model(seen_messages)
    current = Requirements(goals=["Reply within an hour"])

    generate_requirements(
        model,
        "Help with support.",
        "",
        current=current,
        answers=[QuestionAnswer(question="Which tone?", answer="Friendly")],
        feedback="Also cover refunds.",
    )

    content = seen_messages[0][1].content
    assert content.index("Reply within an hour") < content.index("Q: Which tone?")
    assert content.index("Q: Which tone?") < content.index("Also cover refunds.")


def test_analyst_prompt_carries_the_asking_and_folding_policy():
    from bestteam.core.requirements import _ANALYST_SYSTEM_PROMPT

    assert "up to 4" in _ANALYST_SYSTEM_PROMPT
    assert "Assumed:" in _ANALYST_SYSTEM_PROMPT
    assert "Never re-ask" in _ANALYST_SYSTEM_PROMPT
```

- [ ] **Step 2: Run to verify failure**: `./.venv/Scripts/python.exe -m pytest tests/test_requirements.py -v` — new tests FAIL (ImportError `QuestionAnswer` / assertion).

- [ ] **Step 3: Implement** in `src/bestteam/core/requirements.py`:

```python
class QuestionAnswer(BaseModel):
    """One clarifying question paired with the customer's answer.

    An empty/whitespace answer means the customer declined to answer: the
    analyst is instructed to make its best assumption, record it in
    `constraints` prefixed "Assumed:", and retire the question.
    """

    question: str
    answer: str = ""


_UNANSWERED_NOTE = (
    '(not answered -- make your best assumption, add it to `constraints` '
    'prefixed "Assumed:", and remove the question)'
)
```

In `generate_requirements` add keyword `answers: Optional[Sequence[QuestionAnswer]] = None`
(import `Sequence` from typing) and, after the `current` block / before the
`feedback` block:

```python
    if answers:
        qa_lines = ["The customer was asked these clarifying questions:"]
        for qa in answers:
            qa_lines.append(f"Q: {qa.question}")
            qa_lines.append(f"A: {qa.answer.strip() or _UNANSWERED_NOTE}")
        content += "\n\n" + "\n".join(qa_lines)
```

Replace the system prompt's "too vague → 1-2 questions" paragraph with:

```
List in `clarifying_questions` up to 4 short questions whose answers would \
most change what team should be built -- missing volumes, tools, languages, \
tone, approval steps, and the like. Every question is work pushed back onto \
the customer: ask only what genuinely matters, and leave the list empty if \
their description already covers it.

You may also be shown clarifying questions the customer was previously \
asked, each paired with their answer. Treat each answer as the customer's \
own words: fold it into the requirements and remove that question from \
`clarifying_questions`. Where an answer is marked "(not answered ...)", \
follow its instruction: record your best assumption in `constraints` \
prefixed "Assumed:" and remove the question. Never re-ask a question that \
has been answered or assumed; keep a question only while it is genuinely \
still open.
```

Export `QuestionAnswer` from `src/bestteam/__init__.py` beside `Requirements`.

- [ ] **Step 4: Run to verify pass**: `./.venv/Scripts/python.exe -m pytest tests/test_requirements.py -v` — all PASS.
- [ ] **Step 5: Commit** `feat(sdk): pair clarifying answers with their questions in the analyst prompt`

---

### Task 2: Fake architect — message-sensitive Requirements

**Files:**
- Modify: `src/bestteam/adapters/langgraph_adapter.py` (`_fake_architect_requirements`, `_FakeArchitectStructuredResult`, `with_structured_output`)
- Test: `tests/test_fake_architect_model.py`

**Interfaces:**
- Consumes: the Task 1 answers-block header line
  `"The customer was asked these clarifying questions:"` and the
  `"A: (not answered"` prefix.
- Produces: marker `[interview me]` in the prompt → canned questions
  `["How many emails do you receive per day?", "Which mailbox provider do you use?"]`;
  answers block → no questions, constraints gain
  `"The customer clarified: <answer>"` per answered pair and
  `"Assumed: replies can go out within one business day."` per unanswered.

- [ ] **Step 1: Write failing tests** (append to `tests/test_fake_architect_model.py`, following its existing invocation pattern — check how it builds the model, e.g. `_resolve_model("fake-architect:e2e")`, and reuse):

```python
def test_fake_architect_requirements_default_has_no_questions():
    from bestteam.core.requirements import generate_requirements

    model = _resolve_model("fake-architect:e2e")
    result = generate_requirements(model, "We handle customer support emails.")
    assert result.clarifying_questions == []


def test_fake_architect_requirements_marker_triggers_questions():
    from bestteam.core.requirements import generate_requirements

    model = _resolve_model("fake-architect:e2e")
    result = generate_requirements(model, "We handle support emails. [interview me]")
    assert result.clarifying_questions == [
        "How many emails do you receive per day?",
        "Which mailbox provider do you use?",
    ]


def test_fake_architect_requirements_folds_answers_and_assumes_blanks():
    from bestteam.core.requirements import QuestionAnswer, generate_requirements

    model = _resolve_model("fake-architect:e2e")
    result = generate_requirements(
        model,
        "We handle support emails.",
        answers=[
            QuestionAnswer(question="How many emails do you receive per day?", answer="About 40 a day"),
            QuestionAnswer(question="Which mailbox provider do you use?", answer=""),
        ],
    )
    assert result.clarifying_questions == []
    assert "The customer clarified: About 40 a day" in result.constraints
    assert "Assumed: replies can go out within one business day." in result.constraints
```

- [ ] **Step 2: Run to verify failure**: `./.venv/Scripts/python.exe -m pytest tests/test_fake_architect_model.py -v`
- [ ] **Step 3: Implement** in `langgraph_adapter.py`:

```python
_FAKE_INTERVIEW_MARKER = "[interview me]"
_FAKE_ANSWERS_HEADER = "The customer was asked these clarifying questions:"


def _fake_architect_requirements(prompt_text: str) -> "Requirements":
    from ..core.requirements import Requirements

    base = Requirements(
        summary="Customers need faster, friendlier email support.",
        pain_points=["Replies take too long."],
        goals=["Answer common questions quickly."],
        success_criteria=["Customers get a reply within minutes."],
        constraints=["Must stay professional and on-topic."],
        clarifying_questions=[],
    )
    if _FAKE_ANSWERS_HEADER in prompt_text:
        # Deterministic "folding": each answered pair lands in constraints
        # verbatim, each unanswered one becomes a fixed assumption -- so an
        # E2E test can assert the round-trip on the Confirm page.
        for line in prompt_text.splitlines():
            if line.startswith("A: (not answered"):
                base.constraints.append("Assumed: replies can go out within one business day.")
            elif line.startswith("A: "):
                base.constraints.append(f"The customer clarified: {line[len('A: '):]}")
        return base
    if _FAKE_INTERVIEW_MARKER in prompt_text:
        base.clarifying_questions = [
            "How many emails do you receive per day?",
            "Which mailbox provider do you use?",
        ]
    return base
```

`_FakeArchitectStructuredResult` accepts a value or a callable of the
flattened prompt text:

```python
class _FakeArchitectStructuredResult:
    """Returned by `_FakeArchitectChatModel.with_structured_output(...).invoke(...)`."""

    def __init__(self, value: Any) -> None:
        self._value = value

    def invoke(self, messages: Any) -> Any:
        if callable(self._value):
            text = "\n".join(str(getattr(m, "content", m)) for m in messages)
            return self._value(text)
        return self._value
```

In `with_structured_output`: `if schema is Requirements: return
_FakeArchitectStructuredResult(_fake_architect_requirements)` (pass the
function, not a call).

- [ ] **Step 4: Run to verify pass** (same command), plus
  `./.venv/Scripts/python.exe -m pytest tests/test_requirements.py tests/test_specification.py -v` for no regressions.
- [ ] **Step 5: Commit** `feat(sdk): fake-architect answers the interview deterministically for e2e`

---

### Task 3: Backend — answers path on the requirements endpoint

**Files:**
- Modify: `ui/backend/builder.py` (`RequirementsRequest`, `submit_requirements`)
- Test: `tests/test_builder_api.py`

**Interfaces:**
- Consumes: `QuestionAnswer` (import `from bestteam import QuestionAnswer`
  or `from bestteam.core.requirements import QuestionAnswer` — match the
  file's existing `Requirements` import style), Task 1's `answers=` kwarg.
- Produces: `POST /api/builder/sessions/{id}/requirements` accepting
  `{"model": ..., "answers": [{"question","answer"}]}`; appends
  `feedback_history` entry `{stage: "clarifying", answers: [...], skipped: bool, at}`.

- [ ] **Step 1: Write failing tests** (append to `tests/test_builder_api.py`; follow its existing client/session fixtures — it already creates sessions and posts requirements with `fake-architect:` or monkeypatched models; mirror the file's established pattern for a stored-requirements session):

```python
def test_answers_require_stored_requirements(client):
    session = _create_session(client)  # use the file's existing helper/pattern
    resp = client.post(
        f"/api/builder/sessions/{session['id']}/requirements",
        json={"model": "fake-architect:x", "answers": [{"question": "Q?", "answer": "A"}]},
    )
    assert resp.status_code == 400
    assert "clarifying questions" in resp.json()["detail"]


def test_answers_and_requirements_together_rejected(client):
    session = _create_session_with_requirements(client)  # per existing pattern
    resp = client.post(
        f"/api/builder/sessions/{session['id']}/requirements",
        json={
            "requirements": {"summary": "s"},
            "answers": [{"question": "Q?", "answer": "A"}],
        },
    )
    assert resp.status_code == 400


def test_answers_without_model_rejected(client):
    session = _create_session_with_requirements(client)
    resp = client.post(
        f"/api/builder/sessions/{session['id']}/requirements",
        json={"answers": [{"question": "Q?", "answer": "A"}]},
    )
    assert resp.status_code == 400


def test_answers_rerun_analyst_and_record_paired_history(client):
    session = _create_session_with_requirements(client)  # requirements via fake-architect
    resp = client.post(
        f"/api/builder/sessions/{session['id']}/requirements",
        json={
            "model": "fake-architect:x",
            "answers": [
                {"question": "How many emails do you receive per day?", "answer": "About 40"},
                {"question": "Which mailbox provider do you use?", "answer": ""},
            ],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    constraints = body["requirements_json"]["constraints"]
    assert "The customer clarified: About 40" in constraints
    assert "Assumed: replies can go out within one business day." in constraints
    entry = [e for e in body["feedback_history"] if e["stage"] == "clarifying"][-1]
    assert entry["answers"][0] == {"question": "How many emails do you receive per day?", "answer": "About 40"}
    assert entry["skipped"] is False


def test_all_blank_answers_record_a_skip(client):
    session = _create_session_with_requirements(client)
    resp = client.post(
        f"/api/builder/sessions/{session['id']}/requirements",
        json={
            "model": "fake-architect:x",
            "answers": [{"question": "Which mailbox provider do you use?", "answer": " "}],
        },
    )
    assert resp.status_code == 200
    entry = [e for e in resp.json()["feedback_history"] if e["stage"] == "clarifying"][-1]
    assert entry["skipped"] is True
```

(Adapt helper names to what `tests/test_builder_api.py` actually provides —
do not invent parallel fixtures if equivalents exist.)

- [ ] **Step 2: Run to verify failure**: `./.venv/Scripts/python.exe -m pytest tests/test_builder_api.py -k answers -v`
- [ ] **Step 3: Implement** in `ui/backend/builder.py`:

```python
class RequirementsRequest(BaseModel):
    requirements: Optional[Dict[str, Any]] = None
    model: Optional[str] = None
    feedback: Optional[str] = None
    # Answers to the stored requirements' clarifying_questions, paired.
    # Blank answers are deliberate ("skip"): the analyst records assumptions.
    answers: Optional[List[QuestionAnswer]] = None
```

In `submit_requirements`, before the existing dispatch:

```python
    if req.answers is not None:
        if req.requirements is not None:
            raise HTTPException(status_code=400, detail="Provide either 'answers' or 'requirements', not both")
        if req.model is None:
            raise HTTPException(status_code=400, detail="Answering clarifying questions needs a 'model'")
        if session.requirements_json is None:
            raise HTTPException(status_code=400, detail="There are no clarifying questions to answer yet")
        current = Requirements.model_validate(session.requirements_json)
        chat_model = _call_model(_resolve_model, req.model)
        requirements = _call_model(
            generate_requirements,
            chat_model,
            session.intent_text,
            session.as_is_text,
            current=current,
            answers=req.answers,
        )
        append_feedback(
            db,
            session_id,
            {
                "stage": "clarifying",
                "answers": [qa.model_dump() for qa in req.answers],
                "skipped": all(not qa.answer.strip() for qa in req.answers),
            },
        )
    elif req.requirements is not None:
        ...  # existing branch unchanged
```

- [ ] **Step 4: Run to verify pass**: `./.venv/Scripts/python.exe -m pytest tests/test_builder_api.py -v`
- [ ] **Step 5: Commit** `feat(backend): answering (or skipping) the clarifying questions re-runs the analyst`

---

### Task 4: Backend — answers ride `/refine`

**Files:**
- Modify: `ui/backend/builder.py` (`RefineRequest`, `refine_team`)
- Test: `tests/test_builder_api.py`

**Interfaces:**
- Produces: `POST /{id}/refine` accepting optional
  `answers: [{"question","answer"}]`; blanks filtered; analyst runs when
  feedback non-empty OR any non-blank answer; "clarifying" history entry
  only for non-blank answers.

- [ ] **Step 1: Write failing tests** (append; reuse the file's existing refine test setup — a session with requirements + specification):

```python
def test_refine_runs_analyst_on_answers_alone(client):
    session = _create_session_with_spec(client)  # per existing refine tests
    resp = client.post(
        f"/api/builder/sessions/{session['id']}/refine",
        json={
            "model": "fake-architect:x",
            "feedback": "",
            "answers": [
                {"question": "How many emails do you receive per day?", "answer": "About 40"},
                {"question": "Which mailbox provider do you use?", "answer": "  "},
            ],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    constraints = body["requirements_json"]["constraints"]
    assert "The customer clarified: About 40" in constraints
    # The blank answer was filtered out, not turned into an assumption:
    # on Confirm an unanswered question simply stays open.
    assert "Assumed: replies can go out within one business day." not in constraints
    entry = [e for e in body["feedback_history"] if e["stage"] == "clarifying"][-1]
    assert entry["answers"] == [
        {"question": "How many emails do you receive per day?", "answer": "About 40"}
    ]
    assert entry["skipped"] is False


def test_refine_with_only_blank_answers_skips_the_analyst(client):
    session = _create_session_with_spec(client)
    before = client.get(f"/api/builder/sessions/{session['id']}").json()["requirements_json"]
    resp = client.post(
        f"/api/builder/sessions/{session['id']}/refine",
        json={
            "model": "fake-architect:x",
            "feedback": "",
            "answers": [{"question": "Which mailbox provider do you use?", "answer": ""}],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["requirements_json"] == before
    assert not [e for e in body["feedback_history"] if e["stage"] == "clarifying"]
```

- [ ] **Step 2: Run to verify failure**: `./.venv/Scripts/python.exe -m pytest tests/test_builder_api.py -k refine -v`
- [ ] **Step 3: Implement** in `refine_team`:

```python
class RefineRequest(BaseModel):
    requirements: Optional[Dict[str, Any]] = None
    feedback: str = ""
    model: str
    # Non-blank answers to open clarifying questions; blanks are filtered out
    # here (no skip button on Confirm -- an unanswered question stays open).
    answers: Optional[List[QuestionAnswer]] = None
```

In the body, replace the `if req.feedback.strip():` analyst gate:

```python
    answered = [qa for qa in (req.answers or []) if qa.answer.strip()]
    if req.feedback.strip() or answered:
        requirements = _call_model(
            generate_requirements,
            chat_model,
            session.intent_text,
            session.as_is_text,
            current=requirements,
            feedback=req.feedback,
            answers=answered or None,
        )
```

And beside the existing solution-stage `append_feedback`:

```python
    if answered:
        append_feedback(
            db,
            session_id,
            {
                "stage": "clarifying",
                "answers": [qa.model_dump() for qa in answered],
                "skipped": False,
            },
        )
```

- [ ] **Step 4: Run to verify pass**: `./.venv/Scripts/python.exe -m pytest tests/test_builder_api.py -v`, then the whole backend tier: `./.venv/Scripts/python.exe -m pytest -m "not e2e" -n auto -q`
- [ ] **Step 5: Commit** `feat(backend): Confirm-page answers ride the one Update-the-team action`

---

### Task 5: Frontend foundation — types, api, i18n, sixth step, route

**Files:**
- Modify: `ui/frontend/src/lib/types.ts` (add `QuestionAnswer`)
- Modify: `ui/frontend/src/lib/api.ts` (`submitRequirements`/`refineTeam` payloads)
- Modify: `ui/frontend/src/locales/en.ts`, `ui/frontend/src/locales/zhCN.ts`
- Modify: `ui/frontend/src/components/WizardProgress.tsx`
- Modify: `ui/frontend/src/App.tsx` (route; the page itself lands in Task 6 —
  create a minimal `QuestionsPage` stub in this task only if the build needs
  it, otherwise add the route in Task 6)
- Test: `ui/frontend/src/components/WizardProgress.test.tsx`

**Interfaces:**
- Produces: `export interface QuestionAnswer { question: string; answer: string }`;
  `api.submitRequirements(id, { model?, feedback?, requirements?, answers? })`;
  `api.refineTeam(id, { requirements?, feedback, model, answers? })`;
  `STEPS = ['intent', 'questions', 'documents', 'preview', 'confirm', 'deploy']`,
  `questions` unlocked by `Boolean(session)`; locale namespaces
  `wizard.steps.questions`, `wizard.questions.*`, `wizard.confirm.clarifyHint`.

- [ ] **Step 1: Update `WizardProgress.test.tsx` expectations first** (six steps, questions unlocked with a session, locked without) — run `npx vitest run src/components/WizardProgress.test.tsx` in `ui/frontend`, verify FAIL.
- [ ] **Step 2: Implement.** `WizardProgress.tsx`: add `'questions'` after `'intent'` in `STEPS`, a `case 'questions': return t('wizard.steps.questions')`, and `questions: Boolean(session)` in `unlocked`. `types.ts`: add the interface. `api.ts`: extend both payload types with `answers?: QuestionAnswer[]` (import the type). Locales — `en.ts` under `wizard`:

```ts
    questions: {
      title: 'A few quick questions',
      subtitle:
        "Your answers help us design the right team. You can skip any question — or all of them — and we'll make a sensible assumption you can review in the summary.",
      answerPlaceholder: 'Type your answer (optional)',
      skip: 'Skip these questions',
      updating: 'Updating what we understood…',
      updatingNotice: "We're folding your answers into what we understood about your business — this takes a moment.",
      noQuestions: 'No open questions — your description gave us what we need.',
    },
```

`wizard.steps.questions: 'A few questions'`; `wizard.confirm.clarifyHint:
'Your answers are applied when you press "Update the team".'`.

`zhCN.ts` (keys must mirror en.ts — `Resources` typing enforces it):

```ts
    questions: {
      title: '几个简短的问题',
      subtitle: '您的回答能帮我们设计更合适的团队。任何问题都可以跳过——留空的问题我们会替您做一个合理的假设，您可以在摘要中查看。',
      answerPlaceholder: '输入您的回答（可选）',
      skip: '跳过这些问题',
      updating: '正在更新我们的理解…',
      updatingNotice: '正在把您的回答融入我们对您业务的理解——请稍等片刻。',
      noQuestions: '没有待回答的问题——您的描述已经足够清楚。',
    },
```

`wizard.steps.questions: '补充问题'`; `wizard.confirm.clarifyHint:
'按下"更新团队"后，您的回答会被一并采纳。'`.

- [ ] **Step 3: Verify**: in `ui/frontend`, `npx vitest run src/components/WizardProgress.test.tsx` PASS, `npx tsc -b --noEmit` (or `npm run build`) clean.
- [ ] **Step 4: Commit** `feat(frontend): sixth wizard step, QuestionAnswer plumbing, bilingual copy`

---

### Task 6: QuestionsPage + route

**Files:**
- Create: `ui/frontend/src/pages/wizard/QuestionsPage.tsx`
- Modify: `ui/frontend/src/App.tsx` (import + `<Route path=":sessionId/questions" element={<QuestionsPage />} />`)
- Test: `ui/frontend/src/pages/wizard/QuestionsPage.test.tsx`

**Interfaces:**
- Consumes: `WizardOutletContext` (`session, setSession, loading, sessionId,
  setNavBusy`), `api.submitRequirements` with `answers`, locale keys from
  Task 5.

- [ ] **Step 1: Write failing tests** (`QuestionsPage.test.tsx`, modelled on `ConfirmPage.test.tsx`'s mock scaffolding — mock `../../lib/api` with `modelCatalog` + `submitRequirements`, mock `useOutletContext`/`useNavigate`):

```tsx
// Cases:
// 1. renders one textarea per clarifying question (labels = question text)
// 2. Continue is disabled until at least one answer is non-blank
// 3. Continue sends the full paired batch (typed answers trimmed, blanks '')
//    and navigates to /wizard/s1/documents on success
// 4. Skip is enabled with all answers blank and sends every answer as ''
// 5. a rejected submitRequirements shows the error banner and re-enables
// 6. with no clarifying questions, shows the noQuestions card whose Continue
//    navigates to documents without calling the API
```

Test 3's assertion, concretely:

```tsx
await waitFor(() =>
  expect(mockedApi.submitRequirements).toHaveBeenCalledWith('s1', {
    model: 'deepseek:friendly-assistant',
    answers: [
      { question: 'How many emails do you receive per day?', answer: 'About 40' },
      { question: 'Which mailbox provider do you use?', answer: '' },
    ],
  }),
)
expect(navigateMock).toHaveBeenCalledWith('/wizard/s1/documents')
```

- [ ] **Step 2: Run to verify failure**: `npx vitest run src/pages/wizard/QuestionsPage.test.tsx`
- [ ] **Step 3: Implement `QuestionsPage.tsx`:**

```tsx
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate, useOutletContext } from 'react-router-dom'
import { api } from '../../lib/api'
import { pickDefaultModel } from '../../lib/models'
import { useModelCatalog } from '../../lib/useModelCatalog'
import type { WizardOutletContext } from '../../lib/types'

export default function QuestionsPage() {
  const { t } = useTranslation()
  const { session, setSession, loading, sessionId, setNavBusy } = useOutletContext<WizardOutletContext>()
  const navigate = useNavigate()
  const { entries, loading: catalogLoading, failed: catalogFailed, retry: retryCatalog } = useModelCatalog()
  const catalogUnavailable = catalogFailed || (!catalogLoading && entries.length === 0)

  const [answers, setAnswers] = useState<Record<number, string>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  if (loading) return <p className="hint">{t('common.loading')}</p>
  if (!session) return null

  const questions = session.requirements_json?.clarifying_questions ?? []
  const anyAnswered = questions.some((_, i) => (answers[i] ?? '').trim())

  if (questions.length === 0) {
    return (
      <div className="wizard-card">
        <h2>{t('wizard.questions.title')}</h2>
        <p className="subtitle">{t('wizard.questions.noQuestions')}</p>
        <div className="wizard-actions">
          <button className="btn btn-primary" onClick={() => navigate(`/wizard/${sessionId}/documents`)}>
            {t('common.continue')}
          </button>
        </div>
      </div>
    )
  }

  // One call for both buttons: a skip is the same request with every answer
  // blank, and the analyst records the assumptions it made instead.
  const submit = async (skip: boolean) => {
    if (busy || catalogLoading || catalogUnavailable) return
    setBusy(true)
    // The analyst call is long enough for the customer to wander; suspend the
    // step bar like the Confirm page does while its one action is in flight.
    setNavBusy(true)
    setError(null)
    try {
      const updated = await api.submitRequirements(sessionId!, {
        model: pickDefaultModel(entries),
        answers: questions.map((question, i) => ({
          question,
          answer: skip ? '' : (answers[i] ?? '').trim(),
        })),
      })
      setSession(updated)
      navigate(`/wizard/${sessionId}/documents`)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
      setNavBusy(false)
    }
  }

  return (
    <div className="wizard-card">
      <h2>{t('wizard.questions.title')}</h2>
      <p className="subtitle">{t('wizard.questions.subtitle')}</p>

      {catalogUnavailable && (
        <div className="banner banner-error">
          {catalogFailed ? t('modelCatalog.loadFailed') : t('modelCatalog.empty')}
          <div className="wizard-actions" style={{ marginTop: 8 }}>
            <button className="btn btn-secondary" onClick={retryCatalog}>
              {t('common.tryAgain')}
            </button>
          </div>
        </div>
      )}

      {error && (
        <div className="banner banner-error">
          {error}
          <div className="wizard-actions" style={{ marginTop: 8 }}>
            <button className="btn btn-secondary" onClick={() => submit(false)} disabled={busy}>
              {t('common.tryAgain')}
            </button>
          </div>
        </div>
      )}

      {questions.map((question, i) => (
        <div className="field" key={i}>
          <label htmlFor={`question-${i}`}>{question}</label>
          <textarea
            id={`question-${i}`}
            rows={2}
            value={answers[i] ?? ''}
            onChange={(e) => setAnswers((prev) => ({ ...prev, [i]: e.target.value }))}
            placeholder={t('wizard.questions.answerPlaceholder')}
            disabled={busy}
          />
        </div>
      ))}

      <div className="wizard-actions">
        <button
          className="btn btn-secondary"
          onClick={() => submit(true)}
          disabled={busy || catalogLoading || catalogUnavailable}
        >
          {t('wizard.questions.skip')}
        </button>
        <button
          className="btn btn-primary"
          onClick={() => submit(false)}
          disabled={busy || catalogLoading || catalogUnavailable || !anyAnswered}
        >
          {busy ? t('wizard.questions.updating') : t('common.continue')}
        </button>
      </div>
      {busy && <p className="hint">{t('wizard.questions.updatingNotice')}</p>}
    </div>
  )
}
```

- [ ] **Step 4: Verify**: `npx vitest run src/pages/wizard/QuestionsPage.test.tsx` PASS; `npm run build` clean.
- [ ] **Step 5: Commit** `feat(frontend): the questions step — answer or skip the analyst's interview`

---

### Task 7: IntentPage forks to the questions step

**Files:**
- Modify: `ui/frontend/src/pages/wizard/IntentPage.tsx`
- Test: create `ui/frontend/src/pages/wizard/IntentPage.test.tsx`

- [ ] **Step 1: Write failing tests** (new file; mock `../../lib/api` with `modelCatalog`, `createSession`, `submitRequirements`; mock `useNavigate`):

```tsx
// Cases:
// 1. requirements with clarifying questions -> navigate('/wizard/s1/questions')
// 2. requirements with no questions -> navigate('/wizard/s1/documents')
// 3. submitRequirements rejects -> still navigate('/wizard/s1/documents')
//    (the existing best-effort contract)
```

- [ ] **Step 2: Run to verify failure**: `npx vitest run src/pages/wizard/IntentPage.test.tsx`
- [ ] **Step 3: Implement** — in `start()`, replace the fire-and-forget block:

```tsx
    // Best-effort: /specification degrades gracefully (falls back to the raw
    // intent/as-is text) if this fails, so don't block on it. When the
    // analyst has questions, the interview step comes before any documents.
    setStage('requirements')
    let next = `/wizard/${id}/documents`
    try {
      const updated = await api.submitRequirements(id, { model })
      if (updated.requirements_json?.clarifying_questions?.length) {
        next = `/wizard/${id}/questions`
      }
    } catch {
      // ignored — non-blocking
    }

    navigate(next)
```

- [ ] **Step 4: Verify**: `npx vitest run src/pages/wizard/IntentPage.test.tsx` PASS.
- [ ] **Step 5: Commit** `feat(frontend): Intent hands over to the interview when the analyst has questions`

---

### Task 8: ConfirmPage — answerable questions ride the one action

**Files:**
- Modify: `ui/frontend/src/pages/wizard/ConfirmPage.tsx`
- Test: `ui/frontend/src/pages/wizard/ConfirmPage.test.tsx`

- [ ] **Step 1: Write failing tests** (append; give the mocked session a
  `requirements_json` with `clarifying_questions: ['Which tone should replies use?']`):

```tsx
// Cases:
// 1. each clarifying question renders a textarea (label = question text)
// 2. a typed answer is sent: refineTeam called with
//    answers: [{ question: 'Which tone should replies use?', answer: 'Friendly' }]
//    alongside requirements/feedback/model
// 3. with the answer left blank, refineTeam payload has NO answers key
//    (blanks are filtered client-side; the question stays open)
```

- [ ] **Step 2: Run to verify failure**: `npx vitest run src/pages/wizard/ConfirmPage.test.tsx`
- [ ] **Step 3: Implement:**
  - Add `const [answerDraft, setAnswerDraft] = useState<Record<number, string>>({})`;
    in the existing `useEffect` that syncs `reqDraft` from
    `session.requirements_json`, also `setAnswerDraft({})` (a regeneration
    retires/replaces questions, so stale answers must not survive it).
  - Replace the read-only questions banner with textareas (ids `clarify-${i}`,
    label = question, placeholder `t('wizard.questions.answerPlaceholder')`,
    `disabled={busy}`), followed by
    `<p className="hint">{t('wizard.confirm.clarifyHint')}</p>`.
  - In `updateTeam`, before the API call:

```tsx
    const answered = reqDraft.clarifying_questions
      .map((question, i) => ({ question, answer: (answerDraft[i] ?? '').trim() }))
      .filter((qa) => qa.answer)
```

  and extend the payload:

```tsx
      const updated = await api.refineTeam(sessionId!, {
        ...(session.requirements_json ? { requirements: reqDraft } : {}),
        ...(answered.length ? { answers: answered } : {}),
        feedback: feedback.trim(),
        model: pickDefaultModel(catalogEntries),
      })
```

- [ ] **Step 4: Verify**: `npx vitest run src/pages/wizard/ConfirmPage.test.tsx` PASS (existing cases must stay green — the no-answers payload shape is unchanged); full `npx vitest run`; `npm run lint`; `npm run build`.
- [ ] **Step 5: Commit** `feat(frontend): Confirm-page questions take answers through Update the team`

---

### Task 9: E2E + full local gates

**Files:**
- Modify: `tests/e2e/test_wizard_full.py`

- [ ] **Step 1: Add the marker-driven test:**

```python
def test_t4_7_clarifying_questions_answer_and_skip(page):
    """The interview step: the analyst's questions appear after Intent; one
    answered, one left blank -> the folded answer and the recorded assumption
    both surface as constraints on the Confirm page."""
    _login(page)
    page.goto(BASE_URL + "/wizard")
    page.wait_for_selector("#intent", timeout=8000)
    page.fill("#intent", "We handle customer support emails. [interview me]")
    page.click("button:has-text('Start building my team')")
    page.wait_for_url("**/questions", timeout=15000)

    page.fill("#question-0", "About 40 a day")
    page.click("button:has-text('Continue')")
    page.wait_for_url("**/documents", timeout=20000)
    page.click("button:has-text('Skip for now')")
    page.wait_for_url("**/preview", timeout=20000)
    page.wait_for_selector(".team-flow, .employee-card", timeout=8000)
    page.click("button:has-text('Continue')")
    page.wait_for_url("**/confirm", timeout=8000)

    # Constraints render as BulletEditor inputs -- match on displayed value.
    pw_expect(page.get_by_display_value("The customer clarified: About 40 a day")).to_be_visible()
    pw_expect(
        page.get_by_display_value("Assumed: replies can go out within one business day.")
    ).to_be_visible()
```

(Verify how `BulletEditor` renders items first — if items are not `<input>`
elements, switch the assertion to the matching locator.)

- [ ] **Step 2: Run the four gates, in order:**
  1. `cd ui/frontend && npm run lint && npm run build && npx vitest run`
  2. `./.venv/Scripts/python.exe -m pytest -m "not e2e" -n auto -q`
  3. `./.venv/Scripts/python.exe -m pytest tests/e2e -q` (serial; ports 8000/5173 free)
- [ ] **Step 3: Fix anything red; re-run until green.**
- [ ] **Step 4: Commit** `test(e2e): the interview step answers and skips deterministically`

---

### Task 10: Documentation

**Files:**
- Modify: `docs/STATUS.md` (roadmap entry → done; note the shipped shape)
- Modify: `ui/frontend/CLAUDE.md` ("five steps" → six; QuestionsPage;
  ConfirmPage questions now answerable through the one action)
- Modify: `src/bestteam/core/CLAUDE.md` (Requirements section: `answers=`,
  `QuestionAnswer`, the asking/folding policy)
- Check: root `CLAUDE.md` and `docs/team_builder_methodology.md` for step
  counts or "clarifying" mentions needing sync (grep before editing).

- [ ] **Step 1: Update each doc.** STATUS.md: remove the roadmap bullet, add
  a Done entry recording: the sixth wizard step, answers paired in
  `feedback_history` "clarifying" entries, the skip→`Assumed:` contract, the
  Confirm-page one-action integration, and the `[interview me]` e2e marker.
- [ ] **Step 2: Commit** `docs: record the clarifying-questions interview as shipped`

---

## Self-review notes

- Spec coverage: §1→Tasks 1/3, §2→Task 1, §3→Tasks 3/4, §4→Tasks 5-8,
  §5→Tasks 2/9, testing→each task + Task 9; docs→Task 10. No gaps.
- Type consistency: `QuestionAnswer{question, answer}` identical across SDK,
  backend, frontend; `answers` optional everywhere; blank-filtering only in
  `/refine` + ConfirmPage, blanks preserved on the Questions step.
- Helper names in Task 3/4 tests are placeholders by necessity (`_create_session*`)
  with an explicit instruction to adapt to the file's real fixtures.
