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

- **Status**: **Superseded** (2026-07-16) by "Deployment: org-scoped
  multi-tenancy, one codebase for both models" below.
- **Context**: bestteam needs a deployment model for delivering the Team
  Builder + monitoring UI to customers.
- **Decision (original)**: bestteam ships as **one independent instance per
  customer** (Docker Compose, its own SQLite database), not a shared
  multi-tenant SaaS.
- **Why superseded**: the business now anticipates one shared hosted
  platform serving several customer organisations (one or more accounts per
  customer org, each org on its own external services). A shared instance
  under the original model had zero cross-customer isolation.

## Deployment: org-scoped multi-tenancy, one codebase for both models

- **Status**: Accepted (2026-07-16)
- **Context**: several customer organisations should be servable from one
  hosted deployment, with multiple employee accounts per org, without
  giving up the existing per-customer-instance option.
- **Decision**: row-level multi-tenancy via an `organizations` table and
  `org_id` columns on every org-owned resource (users, agents, teams,
  knowledge bases, skills, workflows, builder sessions, runs, usage), with
  API-layer scoping through a `get_current_org` dependency. **The same code
  serves both deployment models** — a per-customer instance is simply a
  deployment with one org (the migration backfills `default`), a shared
  platform is one with many.
- **Consequences**:
  - Public registration is removed; orgs and accounts are provisioned by
    the platform operator via the `ui.backend.admin` CLI. Platform
    operators are org-NULL users; org users never see another org's data
    (cross-org access is 404 — existence is not revealed).
  - Component names are unique per `(org_id, name)`, not globally; skills
    have a platform tier (`org_id IS NULL` = built-ins visible to all).
  - Isolation is enforced in the API layer (central loaders + dependency),
    not the database engine; Postgres row-level security can be layered
    onto the same `org_id` columns later if the DB moves off SQLite.
  - Per-org secrets were NOT part of this decision. Per-org **email**
    credentials are now implemented (encrypted secrets store + `admin
    set-email`; spec `2026-07-18-per-org-email-credentials-design.md`);
    per-org LLM credentials and a self-service settings UI remain future.
    Process-env email credentials still must not be set on a shared instance
    (the env path stays single-org and is refused on multi-org).
  - **Interim: one member per org is enforced at the schema level** — a
    partial unique index on `users.org_id WHERE org_id IS NOT NULL`
    (migration `e1f2a3b4c5d6`), with `create_user` doing a friendly
    pre-check. The architecture allows multiple accounts per org, but
    org-scoped resources — notably the self-service shared mailbox — have no
    per-member privilege separation yet: any member can
    connect/redirect/disconnect them. Until a per-org admin role exists, a
    second member would mean unprivileged co-management of the org's mailbox,
    so the invariant is enforced (not merely assumed) and a race or a bypass
    of `create_user` still can't create one. A database upgraded from the
    earlier multi-member architecture is not mutated automatically: the
    migration **refuses and names the offending orgs** so the operator
    resolves duplicates by hand (accounts are never auto-deleted). Lifting the
    constraint is gated on the per-org admin role (deferred sub-project C).
    Platform operators (org-NULL) are exempt — there can be several.
  - See `docs/superpowers/specs/2026-07-15-org-multi-tenancy-design.md`.

## Memory: SQLite + BM25 in-house, not the mem0 library

- **Status**: Accepted
- **Context**: The platform needed per-user memory so runs can recall a
  user's preferences/history across sessions (working / episodic / semantic /
  procedural). `mem0` was evaluated as an off-the-shelf option.
- **Decision**: Implement memory in-house on **stdlib `sqlite3` + BM25**
  (reusing the CJK-aware tokenizer already shared with the knowledge base),
  behind the existing `Memory` ABC — **no `mem0` dependency**.
- **Reasons**:
  - Matches the project's established "no vector store, no extra service, own
    SQLite file" posture (same stack as `local_folder` knowledge bases), so it
    deploys with zero new infrastructure.
  - Default path is **$0 and offline** (episodic recall needs no LLM); richer
    semantic/procedural extraction is opt-in via `BESTTEAM_MEMORY_MODEL`.
  - mem0 would pull in a vector store + per-run LLM extraction calls,
    contradicting the zero-infra default.
- **Consequences**:
  - The `Memory` ABC keeps the store swappable — a `Mem0Memory(Memory)` (or
    Redis/Postgres-backed) implementation can drop in later with **no changes
    to agents, the adapter, or the API**.
  - Recall is single-stage BM25 (no rerank/expansion) and semantic/procedural
    records aren't auto-deduped — accepted trade-offs for the in-house MVP,
    tracked in `STATUS.md`.
