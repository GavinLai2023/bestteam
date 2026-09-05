# BestTeam — project brief for a resume / CV

*A self-contained summary of the project, written to be pasted into a resume
assistant, a portfolio page, or a job application. Everything here is drawn
from the repository itself (`README.md`, `docs/ARCHITECTURE.md`,
`docs/STATUS.md`, `CHANGELOG.md`, `docs/DECISIONS.md`) as of **2026-09-05**.
Numbers are measured from the tree, not estimated — re-measure with the
commands in "How the numbers were measured" before quoting them later.*

---

## 1. One-line summary

**BestTeam** — a commercial multi-agent AI framework that wraps LangGraph
behind a business-friendly SDK, CLI and multi-tenant web platform, so
non-technical customers can build, deploy and monitor their own AI teams.
*Slogan: "Intent in, BestTeam out."*

## 2. Elevator pitch (2–3 sentences)

BestTeam turns LangGraph's low-level graph orchestration into a product. A
developer describes agents and teams in a few lines of Python or a YAML file;
a non-technical customer instead walks a guided "Team Builder" wizard that
interviews them, has an LLM architect design the team, and deploys it — then
watches it run live over WebSocket. The platform side adds what a commercial
deployment actually needs: org-scoped multi-tenancy, authentication, usage
metering and spend caps, run history with retention policies, knowledge-base
ingestion, and an autonomous email-triage mode that drafts replies but is
architecturally incapable of sending them.

## 3. Status

| | |
|---|---|
| **Stage** | Public beta. Latest build `0.1.0b3` (git tag `v0.1.0-beta.3`, 2026-08-31), deployed on the beta VPS. |
| **Release history** | `0.1.0b1` (2026-08-22) → `0.1.0b2` (2026-08-24, cut but never tagged) → `0.1.0b3` (2026-08-31), each cut from a green `main`. |
| **Development period** | Design decisions logged from 2026-07; active development through 2026-09 (ongoing). |
| **Delivery** | Docker Compose + nginx, single-process + SQLite per deployment; `scripts/deploy.sh` performs backup → pull → env check → build → health wait as one command. |
| **Beta gate** | All launch-gate items G1–G6 and hardening items B1–B5/B8 closed; the conditional G7 (live Microsoft 365 tenant smoke test) and the backup/restore rehearsal both passed 2026-08-31. |
| **In progress** | Nothing blocking; Stage 1 hygiene work (ruff/mypy, `pip-audit`, `/metrics`, unified settings) runs alongside the beta. |
| **Next** | External vector stores (Chroma/FAISS/Pinecone), DMS connectors, read-only SQL executor, sandboxed Python tool, CrewAI adapter, live-run rehydration after restart, per-org admin roles and per-org LLM credentials. |

## 4. Architecture

Three layers, deliberately separated so the engine can be swapped without
touching the public API:

```
Layer 1 · SDK   src/bestteam/    Agent / Team / Pipeline / ToolKit / Memory
                                 ↕ EngineAdapter ABC (LangGraph today, CrewAI possible)
Layer 2 · CLI   bestteam …       init / run / graph — scaffold, execute YAML, render the DAG
Layer 3 · UI    ui/backend       FastAPI + WebSocket (multi-tenant API, metering, triggers)
                ui/frontend      React 19 + TypeScript SPA (dashboard, wizard, admin)
```

- Pipelines are **declarative YAML** loaded by `core/loader.py`; a "Pipeline"
  is what the customer UI calls an **AI team**.
- Teams collaborate in **SEQUENTIAL / PARALLEL / HIERARCHICAL** modes; the
  adapter translates a mode into engine structure so customers never write
  graph code.
- Persistence is SQLAlchemy 2.0 over SQLite with **row-level `org_id`
  isolation**, letting one codebase serve both a single-customer instance and
  a shared multi-tenant platform.

## 5. Tech stack

