"""Code review demo: a Reviewer agent finds issues, a Fixer agent patches them.

Uses FakeListChatModel so it runs with no API key — swap in a real
BaseChatModel (ChatOpenAI, ChatAnthropic, ...) to get genuine reviews;
nothing else in this script changes.
"""

from langchain_core.language_models.fake_chat_models import FakeListChatModel

from bestteam import Agent, CollaborationMode, Team, Workflow

CODE_TO_REVIEW = '''\
def calculate_average(numbers):
    total = 0
    for n in numbers:
        total += n
    return total / len(numbers)


def get_user(users, user_id):
    for u in users:
        if u["id"] == user_id:
            return u
'''

reviewer = Agent(
    name="reviewer",
    role="Senior Code Reviewer",
    goal="Find correctness bugs and missing edge-case handling in the given code",
    backstory="Has caught countless production incidents caused by unhandled edge cases",
    model=FakeListChatModel(responses=[
        "Found 2 issues:\n"
        "1. `calculate_average`: divides by `len(numbers)` without checking for an "
        "empty list -> ZeroDivisionError.\n"
        "2. `get_user`: returns None (implicitly) when no user matches, but callers "
        "aren't signaled this can happen -> potential AttributeError downstream."
    ]),
)

fixer = Agent(
    name="fixer",
    role="Bug Fix Engineer",
    goal="Patch the issues raised in the review with minimal, targeted changes",
    backstory="Prefers small, safe diffs over rewrites",
    model=FakeListChatModel(responses=[
        "Patched both issues:\n\n"
        "```python\n"
        "def calculate_average(numbers):\n"
        "    if not numbers:\n"
        "        raise ValueError(\"calculate_average() requires a non-empty sequence\")\n"
        "    return sum(numbers) / len(numbers)\n"
        "\n"
        "\n"
        "def get_user(users, user_id):\n"
        "    for u in users:\n"
        "        if u[\"id\"] == user_id:\n"
        "            return u\n"
        "    raise KeyError(f\"No user with id={user_id!r}\")\n"
        "```"
    ]),
)

qa_team = Team(
    name="qa_team",
    agents=[reviewer, fixer],
    mode=CollaborationMode.SEQUENTIAL,
)

workflow = Workflow(name="code_review_pipeline", steps=[qa_team])

if __name__ == "__main__":
    print("=== Code under review ===")
    print(CODE_TO_REVIEW)

    result = workflow.run(f"Review this Python code and report any bugs:\n\n{CODE_TO_REVIEW}")

    print("=== Step-by-step ===")
    for step in result.steps:
        print(f"\n--- {step['agent']} ---")
        print(step["output"])

    print("\n=== Final pipeline output (Fixer's patch) ===")
    print(result.output)
