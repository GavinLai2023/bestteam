# Data Architecture Review Report

**Project:** bestteam  
**Date:** 22 July 2026  
**Scope:** Phase 1 — correctness and data integrity; Phase 2 — commercial multi-tenant readiness.

## Executive assessment

The architecture is suitable for a small, single-process MVP, but it is not yet a durable commercial data model.

The central design—treating a Workflow as the deployable aggregate containing inline Agents and internal Teams—is sound. The main weakness is that most relationships are resolved through mutable names inside JSON rather than stable IDs and immutable versions.

The highest-priority risks are:

1. Deployed AI Teams are not versioned and can change without being redeployed.
2. Runs cannot identify the exact configuration and dependencies they executed.
3. Memory is keyed by username without organization scope.
4. Workflow lifecycle states are not enforced.
5. SQLite foreign keys are declared but not enabled.
6. Runtime history remains dependent on an in-memory registry.
7. The account model supports only one user per organization.
8. Models, tools, skills, connectors and KBs lack consistent organization-level availability and version policies.

## Domain terminology

The current product concepts map as follows:

| Product concept | Current implementation | Assessment |
|---|---|---|
| AI Team | `WorkflowRecord` after deployment | Should become the explicit product aggregate |
| AI Team draft | `BuilderSession.specification_json` | Appropriate, but needs an explicit AI Team/version relationship |
| Workflow | Deployable configuration containing Agents and internal Teams | Good aggregate boundary |
| Team | SDK collaboration group inside a Workflow | Conflicts with customer-facing “AI Team” terminology |
| Agent | Inline `AgentSpec` inside Workflow JSON | Appropriate unless reusable Agent templates are introduced |
| Skill | Standalone `SkillRecord`, referenced by name | Needs immutable versions |
| Knowledge Base | Standalone or inline KB configuration | Two competing ownership models |
| Tool | Python registry function, email-scoped function or KB adapter | Multiple resource types share one name namespace |
| Model Catalog | Global model metadata and pricing | Not an authoritative availability/configuration model |
| Memory | Separate SQLite store keyed by username | Insufficient tenant and lifecycle boundaries |

---

# Phase 1 — Correctness and data integrity

These problems affect correctness, reproducibility or internal consistency even at the current scale.

## P1-01 — AI Team identity is ambiguous

**Severity:** High  
**Type:** Domain-model defect

The UI calls Builder Sessions “My teams”, while the runnable object is `WorkflowRecord`. SDK `Team` means something different again.

Consequences:

- A draft session can be mistaken for a deployed AI Team.
- Customer-facing identity depends on `specification_json.name`.
- Product and SDK terminology communicate different object boundaries.

**Recommendation:** Introduce an explicit `AITeam` aggregate. Treat BuilderSession as an editing session and SDK Team as an internal collaboration group.

## P1-02 — Multiple Builder Sessions can represent the same deployed object

**Severity:** High  
**Type:** Current correctness risk

Deployment upserts a Workflow by `(org_id, specification.name)`. Two sessions with the same name therefore overwrite the same Workflow, while “My teams” can display both sessions as separate teams.

Evidence: [`ui/backend/builder.py`](../ui/backend/builder.py).

**Recommendation:** Give AI Teams stable IDs. BuilderSession should carry `ai_team_id`; deployment should create a new version under that ID.

## P1-03 — No immutable Workflow versions

**Severity:** High  
**Type:** Reproducibility defect

`WorkflowRecord.config` is overwritten in place. There is no published-version history, rollback point or immutable execution snapshot.

Consequences:

- Previous deployed configurations are lost.
- Existing Runs cannot be reproduced.
- Rollback requires reconstructing old JSON externally.
- Editing through Advanced config immediately changes future executions.

**Recommendation:** Add `ai_team_versions` or `workflow_versions`, with immutable configuration snapshots and `current_published_version_id`.

## P1-04 — Dependencies are soft name references

**Severity:** High  
**Type:** Referential-integrity defect

Agents reference:

- Skills by name.
- KBs through the heterogeneous `tools` list.
- Tools by function name.
- Models by spec string.
- Teams and Agents by name inside Workflow JSON.

The database cannot enforce these relationships or efficiently answer “what depends on this resource?”

Evidence: [`src/bestteam/core/loader.py`](../src/bestteam/core/loader.py).

**Recommendation:** Add typed dependency records containing stable resource IDs and optional resource-version IDs.

## P1-05 — Deployed behavior can change without redeployment

**Severity:** High  
**Type:** Behavioral-drift defect

