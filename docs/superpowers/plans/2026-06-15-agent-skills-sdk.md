# Agent Skills SDK Primitive (sub-project 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `SkillSpec` model and `AgentSpec.skills` field, and have the
loader merge a referenced Skill's `tools` and `instructions` into the
referencing agent's `tools`/`backstory` at build time.

**Architecture:** `SkillSpec` (name/description/instructions/tools) lives in
`src/bestteam/core/specification.py` alongside the other `*Spec` models.
`AgentSpec.skills: List[str]` is a real loader-level field (kept by
`to_raw()`). `core/loader.py::_build_workflow` gains an optional
`extra_skills: Dict[str, SkillSpec]` parameter (mirrors `extra_tools`);
`_build_agent` resolves each agent's `skills:` names against it, merges the
skill's `tools` into the agent's own `tools` (de-duplicated, agent's tools
first) and appends the skill's `instructions` to the agent's `backstory`
(joined by `"\n\n"`, in `skills:` order). `validate_specification()`,
`generate_specification()`, and `load_workflow()` all accept and pass through
the same `extra_skills`/`skills` data so YAML, Specification, and
programmatic callers behave identically.

**Tech Stack:** Python, Pydantic v2, pytest. No new dependencies.

---

## File Structure

- **Modify `src/bestteam/core/specification.py`** — add `SkillSpec` model,
  `AgentSpec.skills` field + `to_raw()` support, `extra_skills` params on
  `validate_specification`/`generate_specification`.
- **Modify `src/bestteam/core/loader.py`** — add `extra_skills` param to
  `_build_workflow`, rewrite `_build_agent` to resolve/merge skills, add
  `skills` param to `load_workflow`.
- **Modify `src/bestteam/__init__.py`** — export `SkillSpec`.
- **Modify `tests/test_specification.py`** — tests for `SkillSpec`,
  `AgentSpec.skills`/`to_raw()`, and `extra_skills` resolution/merge/errors.
- **Modify `tests/test_tools.py`** — test for `load_workflow(..., skills=...)`.
- **Modify `src/bestteam/core/CLAUDE.md`** — document `SkillSpec`,
  `AgentSpec.skills`, and the `extra_skills` merge rules.
- **Modify `docs/STATUS.md`** — add sub-project 2 (persistent Skills library)
  to the roadmap.

---

### Task 1: `SkillSpec` model + `AgentSpec.skills` field

**Files:**
- Modify: `src/bestteam/core/specification.py:45-67` (AgentSpec, new SkillSpec class)
- Modify: `src/bestteam/__init__.py:6-14,25-55` (export SkillSpec)
- Test: `tests/test_specification.py`

- [ ] **Step 1: Write the failing tests**

In `tests/test_specification.py`, update the `from bestteam import (...)` block
(lines 8-16) to add `SkillSpec`:

```python
from bestteam import (
    AgentSpec,
    KnowledgeBaseSpec,
    Specification,
    SkillSpec,
    TeamSpec,
    WorkflowSpec,
    generate_specification,
    validate_specification,
)
```

Then add these three new tests immediately after
`test_to_raw_strips_friendly_fields_and_matches_loader_shape` (after line 91,
before `test_validate_specification_accepts_a_valid_spec_and_runs`):

```python
def test_skill_spec_round_trips_basic_fields():
    skill = SkillSpec(
        name="research_skill",
        description="Research helper",
        instructions="Use the calculator for math.",
        tools=["calculator"],
    )

    assert skill.name == "research_skill"
    assert skill.description == "Research helper"
    assert skill.instructions == "Use the calculator for math."
    assert skill.tools == ["calculator"]


def test_agent_spec_to_raw_omits_skills_when_empty():
    spec = AgentSpec(
        name="support_agent",
        role="Customer Support Specialist",
        goal="Answer customer questions",
        model="fake:hello",
    )

    assert "skills" not in spec.to_raw()


def test_agent_spec_to_raw_includes_skills_when_set():
    spec = AgentSpec(
        name="support_agent",
        role="Customer Support Specialist",
        goal="Answer customer questions",
        model="fake:hello",
        skills=["research_skill"],
    )

    assert spec.to_raw()["skills"] == ["research_skill"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_specification.py -v -k "skill"`
