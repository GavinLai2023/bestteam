"""Demo: attaching built-in tools to agents.

Uses fake models so no real API keys are needed.
The calculator tool runs for real (stdlib only).
web_search is registered on the agent but not actually called by FakeListChatModel
— in production, swap the model for a real LLM and it will invoke tools as needed.
"""
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from bestteam import Agent, CollaborationMode, Team, Workflow, calculator, web_search

fake = FakeListChatModel(responses=[
    "I'll search for the latest AI news and crunch the numbers.",
    "Based on the search results and calculations, here is the final brief.",
])

researcher = Agent(
    name="researcher",
    role="Research Analyst",
    goal="Find the latest developments in AI and compute key statistics",
    backstory="Expert at synthesising web search results into concise briefs.",
    model=fake,
    tools=[web_search, calculator],
)

writer = Agent(
    name="writer",
    role="Content Writer",
    goal="Turn the researcher's findings into a polished one-paragraph brief",
    model=fake,
)

team = Team(
    name="brief_team",
    agents=[researcher, writer],
    mode=CollaborationMode.SEQUENTIAL,
)

workflow = Workflow(name="research_brief", steps=[team])

print("=== Running research brief workflow ===\n")
result = workflow.run("What are the biggest AI breakthroughs this week?")
print("Final output:\n", result.output)

print("\n=== calculator tool (runs standalone) ===")
print("(3 + 4) * 2 ** 8 =", calculator("(3 + 4) * 2 ** 8"))
print("355 / 113        =", calculator("355 / 113"))
