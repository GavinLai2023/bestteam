"""Same Reviewer -> Fixer pipeline as code_review_demo.py, but wired to a real
OpenAI model via langchain-openai instead of FakeListChatModel.

Note how nothing in the bestteam SDK usage changes — only the `model=` value
passed to each Agent. That's the adapter seam doing its job: customer code is
identical whether the backing model is fake, OpenAI, Anthropic, or anything
else that exposes a langchain BaseChatModel interface.

Requires OPENAI_API_KEY in the environment.
"""

from langchain_openai import ChatOpenAI

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

model = ChatOpenAI(model="gpt-4o-mini", temperature=0)

reviewer = Agent(
    name="reviewer",
    role="Senior Code Reviewer",
    goal="Find correctness bugs and missing edge-case handling in the given code. "
         "Be specific: name the function and the exact failure scenario.",
    backstory="Has caught countless production incidents caused by unhandled edge cases",
    model=model,
)

fixer = Agent(
    name="fixer",
    role="Bug Fix Engineer",
    goal="Patch the issues raised in the review with minimal, targeted changes. "
         "Return the corrected code in a fenced Python code block.",
    backstory="Prefers small, safe diffs over rewrites",
    model=model,
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