Expected: FAIL/ERROR — collection error, `ImportError: cannot import name 'SkillSpec' from 'bestteam'`

- [ ] **Step 3: Implement `AgentSpec.skills` + `SkillSpec` in specification.py**

In `src/bestteam/core/specification.py`, replace the `AgentSpec` class
(lines 45-67) with:

```python
class AgentSpec(BaseModel):
    """Mirrors an `agents:` entry in the loader's raw dict (see `core/loader.py`).

    `display_name`/`friendly_description` are wizard-only presentation fields
    -- `to_raw()` strips them before handing the spec to `_build_workflow`.
    """

    name: str
    role: str
    goal: str
    backstory: str = ""
    model: str
    tools: List[str] = Field(default_factory=list)
    skills: List[str] = Field(default_factory=list)
    display_name: Optional[str] = None
    friendly_description: Optional[str] = None

    def to_raw(self) -> Dict[str, Any]:
        raw: Dict[str, Any] = {"name": self.name, "role": self.role, "goal": self.goal, "model": self.model}
        if self.backstory:
            raw["backstory"] = self.backstory
        if self.tools:
            raw["tools"] = list(self.tools)
        if self.skills:
            raw["skills"] = list(self.skills)
        return raw


class SkillSpec(BaseModel):
    """A reusable instruction document plus the tools it depends on.

    Referenced by name from `AgentSpec.skills` and resolved against
    `extra_skills` in `core/loader.py::_build_workflow` -- the skill's
    `instructions` are appended to the referencing agent's `backstory` and
    its `tools` are merged into the agent's `tools`.
    """

    name: str
    description: str = ""
    instructions: str
    tools: List[str] = Field(default_factory=list)
```

- [ ] **Step 4: Export `SkillSpec` from `src/bestteam/__init__.py`**

In `src/bestteam/__init__.py`, update the `from .core.specification import (...)`
block (lines 6-14):

```python
from .core.specification import (
    AgentSpec,
    KnowledgeBaseSpec,
    Specification,
    SkillSpec,
    TeamSpec,
    WorkflowSpec,
    generate_specification,
    validate_specification,
)
```

And add `"SkillSpec"` to `__all__` (after `"Specification"`, line 40):

```python
    "Specification",
    "SkillSpec",
    "AgentSpec",
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_specification.py -v`
Expected: PASS — all tests pass, including the 3 new ones

- [ ] **Step 6: Commit**

```bash
git add src/bestteam/core/specification.py src/bestteam/__init__.py tests/test_specification.py
git commit -m "feat: add SkillSpec model and AgentSpec.skills field"
```

---

### Task 2: `extra_skills` resolution in the loader

**Files:**
- Modify: `src/bestteam/core/loader.py:1-19,60-83,118-129` (imports, `_build_workflow`, `_build_agent`)
- Modify: `src/bestteam/core/specification.py:156-216` (`validate_specification`, `generate_specification`)
- Test: `tests/test_specification.py`

- [ ] **Step 1: Write the failing tests**

In `tests/test_specification.py`, update the `from bestteam import (...)` block
again to add `calculator`:

```python
from bestteam import (
    AgentSpec,
    KnowledgeBaseSpec,
    Specification,
    SkillSpec,
    TeamSpec,
    WorkflowSpec,
    calculator,
    generate_specification,
    validate_specification,
)
```

Update the `_basic_spec()` helper (lines 46-71) to accept `agent_skills`:

```python
def _basic_spec(
    *, mode: str = "sequential", agent_tools=None, agent_skills=None, manager: str | None = None
) -> Specification:
    return Specification(
        name="support_workflow",
        agents=[
            AgentSpec(
                name="support_agent",
                role="Customer Support Specialist",
                goal="Answer customer questions",
                model="fake:hello",
                tools=agent_tools or [],
                skills=agent_skills or [],
                display_name="Support Specialist",
                friendly_description="Answers customer questions.",
            )
        ],
        teams=[
            TeamSpec(
                name="support_team",
                agents=["support_agent"],
                mode=mode,
                manager=manager,
                display_name="Support Team",
                friendly_description="The support specialist handles every request.",
            )
        ],
        workflow=WorkflowSpec(steps=["support_team"]),
    )
```

Then add these five new tests after `test_validate_specification_rejects_unknown_tool`
(after line 106, before `test_validate_specification_rejects_hierarchical_team_without_manager`):

```python
def test_validate_specification_resolves_skill_into_tools_and_backstory(tmp_path):
    spec = _basic_spec(agent_skills=["research_skill"])
    extra_skills = {
        "research_skill": SkillSpec(
            name="research_skill",
            instructions="Use the calculator for any math in the customer's question.",
            tools=["calculator"],
        )
    }

    workflow = validate_specification(spec, source=tmp_path / "workflow.yaml", extra_skills=extra_skills)
    agent = workflow.steps[0].agents[0]

    assert calculator in agent.tools
    assert "Use the calculator for any math in the customer's question." in agent.backstory


def test_validate_specification_dedupes_skill_tools_with_agent_tools(tmp_path):
    spec = _basic_spec(agent_tools=["calculator"], agent_skills=["research_skill"])
    extra_skills = {
        "research_skill": SkillSpec(
            name="research_skill",
            instructions="Use the calculator for any math in the customer's question.",
            tools=["calculator"],
        )
    }

    workflow = validate_specification(spec, source=tmp_path / "workflow.yaml", extra_skills=extra_skills)
    agent = workflow.steps[0].agents[0]

    assert agent.tools.count(calculator) == 1


def test_validate_specification_concatenates_multiple_skill_instructions_in_order(tmp_path):
    spec = _basic_spec(agent_skills=["skill_a", "skill_b"])
    extra_skills = {
        "skill_a": SkillSpec(name="skill_a", instructions="Follow step A first."),
        "skill_b": SkillSpec(name="skill_b", instructions="Then follow step B."),
    }

    workflow = validate_specification(spec, source=tmp_path / "workflow.yaml", extra_skills=extra_skills)
    agent = workflow.steps[0].agents[0]

    assert agent.backstory.index("Follow step A first.") < agent.backstory.index("Then follow step B.")


def test_validate_specification_rejects_unknown_skill(tmp_path):
    spec = _basic_spec(agent_skills=["does_not_exist"])

    with pytest.raises(ConfigurationError, match="Unknown skill"):
        validate_specification(spec, source=tmp_path / "workflow.yaml")


def test_validate_specification_rejects_skill_with_unknown_tool(tmp_path):
    spec = _basic_spec(agent_skills=["research_skill"])
    extra_skills = {
        "research_skill": SkillSpec(
            name="research_skill",
            instructions="Use a tool that doesn't exist.",
            tools=["does_not_exist"],
        )
    }

    with pytest.raises(ConfigurationError, match="Unknown tool"):
        validate_specification(spec, source=tmp_path / "workflow.yaml", extra_skills=extra_skills)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_specification.py -v -k "skill"`
Expected: FAIL — `TypeError: validate_specification() got an unexpected keyword argument 'extra_skills'`

- [ ] **Step 3: Implement `extra_skills` resolution in loader.py**

In `src/bestteam/core/loader.py`, update the imports (line 4):

```python
from typing import Any, Dict, Optional
```

Replace `_build_workflow` (lines 60-83) with:

```python
def _build_workflow(
    raw: Dict[str, Any], *, source: Path, extra_tools: Dict[str, Any], extra_skills: Optional[Dict[str, Any]] = None
) -> Workflow:
    tool_lookup = {**_TOOL_REGISTRY, **extra_tools}
    skill_lookup = extra_skills or {}
    for spec in raw.get("knowledge_bases", []):
        kb = _build_knowledge_base(spec, source)
        tool_lookup[kb.name] = make_knowledge_base_tool(kb)

    agents = {spec["name"]: _build_agent(spec, tool_lookup, skill_lookup) for spec in raw.get("agents", [])}

    teams: Dict[str, Team] = {}
    for spec in raw.get("teams", []):
        team_name = spec["name"]
        team_agents = [_lookup(agents, name, "agent", team_name) for name in spec["agents"]]
        manager = _lookup(agents, spec["manager"], "agent", team_name) if "manager" in spec else None
        teams[team_name] = Team(
            name=team_name,
            agents=team_agents,
            mode=_parse_mode(spec.get("mode", "sequential"), team_name),
            manager=manager,
        )

    workflow_spec = raw.get("workflow", {})
    steps = [_lookup(teams, name, "team", "workflow") for name in workflow_spec.get("steps", [])]

    return Workflow(name=raw.get("name", source.stem), steps=steps)
```

Replace `_build_agent` (lines 118-129) with:

```python
def _build_agent(spec: Dict[str, Any], tool_lookup: Dict[str, Any], skill_lookup: Dict[str, Any]) -> Agent:
    spec = dict(spec)

    skill_names = spec.pop("skills", []) or []
    resolved_skills = []
    for name in skill_names:
        if name not in skill_lookup:
            available = ", ".join(sorted(skill_lookup))
            raise ConfigurationError(
                f"Unknown skill '{name}'. Available skills: {available}"
            )
        resolved_skills.append(skill_lookup[name])

    raw_tools = list(spec.pop("tools", []) or [])
    for skill in resolved_skills:
        for name in skill.tools:
            if name not in raw_tools:
                raw_tools.append(name)

    tools = []
    for name in raw_tools:
        if name not in tool_lookup:
            available = ", ".join(sorted(tool_lookup))
            raise ConfigurationError(
                f"Unknown tool '{name}'. Available tools: {available}"
            )
        tools.append(tool_lookup[name])

    spec["backstory"] = "\n\n".join(
        [spec.get("backstory", "")] + [skill.instructions for skill in resolved_skills]
    ).strip()

    return Agent(**spec, tools=tools)
```

- [ ] **Step 4: Pass `extra_skills` through `validate_specification`/`generate_specification`**

In `src/bestteam/core/specification.py`, replace `validate_specification`
(lines 156-169) with:

```python
def validate_specification(
    spec: Specification,
    *,
    source: Path,
    extra_tools: Optional[Dict[str, Any]] = None,
    extra_skills: Optional[Dict[str, Any]] = None,
) -> Workflow:
    """Compile a Specification through the same pipeline as a YAML workflow file.

    Returns the resulting `Workflow` if the spec is valid. Raises
    `ConfigurationError` with a message suitable for showing to a customer or
    feeding back to the Solution Architect agent for self-correction.
    """
    raw = spec.to_raw()
    try:
        return _build_workflow(raw, source=source, extra_tools=extra_tools or {}, extra_skills=extra_skills or {})
    except (KeyError, TypeError) as exc:
        raise ConfigurationError(f"Specification is malformed: missing or invalid field {exc}") from exc
```

Replace `generate_specification` (lines 172-216) with:

```python
def generate_specification(
    model: BaseChatModel,
    requirements: str,
    *,
    source: Path,
    extra_tools: Optional[Dict[str, Any]] = None,
    extra_skills: Optional[Dict[str, Any]] = None,
    max_attempts: int = 3,
) -> Specification:
    """Generate a Specification from Requirements, self-correcting on validation errors.

    Calls `model.with_structured_output(Specification)` to get a candidate
    design, then validates it via `validate_specification`. If validation
    fails, the `ConfigurationError` is turned into feedback appended to the
    conversation and the architect tries again, up to `max_attempts` times.
    """
    structured_model = model.with_structured_output(Specification)

    messages: List[BaseMessage] = [
        SystemMessage(content=_ARCHITECT_SYSTEM_PROMPT),
        HumanMessage(content=requirements),
    ]

    last_error: Optional[ConfigurationError] = None
    for _ in range(max_attempts):
        result = structured_model.invoke(messages)
        spec = result if isinstance(result, Specification) else Specification.model_validate(result)
        try:
            validate_specification(spec, source=source, extra_tools=extra_tools, extra_skills=extra_skills)
            return spec
        except ConfigurationError as exc:
            last_error = exc
            messages.append(AIMessage(content=spec.model_dump_json()))
            messages.append(
                HumanMessage(
                    content=(
                        f"That design doesn't work yet: {exc} "
                        "Please revise the specification to fix this issue."
                    )
                )
            )

    raise ConfigurationError(
        f"Solution Architect could not produce a valid specification after "
        f"{max_attempts} attempts. Last error: {last_error}"
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_specification.py -v`
Expected: PASS — all tests pass, including the 5 new ones

- [ ] **Step 6: Run the full test suite to check for regressions**

Run: `.\.venv\Scripts\python.exe -m pytest`
Expected: PASS — no regressions (existing `_build_agent`/`_build_workflow` callers in
`ui/backend/crud.py` pass `extra_skills` implicitly as `None`, which defaults to `{}`)

- [ ] **Step 7: Commit**

```bash
git add src/bestteam/core/loader.py src/bestteam/core/specification.py tests/test_specification.py
git commit -m "feat: resolve AgentSpec.skills into tools and backstory via extra_skills"
```

---

### Task 3: `load_workflow(..., skills=...)` parameter

**Files:**
- Modify: `src/bestteam/core/loader.py:22-57` (`load_workflow`)
- Test: `tests/test_tools.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_tools.py`, add this test after `test_loader_resolves_custom_toolkit_tool`
(after line 397, before `test_loader_custom_tool_appears_in_error_message`):

```python
def test_loader_resolves_skill_via_skills_param(tmp_path):
    from bestteam import SkillSpec, load_workflow

    yaml_text = """
name: skill_test
agents:
  - name: cruncher
    role: Number Cruncher
    goal: Crunch numbers
    model: "fake:42"
    skills: [research_skill]
teams:
  - name: math_team
    agents: [cruncher]
    mode: sequential
workflow:
  steps: [math_team]
"""
    p = tmp_path / "skill_test.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    research_skill = SkillSpec(
        name="research_skill",
        instructions="Use the calculator for any math.",
        tools=["calculator"],
    )
    wf = load_workflow(str(p), skills=[research_skill])
    agent = wf.steps[0].agents[0]
    assert agent.tools[0] is calculator
    assert "Use the calculator for any math." in agent.backstory
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_tools.py -v -k test_loader_resolves_skill_via_skills_param`
Expected: FAIL — `TypeError: load_workflow() got an unexpected keyword argument 'skills'`

- [ ] **Step 3: Implement the `skills` parameter**

In `src/bestteam/core/loader.py`, replace `load_workflow` (lines 22-57) with:

```python
def load_workflow(path, *, toolkits=None, skills=None) -> Workflow:
    """Build a Workflow from a declarative YAML file.

    This is what lets customers define agents/teams/pipelines without writing
    any orchestration code — the CLI's `run`/`graph` commands are thin
    wrappers around this loader plus Workflow.run()/.visualize().

    Args:
        path: Path to the YAML workflow file.
        toolkits: Optional list of ToolKit instances whose tools are made
            available to agents defined in this workflow. Custom tools are
            merged with the built-in REGISTRY and can be referenced by name
            in the YAML ``tools:`` list.
        skills: Optional list of SkillSpec instances that agents in this
            workflow can reference by name via ``skills:`` in their
            ``agents:`` entry. Looked up by ``.name``.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigurationError(f"Workflow file not found: {path}") from exc

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"Could not parse '{path}' as YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigurationError(f"'{path}' must contain a YAML mapping at the top level")

    extra_tools: Dict[str, Any] = {}
    for tk in (toolkits or []):
        extra_tools.update(tk.items())

    extra_skills: Dict[str, Any] = {s.name: s for s in (skills or [])}

    try:
        return _build_workflow(raw, source=path, extra_tools=extra_tools, extra_skills=extra_skills)
    except (KeyError, TypeError) as exc:
        raise ConfigurationError(f"Malformed workflow config in '{path}': missing or invalid field {exc}") from exc
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_tools.py -v -k test_loader_resolves_skill_via_skills_param`
Expected: PASS

- [ ] **Step 5: Run the full test suite to check for regressions**

Run: `.\.venv\Scripts\python.exe -m pytest`
Expected: PASS — no regressions

- [ ] **Step 6: Commit**

```bash
git add src/bestteam/core/loader.py tests/test_tools.py
git commit -m "feat: add skills parameter to load_workflow"
```

---

### Task 4: Documentation updates

**Files:**
- Modify: `src/bestteam/core/CLAUDE.md`
- Modify: `docs/STATUS.md`

- [ ] **Step 1: Document `SkillSpec`/`extra_skills` in `src/bestteam/core/CLAUDE.md`**

Insert a new section after the existing "## Specification and Requirements"
section (after its last paragraph, before "## Knowledge bases"):

```markdown
## Skills (`SkillSpec`, `AgentSpec.skills`)

`SkillSpec` (`core/specification.py`) is a reusable instruction document plus
the tools it depends on: `{name, description, instructions, tools}`. An
`AgentSpec` can reference skills by name via `skills: List[str]` -- a real
loader-level field (unlike `display_name`/`friendly_description`, `to_raw()`
keeps it).

`core/loader.py::_build_workflow` resolves `skills:` via an optional
`extra_skills: Dict[str, SkillSpec]` parameter (mirrors `extra_tools`;
`load_workflow(..., skills=[...])` builds it by `.name`). For each agent:

- Each skill name is looked up in `extra_skills`; an unknown name raises
  `ConfigurationError("Unknown skill '<name>'. Available skills: <...>")`.
- The skill's `tools` are appended to the agent's own `tools` (agent's tools
  first), de-duplicated preserving order, then resolved through the same
  `tool_lookup` as ordinary `tools:` -- an unresolvable name raises the
  existing `"Unknown tool '<name>'. Available tools: <...>"` error.
- The skill's `instructions` are appended to the agent's `backstory`, one per
  skill in `skills:` order, joined by `"\n\n"`.

`validate_specification()`/`generate_specification()` accept the same
`extra_skills` parameter, passed through to `_build_workflow()`.
```

- [ ] **Step 2: Add sub-project 2 to the roadmap in `docs/STATUS.md`**

In `docs/STATUS.md`, add a bullet to "## Next steps / roadmap" (after the
existing bullets, end of file):

```markdown
- Sub-project 2: persistent Skills library (`SkillRecord` table,
  `/api/config/skills` CRUD, Solution Architect auto-assignment, frontend
  picker) -- builds on the `SkillSpec`/`AgentSpec.skills`/`extra_skills`
  primitive (see `docs/superpowers/specs/2026-06-15-agent-skills-design.md`).
```

- [ ] **Step 3: Commit**

```bash
git add src/bestteam/core/CLAUDE.md docs/STATUS.md
git commit -m "docs: document SkillSpec/extra_skills and add sub-project 2 to roadmap"
```