A deployed Workflow stores Skill names, not Skill content or versions. Editing a Skill changes every deployed Workflow that uses it after cache invalidation. KBs similarly follow their current content.

**Recommendation:**

- Pin Skills to immutable versions.
- Let KB dependencies explicitly choose `pinned_version` or `follow_latest`.
- Record the resolved dependencies on every Run.

## P1-06 — Workflow lifecycle status is decorative

**Severity:** High  
**Type:** Lifecycle defect

`WorkflowRecord.status` is an unrestricted string. `_get_workflow()` does not require `deployed`, and `/api/workflows` lists all Workflow records.

Evidence: [`ui/backend/main.py`](../ui/backend/main.py).

Consequences:

- Draft Workflows may be run as production workloads.
- Invalid state transitions are possible.
- Deployment does not require a successful test run.
- Archived or disabled states are not supported.

**Recommendation:** Use a database enum/check constraint and separate production execution from preview execution.

## P1-07 — Resource deletion can orphan deployed Workflows

**Severity:** High  
**Type:** Referential-integrity defect

Skills and KBs can be deleted without checking whether Workflow configurations reference them. The cache is invalidated, but the affected Workflow then fails the next time it is loaded.

**Recommendation:** Maintain dependency rows and use either:

- `RESTRICT` deletion while published versions depend on a resource, or
- soft deletion/deprecation while preserving referenced versions.

## P1-08 — Tool namespace collisions are possible

**Severity:** Medium–High  
**Type:** Resolution defect

The same namespace contains:

- Built-in tools.
- Organization email tools.
- Standalone KB tools.
- Inline KB tools.
- Skill-injected tool names.

Later sources can shadow earlier sources. A KB could use the name of a built-in tool, and the resulting behavior depends on resolution order.

**Recommendation:** Use typed names such as:

```text
builtin:http_get
connection:email
kb:returns_policy
```

## P1-09 — `AgentRecord` and `TeamRecord` are vestigial

**Severity:** Medium  
**Type:** Schema debt

The tables exist, but the CRUD routes were removed and no runtime loader consumes them.

Evidence: [`ui/backend/db/models.py`](../ui/backend/db/models.py).

Consequences:

- The schema advertises unsupported object types.
- Future developers may accidentally reintroduce a second source of truth.
- Migrations and organization constraints must maintain unused tables.

**Recommendation:** Remove them. If reusable templates are later required, introduce clearly named `AgentTemplate` and `CollaborationTemplate` entities.

## P1-10 — Knowledge Bases have two ownership models

**Severity:** Medium–High  
**Type:** Aggregate-boundary ambiguity

KBs can be:

- Declared inline inside Workflow JSON.
- Stored as standalone `KnowledgeBaseRecord` rows and referenced through Agent tools.

The Builder discourages inline KB creation, but the schema still supports both.

**Recommendation:** Make deployed Workflows reference standalone KB identities. Retain inline KBs only for SDK/YAML compatibility, not as a second backend persistence model.

## P1-11 — Deployment validation is incomplete

**Severity:** High  
**Type:** Validation defect

`_build_workflow()` constructs Agents but does not necessarily resolve and initialize every model during deployment. An arbitrary model spec can therefore survive configuration validation and fail at compile/run time.

A standalone Skill is also validated structurally, but its required tool names are not checked until a Workflow actually uses the Skill.

**Recommendation:** Add a deployment validation phase that resolves:

- Organization model availability.
- Skill versions.
- Skill tool dependencies.
- KB availability/index readiness.
- Connector bindings.
- Collaboration mode support.

External provider calls need not be executed, but all references and capability requirements should be verified.

## P1-12 — JSON configurations lack stored schema versions

**Severity:** Medium  
**Type:** Evolution risk

Workflow, Skill and KB configurations are stored as generic JSON dictionaries. There is no `schema_version`, migration history or explicit compatibility policy.

Consequences:

- Application upgrades must interpret old and new shapes implicitly.
- Pydantic defaults may hide missing historical fields.
- Renamed fields require ad hoc migration logic.
- Queries cannot efficiently inspect embedded dependencies.

**Recommendation:** Add `schema_version` to every persisted configuration and explicit config-migration functions.

## P1-13 — SQLite foreign keys are not enabled

**Severity:** High  
**Type:** Database-integrity defect

The SQLAlchemy schema declares foreign keys, but the SQLite engine does not enable `PRAGMA foreign_keys=ON`.

Evidence: [`ui/backend/db/database.py`](../ui/backend/db/database.py).

Consequences:

