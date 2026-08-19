"""End-to-end example using a fake LLM — runs with no API key required.

Swap `FakeListChatModel` for any `langchain_core.BaseChatModel` instance
(e.g. `ChatOpenAI()`, `init_chat_model("anthropic:claude-sonnet-4-6")`, ...)
to run the same pipeline against a real provider — nothing else changes.
"""

from langchain_core.language_models.fake_chat_models import FakeListChatModel

from bestteam import Agent, CollaborationMode, Pipeline, Team

researcher = Agent(
    name="researcher",
    role="Research Analyst",
    goal="Find accurate, relevant information about the given topic",
    model=FakeListChatModel(responses=["Found three relevant sources on multi-agent systems."]),
)

writer = Agent(
    name="writer",
    role="Content Writer",
    goal="Turn research notes into a clear, engaging article",
    model=FakeListChatModel(responses=["Here is a short article based on the research above."]),
)

content_team = Team(
    name="content_team",
    agents=[researcher, writer],
    mode=CollaborationMode.SEQUENTIAL,
)

pipeline = Pipeline(name="article_pipeline", steps=[content_team])

if __name__ == "__main__":
    result = pipeline.run("Write a short article about multi-agent AI systems.")

    print("=== Final output ===")
    print(result.output)

    print("\n=== Step-by-step ===")
    for step in result.steps:
        print(f"- {step['agent']}: {step['output']}")

    print("\n=== Graph (Mermaid) ===")
    print(pipeline.visualize())
