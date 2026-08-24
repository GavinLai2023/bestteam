from __future__ import annotations

from typing import List, Optional, Sequence

from langchain_core.exceptions import OutputParserException
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from ..exceptions import ConfigurationError
from ._structured_output import invoke_structured

# Guides the "Business Analyst" agent (see docs/team_builder_methodology.md,
# Requirements stage) from a customer's free-text Intent/As-is description to
# structured Requirements. Written for a real, structured-output-capable
# model -- not a `fake:` chat model.
_ANALYST_SYSTEM_PROMPT = """You are the Business Analyst for bestteam, a multi-agent \
team-building platform for non-technical customers.

A customer has described, in their own words, the challenge they want to \
solve (their "Intent") and, optionally, how they handle it today (their \
"As-is" process).

Summarize this into structured Requirements: the customer's pain points, \
their goals, what success looks like, and any hard constraints (budget, \
tools, compliance, tone, languages, etc.) mentioned or implied. Write a short \
plain-language `summary` a non-technical person would recognize as an \
accurate restatement of what they told you.

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

You may also be given the current understanding, which the customer has just \
reviewed and may have edited by hand, followed by additional information they \
typed. Treat the current understanding as their own words: keep its wording \
and its individual entries unless the additional information contradicts \
them. Where the two conflict, the additional information is the later word \
and wins."""


class QuestionAnswer(BaseModel):
    """One clarifying question paired with the customer's answer.

    An empty/whitespace answer means the customer declined to answer: the
    analyst is instructed to make its best assumption, record it in
    `constraints` prefixed "Assumed:", and retire the question.
    """

    question: str
    answer: str = ""


_UNANSWERED_NOTE = (
    "(not answered -- make your best assumption, add it to `constraints` "
    'prefixed "Assumed:", and remove the question)'
)


class Requirements(BaseModel):
    """Structured output of the Business Analyst agent (Intent/As-is -> Requirements).

    This is the customer-facing "requirements summary card" of
    docs/team_builder_methodology.md's Requirements stage, and the input to
    the Solution Architect (`generate_specification`, see
    `core/specification.py`).
    """

    summary: str = ""
    pain_points: List[str] = Field(default_factory=list)
    goals: List[str] = Field(default_factory=list)
    success_criteria: List[str] = Field(default_factory=list)
    constraints: List[str] = Field(default_factory=list)
    clarifying_questions: List[str] = Field(default_factory=list)

    def to_prompt(self) -> str:
        """Render as plain text, suitable as the `requirements` argument to
        `generate_specification`."""
        lines = [self.summary] if self.summary else []
        for label, items in (
            ("Pain points", self.pain_points),
            ("Goals", self.goals),
            ("Success criteria", self.success_criteria),
            ("Constraints", self.constraints),
        ):
            if items:
                lines.append(f"{label}:")
                lines.extend(f"- {item}" for item in items)
        return "\n".join(lines)


def generate_requirements(
    model: BaseChatModel,
    intent_text: str,
    as_is_text: str = "",
    *,
    current: Optional[Requirements] = None,
    answers: Optional[Sequence[QuestionAnswer]] = None,
    feedback: Optional[str] = None,
    max_attempts: int = 3,
) -> Requirements:
    """Summarize a customer's Intent/As-is description into structured Requirements.

    `feedback` is the customer's reply to a previous round (e.g. answers to
    `clarifying_questions`, or a correction) and is appended to the prompt so
    the analyst can revise its summary.

    `answers` are the customer's replies to a batch of `clarifying_questions`,
    each paired with the question it answers. A blank answer means the
    customer declined: the analyst assumes, records the assumption in
    `constraints` prefixed "Assumed:", and retires the question. Rendered
    after `current` and before `feedback`.

    `current` is the understanding the customer is refining, including any
    edits they made to it by hand. Without it a refinement round re-derives
    from `intent_text` alone and silently forgets what earlier rounds
    established, so a caller refining an existing summary should always pass
    it. It goes in *before* `feedback`, which is the customer's newest word
    and wins where the two conflict.

    Self-corrects on `OutputParserException` (mirrors
    `generate_specification`'s retry loop, see `core/specification.py`): the
    Business Analyst's completion can fail to fit the Requirements schema at
    all, which `with_structured_output` raises before a `Requirements`
    instance exists to return.
    """
    content = f"Intent/Challenge:\n{intent_text}\n\nCurrent process (as-is):\n{as_is_text or '(not described)'}"
    if current is not None and (current_text := current.to_prompt()):
        content += f"\n\nThe current understanding, as the customer has edited it:\n{current_text}"
    if answers:
        # Each pair rides the prompt verbatim; a blank answer carries the
        # skip contract (assume, record "Assumed:" in constraints, retire the
        # question) so the system prompt's folding rules have one shape to act on.
        qa_lines = ["The customer was asked these clarifying questions:"]
        for qa in answers:
            qa_lines.append(f"Q: {qa.question}")
            qa_lines.append(f"A: {qa.answer.strip() or _UNANSWERED_NOTE}")
        content += "\n\n" + "\n".join(qa_lines)
    if feedback:
        content += f"\n\nAdditional information from the customer:\n{feedback}"

    messages: List[BaseMessage] = [SystemMessage(content=_ANALYST_SYSTEM_PROMPT), HumanMessage(content=content)]

    last_error: Optional[OutputParserException] = None
    method = "function_calling"
    for _ in range(max_attempts):
        try:
            result, method = invoke_structured(model, Requirements, messages, method=method)
        except NotImplementedError as exc:
            raise ConfigurationError(
                "The Team Builder needs a real AI model that can produce structured "
                "output; the selected model can't (for example, a demo 'fake:' model). "
                "Choose a real model to design your team."
            ) from exc
        except OutputParserException as exc:
            last_error = exc
            messages.append(AIMessage(content=exc.llm_output or str(exc)))
            messages.append(
                HumanMessage(
                    content=(
                        f"That didn't work yet: {exc} Please try again, matching the required format."
                    )
                )
            )
            continue
        return result if isinstance(result, Requirements) else Requirements.model_validate(result)

    raise ConfigurationError(
        f"Business Analyst could not produce valid Requirements after {max_attempts} attempts. "
        f"Last error: {last_error}"
    )