- Runs, Usage, Traces, organizations and credentials can become orphaned.
- Declared foreign keys provide documentation but not enforcement.
- Delete behavior is undefined at the database boundary.

**Recommendation:** Audit existing data, enable foreign keys on every connection, and define explicit `CASCADE`, `RESTRICT` or `SET NULL` behavior.

## P1-14 — Deployment is not a single atomic transaction

**Severity:** Medium–High  
**Type:** Transaction-boundary defect

Deployment commits the Workflow record and then updates BuilderSession status separately. Failure between those commits can leave a deployed Workflow with a session still marked as undeployed.

**Recommendation:** Perform AI Team version creation, publication pointer update and BuilderSession transition in one database transaction.

## P1-15 — Runs are not linked to the executed Workflow version

**Severity:** High  
**Type:** Audit defect

A Run stores the Workflow name, input and output. It does not reference an immutable Workflow version. `builder_session_id` exists but is not populated by the normal runtime or Builder test-run path.

Evidence: [`ui/backend/runtime.py`](../ui/backend/runtime.py).

**Recommendation:** Require `workflow_version_id`; optionally add `builder_session_id`, `trigger_id` and `actor_user_id`.

## P1-16 — Trace persistence is unfinished

**Severity:** High  
**Type:** Durability defect

`TraceEventRecord` exists, but events are not written to it. The in-memory `RunRegistry` remains authoritative for event replay and run retrieval.

Evidence: [`ui/backend/registry.py`](../ui/backend/registry.py).

Consequences:

- Restart loses trace history.
- Web/API run retrieval cannot recover completed runs.
- Multiple backend workers cannot share run state.
- Database Run rows and in-memory Run objects may disagree.

**Recommendation:** Persist events with monotonically increasing sequence numbers before or atomically with publication. Use the registry only as a live delivery cache.

## P1-17 — Crash recovery and registry lifecycle are incomplete

**Severity:** High  
**Type:** Operational correctness defect

Current known behaviors include:

- Hard restarts can leave persisted Runs in `running`.
- Terminal Runs are never evicted from the registry.
- There is no startup reconciliation.
- A process crash can separate trigger state, Run state and actual execution.

**Recommendation:** Add startup reconciliation, terminal timestamps, leases/heartbeats and bounded registry retention.

## P1-18 — Names act as mutable public identities

**Severity:** Medium  
**Type:** Identity-design defect

API lookup and dependencies frequently use names rather than IDs. Rename semantics are effectively delete-and-create, which can break triggers, Runs, dependencies and UI links.

**Recommendation:** Use opaque stable IDs internally. Keep names as organization-scoped, editable display identifiers.

---

# Phase 2 — Commercial multi-tenant readiness

These issues become blockers when supporting multiple customers, multiple members per customer, organization-specific providers or higher run volumes.

## P2-01 — One member per organization

**Severity:** High  
**Type:** Commercial blocker

A partial unique index enforces one non-platform user per organization.

Evidence: [`ui/backend/db/models.py`](../ui/backend/db/models.py).

This prevents:

- Team collaboration.
- Separate business owners and operators.
- Read-only access.
- Approval workflows.
- Individual audit attribution within a customer.

**Recommendation:** Replace direct ownership with Organization Membership.

## P2-02 — No Membership or organization RBAC model

**Severity:** High  
**Type:** Authorization blocker

`User.org_id` and platform-wide `is_admin` cannot represent:

- Organization owner.
- Organization admin.
- Builder/editor.
- Operator.
- Reviewer.
- Viewer.
- Billing administrator.

**Recommendation:**

```text
User
Organization
OrganizationMembership(user_id, org_id, role, status)
PlatformRole(user_id, role)
```

Authorization should be based on membership and resource-level policies.

## P2-03 — Username is treated as system identity

**Severity:** High  
**Type:** Identity risk

Username is globally unique and used as:

- JWT subject.
- Run actor string.
- Memory owner key.

Usernames are business-facing identifiers and should not be permanent foreign keys.

**Recommendation:** Use immutable `user.id`, or external identity `(issuer, subject)`. Store username/email only as mutable profile attributes.

## P2-04 — Memory can cross organization boundaries after a user move

**Severity:** High  
**Type:** Tenant-isolation risk

Memory is keyed only by username. The operator CLI supports moving a user to another organization. The moved user can therefore recall memory created while belonging to the previous organization.

Evidence: [`src/bestteam/core/memory.py`](../src/bestteam/core/memory.py).

Deleting a user also does not automatically delete or detach their memory.