| Area | Technologies |
|---|---|
| **Language / runtime** | Python 3.10+ (typed, Pydantic v2), TypeScript 6 |
| **Agent orchestration** | LangGraph, `langchain-core`, LangChain model resolution (`init_chat_model`) |
| **LLM providers** | OpenAI, DeepSeek, Google Gemini (`langchain-google-genai`), all behind one model-spec string + a DB-backed model catalog |
| **Backend** | FastAPI, Uvicorn, WebSocket streaming, SQLAlchemy 2.0, Alembic (40 migrations), SQLite |
| **Frontend** | React 19, React Router 7, Vite 8, i18next (English + 简体中文), react-markdown, Vitest + Testing Library |
| **CLI** | Typer + Rich |
| **Retrieval / RAG** | `rank-bm25` keyword index, NumPy cosine vector index, hybrid RRF fusion, opt-in query expansion and cross-encoder reranking (`sentence-transformers`) |
| **Integrations** | Tavily web search, Google Places, IMAP and Microsoft Graph (OAuth, `Mail.ReadWrite`), PDF/Word/Excel/XML parsing, guarded HTTP client |
| **Security** | PBKDF2-SHA256 (260k iterations), Fernet-encrypted per-org secrets store, SSRF blocklist on outbound HTTP, deploy-time tool-pairing validation, cross-org access returns 404 (never 403) |
| **Ops / infra** | Docker + Docker Compose, nginx, GitHub Actions CI with path-filtered job tiers, `uv pip compile` universal lockfile, Sentry SDK, `check-env` / `check-health` operator commands, cron watchdog with webhook alerting |
| **Testing** | pytest (unit / integration / e2e / optional / slow markers), pytest-xdist, Playwright E2E, deterministic `FakeListChatModel` fixtures — zero API cost |
| **Marketing site** | Astro 4 + Tailwind (separate `website/` workspace) |

## 6. Feature highlights (what was actually built)

- **Business-facing SDK** — `Agent` / `Team` / `Pipeline` dataclasses with an
  `EngineAdapter` ABC seam; a two-agent research pipeline is ~15 lines of
  Python or a 20-line YAML file.
- **Team Builder wizard** — a 4-stage guided flow (interview → requirements →
  LLM-generated specification → deploy) that takes a non-technical customer
  from "here is my problem" to a deployed, versioned team, with test runs and
  a refine loop. Includes optional audio interview transcription.
- **Live run monitoring** — WebSocket trace-event streaming with a sync→async
  bridge, per-agent handoff visualisation, cancellation, retry, and a
  persisted run/trace history API.
- **Knowledge bases** — folder upload → parse → chunk → index, with three
  index types (BM25 / vector / hybrid-RRF), incremental async ingestion jobs
  with retry, metered embedding spend, and *grounding checks* with an opt-in
  per-agent policy (`observe | retry | refuse`) and depth (`citation | claim`).
- **Autonomous email triage** — a poller runs an org's deployed team on new
  mail and leaves **draft** replies. Deliberately **no send verb and no SMTP
  anywhere**: the containment argument for feeding an LLM attacker-written
  text is that the worst case is a strange draft a human reads first. Deploy
  validation refuses to pair an email tool with an egress tool in the same
  pipeline.
- **Pre-LLM header filtering + budgets** — sender allow/blocklists, subject
  blocklists and bulk-mail detection decide before a billable model call;
  per-org daily message and monthly spend caps pause dispatch, alert once, and
  auto-resume on period rollover.
- **Multi-tenancy and admin** — org-scoped rows, platform-admin accounts that
  are deliberately org-less, five admin surfaces (accounts, config, memory,
  trace, advanced), a versioned platform/org **skills** system with copy-to-org
  shadowing and per-skill "referenced by deployed teams" impact lists.
- **Metering, retention and compliance posture** — usage records per run,
  per-org retention policies where a purge clears run *content* but keeps
  *accounting*; per-data-subject erasure was explicitly **refused rather than
  approximated**, and that reasoning is written down.
- **Anonymous team sharing** — public share links with continuous chat
  sessions and token-scoped streaming, with strict disclosure boundaries
  (model names, cost estimates and provider error text are kept off
  customer-facing surfaces).
- **Bilingual UI** — English and Simplified Chinese via i18next, single source
  of copy in `locales/`.

## 7. Engineering practices worth calling out

- **~2,485 tests** across unit / integration / E2E tiers, all using
  deterministic fake models — the whole suite costs $0 in API spend and runs in
  ~3m42s serially (2m40s under `-n auto`).
- **A test that enforces the test policy**: `test_marker_completeness.py` fails
  the suite if any test file lacks a tier marker, so a new file can never fall
  outside every CI job's selector.
- **Performance archaeology as routine practice** — documented findings
  include PBKDF2 at production iterations being 69% of suite runtime (fixed by
  a test-only constant *with no env var*, plus a test that reads the
  production literal out of source), and `import bestteam` costing 7.8s vs 1.6s
  because an optional extra drags `transformers` + `torch` onto the path.
