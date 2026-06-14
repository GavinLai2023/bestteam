# bestteam — `src/bestteam/` (SDK core)

Directory-scoped notes for the SDK layer — `Agent`/`Team`/`Workflow` and the
LangGraph adapter. See the root `CLAUDE.md` for project overview,
architecture, and commands. For the Specification/Requirements
structured-output stages and knowledge bases, see
`src/bestteam/core/CLAUDE.md`; for built-in tools, see
`src/bestteam/tools/CLAUDE.md`.

## SDK layer

`Agent`/`Team`/`Workflow` are business-facing dataclasses, decoupled from the
engine via an `EngineAdapter` ABC (`adapters/base.py`). The only
implementation today is `LangGraphAdapter` (`adapters/langgraph_adapter.py`);
the seam exists so a CrewAI (or other) adapter could be added later without
touching the public API. Workflows are declarative YAML (parsed by
`core/loader.py`) — see `ui/backend/workflows/*.yaml` for examples of
sequential and parallel collaboration modes.

## Key design decisions worth knowing

- **State reducer for parallel agents**: `_TeamState.contributions` uses
  `Annotated[Dict[str, str], operator.or_]` so concurrent node writes merge
  instead of raising `InvalidUpdateError`.
- **`fake:<response>` model spec**: a custom convenience added to
  `_resolve_model()` so YAML workflows can declare zero-cost,
  deterministic dry-run models (`FakeListChatModel` under the hood) without
  needing real API keys. Use this for demos/tests — see
  `ui/backend/workflows/*.yaml`.
- **Preserve `BestTeamError` subtypes through adapter boundaries**: always
  `except BestTeamError: raise` *before* a broad `except Exception` in
  adapter code, otherwise `ConfigurationError`/etc. get masked as a generic
  `EngineError` and the real cause is lost.

## Known limitation: CrewAI adapter, DEBATE mode, deployment templates

CrewAI adapter, DEBATE collaboration mode, and deployment templates are
planned on the roadmap, not started. HIERARCHICAL *is* implemented: the
`manager` agent gets a `delegate_to_<name>(task)` tool per subordinate, bound
alongside its own `tools` and run through the same tool-calling loop as
SEQUENTIAL/PARALLEL agents — see `_hierarchical_node` in
`adapters/langgraph_adapter.py`.