**Recommendation:** Scope retrieval by `(org_id, user_id)` and define explicit memory migration/deletion behavior.

## P2-05 — Memory lacks governance metadata

**Severity:** High  
**Type:** Privacy and lifecycle gap

Memory records lack:

- Organization ID.
- Source Run.
- Source Workflow version.
- Consent/purpose.
- Expiry/retention.
- Confidence.
- Supersession/deduplication.
- Sensitivity classification.
- Created-by model/version.

**Recommendation:** Move governed memory into the primary database or a tenant-aware store with corresponding metadata and deletion workflows.

## P2-06 — Model Catalog is global

**Severity:** High  
**Type:** Tenant-configuration blocker

All organizations see the same model catalog. There is no organization-specific model availability or override.

Evidence: [`ui/backend/db/model_catalog.py`](../ui/backend/db/model_catalog.py).

This cannot represent customers who:

- Bring their own API key.
- Use different providers.
- Prohibit specific models.
- Have negotiated pricing.
- Require regional deployments.
- Have different data-processing policies.

**Recommendation:** Separate global model definitions from organization model policies and connections.

## P2-07 — Model metadata, routing and pricing are conflated

**Severity:** Medium–High  
**Type:** Catalog design debt

One row currently contains display metadata, tier and pricing.

Missing concepts include:

- Provider.
- Capabilities.
- Context limits.
- Structured-output support.
- Tool-calling support.
- Region.
- Currency.
- Price effective dates.
- Cached-input pricing.
- Model retirement.
- Organization routing priority.

**Recommendation:** Introduce `ModelDefinition`, `ModelRate`, `ModelConnection` and `OrgModelPolicy`.

## P2-08 — Per-organization LLM credentials are missing

**Severity:** High  
**Type:** Security/commercial blocker

Email credentials are organization-scoped, but LLM credentials remain process/environment concerns.

**Recommendation:** Store encrypted organization provider connections, with key references rather than plaintext credentials. Add rotation, revocation, health testing and audit history.

## P2-09 — No generic Tool Definition / Connection / Binding model

**Severity:** High  
**Type:** Extensibility blocker

Built-in tools are code functions, while email has its own credential and trigger tables. Every new connector would require another special-purpose schema and loader.

**Recommendation:**

- `ToolDefinition`: executable capability and input schema.
- `ToolConnection`: organization-specific external account or credential.
- `ToolBinding`: Agent/AI Team binding, permissions and alias.
- `SecretReference`: encrypted credential reference.

Tools should remain executable code, but connections and policies should be first-class data.

## P2-10 — Tool and connector authorization is too coarse

**Severity:** High  
**Type:** Security gap

There is no generic model for:

- Which member may configure a connection.
- Which AI Team may use it.
- Read versus write capability.
- Human approval requirements.
- Data-domain restrictions.
- Risk classification.
- Connector health or revocation.

**Recommendation:** Add capability-level policies and binding-time validation.

## P2-11 — Skills lack commercial lifecycle management

**Severity:** Medium–High  
**Type:** Library governance gap

Skills currently lack:

- Immutable versions.
- Publisher/source.
- Changelog.
- Compatibility requirements.
- Approval state.
- Deprecation.
- Organization installation state.
- Integrity hash.
- Parameter schema.
- Tool-version constraints.

Platform built-ins are seeded but not automatically upgraded because the system cannot distinguish customization from the shipped version.

**Recommendation:** Model `Skill` and immutable `SkillVersion`, with install/override records and explicit upgrade actions.

## P2-12 — Knowledge Base persistence is not a scalable data architecture

**Severity:** High  
**Type:** Scale and governance blocker

The database stores KB configuration, while documents, chunks and embedding caches live on local disk/in memory.

Missing concepts include:

- Document identity and version.
- Source connector.
- Ingestion job.
- Parse/index status.
- Chunk provenance.
- Index version.
- Embedding model version.
- Organization storage quota.
- Document ACL.
- Retention/deletion status.
- Object-store location.
- Failure/retry metadata.

**Recommendation:** Separate KB metadata, documents, versions, ingestion jobs and indexes. Use object storage and a managed or database-backed retrieval index when scale requires it.

## P2-13 — SQLite and process-local execution limit shared hosting

**Severity:** High  
**Type:** Scaling blocker

The current design assumes:

- One backend database file.
- One effective runtime process.
- Process-local thread executor.
- Process-local locks.
- Process-local RunRegistry.
- Process-local caches.
- A single trigger poller.

Multiple workers can duplicate trigger processing and cannot share live run state.

