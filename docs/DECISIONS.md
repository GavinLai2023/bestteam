# Technical decision log

This file records **why** significant decisions were made — not **what**
the current behavior is (that's what the `CLAUDE.md` files are for). The
goal is to stop future sessions (human or Claude) from re-litigating
settled questions or "fixing" something that was a deliberate trade-off.

Append new entries at the bottom using this template:

```markdown
## <Short title>

- **Status**: Accepted / Superseded by <link>
- **Context**: what problem or question prompted this decision
- **Decision**: what was decided
- **Consequences**: what this enables, what it rules out or defers
```

---

## Engine: LangGraph (not CrewAI) as the orchestration engine

- **Status**: Accepted
- **Context**: bestteam needs an engine to execute `Agent`/`Team`/`Workflow`
  under the SEQUENTIAL/PARALLEL/HIERARCHICAL collaboration modes, with
  tool-calling, structured outputs, and streaming trace events for the
  monitoring dashboard. LangGraph and CrewAI were both viable candidates.
- **Decision**: LangGraph was chosen as the engine from the project's first
  commit.
- **Reasons**:
  - LangGraph's graph/state-machine model maps directly onto the
    `Agent`/`Team`/`Workflow` design and its collaboration modes — a team's
    "who talks to whom, in what order" is naturally a graph, which fits
    SEQUENTIAL/PARALLEL/HIERARCHICAL better than CrewAI's crew/task
    abstraction.
  - bestteam already builds on `langchain_core` for model specs, tools, and
    `with_structured_output`. LangGraph is a natural extension of that same
    stack, rather than adopting a second framework's conventions on top.
- **Consequences**: The `EngineAdapter` ABC (`src/bestteam/adapters/base.py`)
  keeps all LangGraph-specific code inside `LangGraphAdapter`, so a CrewAI
  (or other) adapter could be added later behind the same public API. This
  is currently **unimplemented and not prioritized** — see `STATUS.md`.

## Deployment: per-customer instance, no multi-tenancy

- **Status**: Accepted
- **Context**: bestteam needs a deployment model for delivering the Team
  Builder + monitoring UI to customers.
- **Decision**: bestteam ships as **one independent instance per customer**
  (Docker Compose, its own SQLite database), not a shared multi-tenant SaaS.
- **Consequences**:
  - The `users` table (`ui/backend/db/users.py`) has no `tenant_id` — it's
    "a handful of users sharing one deployment", not cross-customer
    isolation.
  - Each customer's config, builder sessions, run history, and usage records
    live in their own SQLite file (`bestteam_data` volume — see
    `deployment.md`).
  - Distributing code updates *across* many customer deployments is a
    separate, deferred concern (Phase 6 in `team_builder_methodology.md`),
    not something app-level multi-tenancy would solve anyway.