- **Reproducible dependency story** — floating ranges in `pyproject.toml`
  because it ships as a library, a `uv pip compile --universal` lockfile that
  CI and Docker install against, and a packaging test that fails if a declared
  dependency is missing from the lock.
- **CI designed around its own failure modes** — path filters written as
  allowlists (a `!negation` pattern would collapse the filter to "always
  true"), with the serial full-suite and E2E tiers gated on `main` because
  running serially in one process is what catches cross-test isolation bugs.
- **Decision records** — `docs/DECISIONS.md` captures *why* (LangGraph over
  CrewAI; multi-tenancy over per-customer instances; in-house SQLite+BM25
  memory over mem0; header rules over a classifier model; SQLite before
  Postgres, with named upgrade triggers), so choices are not re-litigated.
  52 dated design specs live under `docs/superpowers/specs/`.

## 8. Scale of the codebase

| Metric | Value |
|---|---|
| Python — SDK (`src/bestteam/`) | 40 files, ~10,200 lines |
| Python — backend (`ui/backend/`) | 71 files, ~20,500 lines |
| TypeScript/TSX — frontend | 127 files, ~22,100 lines |
| Tests | 121 files, ~48,600 lines, 2,485 test functions |
| REST + WebSocket endpoints | ~96 route declarations |
| Database tables | 31 |
| Alembic migrations | 40 |
| Git history | 237 commits, 46 merged pull requests |
| Design specs / decision records | 52 specs + a dedicated decisions log |

## 9. Resume-ready bullets

### Short (3 bullets — for a dense resume)

- Built **BestTeam**, a commercial multi-agent AI platform wrapping LangGraph
  behind a business-friendly SDK, CLI and multi-tenant FastAPI/React product;
  shipped to public beta (`v0.1.0-beta.3`) on Docker/nginx.
- Designed a guided "Team Builder" wizard that takes non-technical customers
  from a plain-English problem statement to a deployed, versioned AI team, with
  live WebSocket run monitoring, usage metering and per-org spend caps.
- Established the quality bar: ~2,485 tests across unit/integration/Playwright
  E2E tiers running at zero API cost on deterministic fake models, in a
  path-filtered GitHub Actions pipeline with a `uv`-compiled universal lockfile.

### Medium (5–6 bullets)

- Architected and built **BestTeam**, a three-layer commercial multi-agent
  framework (SDK / CLI / web platform) that wraps LangGraph behind
  `Agent`/`Team`/`Pipeline` abstractions and declarative YAML, with an
  `EngineAdapter` ABC keeping the orchestration engine swappable.
- Delivered a multi-tenant FastAPI + WebSocket backend (31 tables, ~96
  endpoints, 40 Alembic migrations) with row-level org isolation where
  cross-org access returns **404, never 403**, so record existence is never
  disclosed.
- Shipped a React 19 + TypeScript SPA — bilingual (EN/简体中文) — covering a
  4-stage AI team-building wizard, live agent-handoff monitoring, five admin
  surfaces, and anonymous public share-link chat.
- Built a retrieval layer with three index types (BM25, vector cosine, hybrid
  RRF) plus opt-in query expansion, cross-encoder reranking, incremental
  ingestion jobs and metered embedding spend, with configurable grounding
  policies (`observe | retry | refuse`).
- Designed an autonomous email-triage mode around a hard containment
  property — **draft-only, no send verb, no SMTP in the process** — with
  pre-LLM header filtering and per-org daily/monthly budget caps so untrusted
  inbound mail can never trigger unbounded spend or outbound action.
- Ran the project with production discipline: decision records for every
  architectural choice, 52 dated design specs, operator runbooks and one-command
  deploy/health scripts, and a documented backup-restore rehearsal plus a live
  Microsoft 365 tenant smoke test before the beta gate closed.

### Long (detailed / portfolio version)

> **BestTeam — Multi-agent AI platform** · Python, FastAPI, LangGraph, React,
> TypeScript, SQLAlchemy, Docker · 2026
>
> Sole architect and engineer of a commercial multi-agent framework that turns
> LangGraph's low-level graph orchestration into a product a non-technical
> customer can use. Three layers: a Python SDK (`Agent`/`Team`/`Pipeline`
> dataclasses behind an `EngineAdapter` ABC, with SEQUENTIAL / PARALLEL /
> HIERARCHICAL collaboration modes), a Typer CLI (`init` / `run` / `graph`),
> and a FastAPI + React web platform.
>
> The platform layer is where the commercial requirements live: org-scoped
> multi-tenancy over SQLAlchemy/SQLite (31 tables, 40 migrations), PBKDF2
> authentication with login-rate limiting, a Fernet-encrypted per-org secrets
> store, DB-backed model catalog spanning OpenAI/DeepSeek/Gemini, usage
> metering with per-org budget caps, run history with retention policies, async
> knowledge-base ingestion, versioned platform/org skills with impact analysis,
> and WebSocket streaming of live agent trace events.
>
> Notable design work: an autonomous email-triage mode that reads a customer's
> mailbox and leaves **draft** replies, built so that no send verb and no SMTP
> exists anywhere in the process — making the worst outcome of a successful
> prompt injection a strange draft a human reads, rather than an outbound
> action; deploy-time validation that refuses to pair an email tool with an
> egress tool in the same pipeline; attachment reading that takes no filesystem
> path at all, so traversal is impossible rather than defended against; and a
> documented refusal to approximate per-data-subject erasure instead of
> shipping a feature that would lie about its guarantees.
>
> Quality: 2,485 tests across unit / integration / Playwright E2E tiers, all on
> deterministic fake models for zero API cost, with a meta-test that fails the
> suite if any test file escapes CI's tier selectors; a `uv pip compile
> --universal` lockfile that CI and the Docker image install against; and
> path-filtered GitHub Actions job tiers. Shipped three beta builds, each cut
> from a green `main`, and deployed to a VPS with one-command deploy, health
> check and backup/restore scripts.

## 10. Keyword / skills list (for ATS and profile fields)

`Python` · `TypeScript` · `LangGraph` · `LangChain` · `Multi-agent systems` ·
`LLM application development` · `RAG / retrieval` · `BM25` · `Vector search` ·
`Reciprocal Rank Fusion` · `Reranking` · `Prompt injection mitigation` ·
`FastAPI` · `WebSocket` · `Pydantic` · `SQLAlchemy 2.0` · `Alembic` · `SQLite` ·
`REST API design` · `Multi-tenancy` · `Authentication & authorization` ·
`Secrets management` · `React 19` · `React Router` · `Vite` · `i18next` ·
`Internationalisation` · `Typer/CLI tooling` · `pytest` · `Playwright` ·
`Vitest` · `Test architecture` · `GitHub Actions` · `CI/CD` · `Docker` ·
`Docker Compose` · `nginx` · `Astro` · `Tailwind CSS` · `OpenAI API` ·
`DeepSeek` · `Google Gemini` · `Microsoft Graph API` · `OAuth 2.0` · `IMAP` ·
`Usage metering` · `Data retention` · `Technical writing / ADRs`

## 11. Honest scope — useful for interviews

State these plainly; they read as engineering judgement, not gaps:

- **Single process on SQLite for the beta.** Postgres is not planned before GA;
  the decision record names the upgrade triggers (>10 active orgs, routinely >4
  concurrent runs, a customer needing a second account, any HA/SLA requirement).
- **In-flight runs are lost on restart** — `RunRegistry` is not rehydrated from
  the database, so interrupted runs are swept to `failed`. History persists.
- **No external vector store and no DMS connectors** — the retrieval layer is
  in-process by design for the beta's document volumes; the interfaces exist.
- **Alerting is in-app plus one optional webhook, never email** — deliberate,
  because there is no SMTP in the product at all.
- **Attachments are text-only, no OCR, no archive expansion**; inbound mail is
  filtered by header rules, never a classifier model.
- **Not started**: CrewAI adapter, DEBATE collaboration mode, deployment
  templates. (HIERARCHICAL mode *is* implemented.)

## 12. How the numbers were measured

```bash
find src        -name '*.py'   | wc -l && find src        -name '*.py'   -exec cat {} + | wc -l
find ui/backend -name '*.py'   | wc -l && find ui/backend -name '*.py'   -exec cat {} + | wc -l
find ui/frontend/src -name '*.ts*' | wc -l && find ui/frontend/src -name '*.ts*' -exec cat {} + | wc -l
find tests      -name '*.py'   | wc -l && find tests      -name '*.py'   -exec cat {} + | wc -l
grep -rho '^\s*\(async \)\?def test_' tests --include='*.py' | wc -l   # test functions
grep -rhoE '@(app|router)\.(get|post|put|patch|delete|websocket)\("[^"]*"' ui/backend --include='*.py' | wc -l
grep -rhoE '__tablename__ = "[a-z_]*"' ui/backend/db | sort -u | wc -l
ls alembic/versions | wc -l
git rev-list --count HEAD && git log --oneline --merges | wc -l
```