**Recommendation:** Move transactional state to PostgreSQL, use a durable job queue and introduce distributed locks/leader election where required.

## P2-14 — Cache invalidation is local and overly global

**Severity:** Medium  
**Type:** Scale inefficiency

Workflow cache freshness considers global Skill, KB and email metadata. Changes in one organization can invalidate cached Workflows belonging to unrelated organizations. Invalidation does not propagate across processes.

**Recommendation:** Compute dependency-specific revision keys and use a shared invalidation channel if multiple workers are introduced.

## P2-15 — Email trigger is a special-case automation system

**Severity:** Medium–High  
**Type:** Extensibility debt

The trigger directly stores `workflow_name`, mailbox UID state and daily counters. It is not a generic event-trigger model.

Limitations include:

- One trigger per organization.
- One trigger type.
- Soft Workflow name reference.
- Single-process polling.
- No generic retry/dead-letter state.
- No schedule/webhook/event abstraction.

**Recommendation:** Introduce `AutomationTrigger` with typed trigger configuration, stable AI Team version binding and durable dispatch state.

## P2-16 — Audit, billing, quota and retention models are incomplete

**Severity:** High  
**Type:** Commercial operations blocker

Missing platform-level concepts include:

- Organization subscription/plan.
- Usage quota and enforcement.
- Budget alerts.
- Audit log.
- Configuration-change actor.
- Secret access history.
- Data retention policy.
- Account suspension.
- Organization deletion workflow.
- Export and legal deletion state.

Usage metering exists, but it is not yet a complete billing or governance subsystem.

## P2-17 — Platform administrator and organization administrator boundaries are unfinished

**Severity:** High  
**Type:** Authorization/privacy blocker

Current administration is platform-wide. There is no organization-admin role, and memory management is designed around platform operators.

Before introducing multiple organization members, the platform needs:

- Organization-scoped administration.
- Object-level authorization.
- Tenant-safe memory management.
- Separation between platform support access and customer administration.
- Auditing of privileged access.

---

# Recommended target model

```text
Organization
User
OrganizationMembership
PlatformRole

AITeam
AITeamVersion
WorkflowDependency

BuilderSession
BuilderRevision

AgentSpec                -- embedded in AITeamVersion
CollaborationGroupSpec   -- embedded in AITeamVersion

ModelDefinition
ModelRate
ModelConnection
OrgModelPolicy

ToolDefinition
ToolConnection
ToolBinding

Skill
SkillVersion
OrgSkillInstallation

KnowledgeBase
KnowledgeBaseVersion
KnowledgeDocument
KnowledgeDocumentVersion
KnowledgeIngestionJob
KnowledgeIndex

Run
TraceEvent
ModelInvocation / UsageRecord
AutomationTrigger

MemoryRecord
AuditEvent
SecretReference
```

## Required relationship rules

- An AI Team has a stable ID and many immutable versions.
- A Run always references one exact AI Team version.
- A published version has typed, resolvable dependency records.
- A Skill dependency normally pins a Skill version.
- A KB dependency explicitly chooses pinned or follow-latest behavior.
- Tools and KBs do not share an untyped namespace.
- Every customer-owned record carries an organization boundary.
- Every user action is attributed by immutable user ID.
- Memory retrieval is scoped by organization and user.
- Published dependencies cannot be physically deleted.
- Draft, preview and production execution paths are separate.

# Delivery priority

## Phase 1 exit criteria

Phase 1 should not be considered complete until:

- AI Team identity is separated from BuilderSession.
- Workflow versions are immutable.
- Runs reference Workflow versions.
- Deployed status is enforced.
- Dependencies are typed and deletion-safe.
- Skill and KB behavior cannot drift unintentionally.
- SQLite foreign keys are enabled.
- Deployment is transactional.
- Trace events are persisted.
- Crash reconciliation and registry eviction exist.
- Vestigial Agent/Team tables are removed.

## Phase 2 exit criteria

Phase 2 should not be considered complete until:

- Organizations support multiple members.
- Membership roles and organization administration exist.
- Memory is tenant-aware.
- LLM credentials and availability are organization-scoped.
- Models, Skills and KBs are versioned/governed.
- Tool connections and bindings are generic.
- PostgreSQL/durable jobs support multiple workers.
- Triggers use durable dispatch and stable version references.
- Audit, quota, retention and organization-deletion policies exist.

## Final recommendation

Complete Phase 1 before adding more object types or connectors. Phase 2 should then be implemented around stable IDs, immutable versions and organization-scoped policies; otherwise each new integration will deepen the current name-based dependency problem.
