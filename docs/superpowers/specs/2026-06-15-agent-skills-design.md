# Agent Skills (SDK layer) — design

## Context

The user asked whether `Agent`s could be assembled from composable "Skills"
("像搭积木一样"), with Skills bundled into Agents and Agents into Teams —
plus a "Skills library" so an admin (or the platform) can package common
capabilities once and reuse them across agents, without making the platform
feel bloated ("臃肿").

Today, an `Agent` is already partially "composed from named building blocks"
at the tool level: `AgentSpec.tools: List[str]` references names resolved
against `tools.REGISTRY` (the four built-in tools) plus any `extra_tools` and
knowledge bases (which register as named tools too) — see
`src/bestteam/core/CLAUDE.md` and `src/bestteam/tools/CLAUDE.md`.

A "Skill" in this design is modeled on Claude Code's Agent Skills: a named,
reusable **instruction document** ("how to do a repeatable task") that
*declares* which existing tools it needs. It has no model/LLM of its own —
it's text the referencing Agent's own model reads and follows, plus a list of
tool names that get folded into that Agent's tool list. Skills sit between
Tools/MCP (lowest layer) and Agent:

```
Tools/MCP  →  Skills (instructions + tool refs)  →  Agent  →  Team
```

## Scope of this spec

This spec covers **sub-project 1 only**: the SDK/loader-level primitive —
`SkillSpec`, `AgentSpec.skills`, and the `_build_workflow(..., extra_skills=...)`
resolution that merges a Skill's instructions and tools into the agent that
references it.

**Sub-project 2** — a persistent, cross-workflow "Skills library" (DB table +
`/api/config/skills` CRUD + Solution Architect auto-assignment + frontend
picker) — is **out of scope** here and recorded as a roadmap item in
`docs/STATUS.md`. It builds directly on sub-project 1: the library stores
`SkillSpec` records and the UI backend passes them in as `extra_skills`.

This keeps sub-project 1 self-contained, testable with `fake:` models (no
DB/UI), and consistent with the project's incremental, phase-based history
(see `docs/STATUS.md`).

## Design

### 1. Data model — `SkillSpec` + `AgentSpec.skills`

A new Pydantic model in `src/bestteam/core/specification.py`, in the same
style as `KnowledgeBaseSpec`:

```python
class SkillSpec(BaseModel):
    name: str
    description: str = ""
    instructions: str
    tools: List[str] = []
```

- `name` — unique identifier; how `AgentSpec.skills` references it, and the
  natural primary key for sub-project 2's `/api/config/skills` CRUD (defined
  now so sub-project 2 needs no rework).
- `description` — human-readable summary. Unused by sub-project 1's
  resolution logic, but defined now for sub-project 2 (Solution Architect /
  picker UI).
- `instructions` — the "how-to" text, appended to the backstory of any agent
  that references this skill.
- `tools` — tool names this skill depends on, resolved the same way
  `AgentSpec.tools` is today (`tools.REGISTRY` + `extra_tools` + knowledge
  bases).

`AgentSpec` gains one new field:

```python
skills: List[str] = []
```

Unlike `display_name`/`friendly_description` (wizard-only fields stripped by
`to_raw()`), `skills` is a **real loader-level field** — it stays in
`to_raw()`'s output, so YAML/CLI users can write `skills: [...]` directly,
not just wizard users.

### 2. Resolution — `_build_workflow(..., extra_skills=...)`

`core/loader.py::_build_workflow` gains an optional parameter
`extra_skills: Optional[Dict[str, SkillSpec]] = None`, mirroring the existing
`extra_tools: Optional[Dict[str, Callable]] = None`. Default `None` (treated
as `{}`) — fully backward compatible; workflows that don't use `skills:`
behave exactly as before.

For each agent spec, before the existing tool-resolution step:

1. `skill_names = spec.pop("skills", []) or []`
2. For each name, look up the corresponding `SkillSpec` in `extra_skills`.
   If not found, raise the same shape of error as unknown tools:
   `f"Unknown skill '{name}'. Available skills: {available_skills}"`.
3. **Merge tools**: append each resolved skill's `tools` list, in `skills:`
   order, to the agent's own `tools` list, **de-duplicating** while
   preserving first-occurrence order (the agent's own `tools:` entries stay
   first). Resolve the merged name list through the existing `tool_lookup`
   (`REGISTRY` + `extra_tools` + knowledge bases) exactly as today — an
   unresolvable tool name (whether from the agent or from a skill) raises the
   existing `f"Unknown tool '{name}'. Available tools: {available}"` error,
   unchanged.
4. **Merge backstory**: concatenate the agent's own `backstory` with each
   resolved skill's `instructions`, in `skills:` order, joined by `"\n\n"`:
   ```python
   backstory = "\n\n".join(
       [spec.get("backstory", "")] + [s.instructions for s in resolved_skills]
   ).strip()
   ```
   No added headings/separators — `instructions` text is expected to be
   self-contained.
5. Construct `Agent(**spec, tools=<resolved tools>, backstory=<merged
   backstory>)` as before. The `Agent` dataclass itself is unchanged — Skills
   are purely a loader-time expansion into existing `tools`/`backstory`
   fields.

### 3. Testing

Added to `tests/test_specification.py` (alongside the existing
`validate_specification`/`_basic_spec()` tests):

- An agent with `skills: ["research_skill"]` and
  `extra_skills={"research_skill": SkillSpec(tools=["calculator"],
  instructions="...")}` produces an `Agent` whose `tools` include
  `calculator` and whose `backstory` includes the skill's instructions.
- Tool de-duplication: agent already has `tools: ["calculator"]`; the
  referenced skill also lists `calculator` — the resulting tool list contains
  it once.
- Multiple skills: backstory segments appear in `skills:` list order.
- Unknown skill name raises `ConfigurationError` matching `"Unknown skill"`
  (mirrors `test_validate_specification_rejects_unknown_tool`).
- A skill referencing an unresolvable tool name still raises the existing
  `"Unknown tool"` error.
- Backward compatibility (no `skills:`/`extra_skills`) is covered by the
  existing test suite continuing to pass unchanged.

### 4. Documentation

- `src/bestteam/core/CLAUDE.md`: document `SkillSpec`, `AgentSpec.skills`,
  and the `extra_skills` merge rules (tool de-dup, backstory concatenation
  order, error message shapes), in the same style as the existing
  `tools:`/`REGISTRY`/`extra_tools` description in
  `src/bestteam/tools/CLAUDE.md`.
- `docs/STATUS.md`: add sub-project 2 (persistent Skills library —
  `SkillRecord` table, `/api/config/skills` CRUD, Solution Architect
  auto-assignment, frontend picker) to "Next steps / roadmap", noting it
  builds on this spec's `SkillSpec`/`extra_skills` primitive.

## Out of scope / deferred

- Persistent Skills library, CRUD API, frontend management UI, Solution
  Architect integration — sub-project 2 (roadmap item).
- Workflow-local inline `skills:` YAML sections (defining skills directly in
  a workflow file) — not needed while skills are supplied externally via
  `extra_skills`; could be added later if a self-contained-workflow use case
  arises.
- Skills with their own executable code/logic beyond referencing existing
  tools — out of scope; would touch the tool trust-boundary model in
  `src/bestteam/tools/CLAUDE.md`.
