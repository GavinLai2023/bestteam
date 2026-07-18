# Per-org email credentials — foundation (sub-project A+B)

**Status:** Approved design, ready for implementation planning.
**Date:** 2026-07-18

## Context

The email toolkit today reads **one** mailbox from process environment
variables (`BESTTEAM_EMAIL_*`), and `db/orgs.py::ensure_email_single_org`
hard-refuses to boot — and refuses a second org — when email is configured on a
multi-org deployment. So a multi-tenant platform cannot use the email tools at
all: the credentials are process-wide and the built-in `email_triage_reply`
skill is visible to every org, which would expose one customer's mailbox to
all tenants.

The business target is **customer self-service**: several customers, each
connecting their own inbox, each org's agents touching only that org's mailbox.
That decomposes into four pieces:

- **A** — encrypted per-org credential storage;
- **B** — runtime wiring so the email tools resolve the *running org's*
  credentials;
- **C** — a per-org admin role (today "admin" means *platform* admin);
- **D** — a customer-facing self-service settings UI.

A and B are the load-bearing foundation and are identical no matter who enters
the credentials — the secret lands in the same encrypted store and is resolved
the same way at runtime. C and D only change the *entry surface*. **This spec
covers A + B plus a thin operator-CLI entry.** C + D (true self-service) is a
separate follow-up sub-project.

### Decisions locked with the user

- **IMAP (app password) first.** Per-org Microsoft Graph and OAuth /
  "connect your inbox" come later; the existing IMAP backend already does basic
  auth, so the first sub-project focuses on the genuinely new part — encrypted
  per-org storage and runtime resolution.
- **Encrypt with `cryptography` / Fernet.** Python's stdlib has no AES, so
  encryption needs a real cipher library. Key comes from a **new**
  `BESTTEAM_SECRETS_KEY`, kept separate from the JWT `BESTTEAM_SECRET_KEY`
  (never reuse a signing key for encryption).
- **Keep the process-env path** for the SDK/CLI and single-customer
  deployments. The per-org store is the multi-tenant path and overrides env.
- **IMAP trust tradeoff accepted.** With basic auth the customer hands the
  platform standing credentials to their mailbox; they travel to the backend
  and are stored encrypted (but the operator holds the key). This is inherent
  to IMAP; OAuth softens it in a later sub-project.

## A — Encrypted per-org credential storage

- **`ui/backend/db/models.py`** — new `OrgEmailCredential`: `org_id` unique FK
  (one mailbox per org), `backend` (`"imap"` for now), `host`, `port`,
  `username`, `password_encrypted` (Text), `drafts_folder` (nullable),
  `created_at` / `updated_at`. Unique constraint on `org_id`.
- **NEW `ui/backend/secret_store.py`** — `encrypt(plaintext) -> str` /
  `decrypt(token) -> str` over `cryptography.fernet.Fernet`, key read from
  `BESTTEAM_SECRETS_KEY`. Reuse the startup-guard pattern from
  `auth.py::is_insecure_secret_key`: refuse to boot if the key is unset or a
  placeholder while any org has stored credentials. `MultiFernet` leaves room
  for key rotation later (not built now).
- **NEW `ui/backend/db/email_credentials.py`** — `get_email_credentials(db,
  org_id)`, `set_email_credentials(db, org_id, *, host, port, username,
  password, drafts)` (encrypts before store), `clear_email_credentials(db,
  org_id)`. Mirrors the small CRUD modules already in `db/` (e.g. `db/orgs.py`).
- **Alembic migration** creating `org_email_credentials` — guarded/idempotent
  in the project style (the table may already exist from the `create_all` that
  runs at import; follow the `_has_table` guard pattern used by the CR-012 /
  CR-031 migrations).
- **`pyproject.toml`** — add `cryptography` to the backend dependency group.
- **`.env.example` + `docs/deployment.md`** — document `BESTTEAM_SECRETS_KEY`
  (generate with `python -c "from cryptography.fernet import Fernet;
  print(Fernet.generate_key().decode())"`), required once any org has stored
  credentials.

## B — Runtime wiring

- **`src/bestteam/tools/email_client.py`** (SDK) — refactor so a backend can be
  built from explicit parameters, not only env:
  - `_ImapBackend.__init__` currently reads env (`email_client.py:244-258`).
    Change it to accept `host` / `user` / `password` / `port` / `drafts`, and
    add a `_ImapBackend.from_env()` classmethod that preserves today's behavior
    for `_get_backend()`.
  - Add `make_email_tools(backend) -> dict[str, callable]` returning the three
    tool functions (`email_find` / `email_read` / `email_draft_reply`) bound to
    a given backend, **with the same docstrings the LLM sees today** (extract
    the formatting bodies so the env tools and per-org tools share them). The
    module-level `email_find` / etc. keep calling `_get_backend()` (env path)
    and stay in `REGISTRY` unchanged.
- **NEW `ui/backend/email_tools.py`** — `load_email_tools(db, org_id) ->
  dict[str, callable]`, mirroring
  `knowledge_bases.py::load_knowledge_base_tools`. Resolves the org's row; if
  present, decrypts the password (`secret_store.decrypt`), builds an
  `_ImapBackend`, and returns `make_email_tools(backend)`. If **absent**,
  returns three tools that emit a friendly *"No mailbox is connected for your
  team — ask an admin to connect one."* — never a build failure or crash — so a
  workflow referencing the built-in `email_triage_reply` skill always compiles.
- **Merge into `extra_tools`** at the three sites that already inject KB tools,
  right beside `load_knowledge_base_tools`: `main.py::_get_workflow`
  (~`main.py:265-273`), `builder.py`, and `crud.py`. The loader merges
  `{**REGISTRY, **extra_tools}` (`core/loader.py`), so the per-org email tools
  override the env ones by name — the existing override mechanism, no loader
  change needed. `org_id` already flows to all three sites
  (`run_in_background(org_id=)` / `_get_workflow(name, db, org_id)`), so there
  is no new run→tool threading to build.
- **Cache invalidation** — `_get_workflow` bakes `extra_tools` into the
  compiled workflow cached per `(org_id, name)`, freshness-keyed by
  `_dependency_freshness` (`main.py`), which currently counts + `max(updated_at)`
  of `SkillRecord` + `KnowledgeBaseRecord`. **Add `OrgEmailCredential`** to that
  tuple so connecting / rotating / clearing a mailbox invalidates the org's
  cached workflows — covering both directions, including the no-creds →
  connected switch.
- **`db/orgs.py::ensure_email_single_org`** — behavior unchanged, but it now
  guards only the **process-env** mailbox (still process-wide, still unsafe on a
  multi-org deployment). The per-org store is the supported multi-tenant path
  and is *not* blocked. Update the docstring to say so.

## Interim entry — operator CLI (until sub-project C+D)

`ui/backend/admin.py`: `set-email <org> --host --user [--port 993] [--drafts]`
(password via double `getpass`, never a CLI argument — matches `create-user`),
and `clear-email <org>`. An optional `--test` flag attempts an IMAP login
before saving and reports failure, so a bad credential is caught at entry
rather than at first run. This connects early customers' mailboxes and proves
the whole path end-to-end before the self-service UI exists.

## Critical files

- **Create:** `ui/backend/secret_store.py`,
  `ui/backend/db/email_credentials.py`, `ui/backend/email_tools.py`,
  `alembic/versions/*_org_email_credentials.py`, tests.
- **Modify:** `src/bestteam/tools/email_client.py`, `ui/backend/db/models.py`,
  `ui/backend/db/__init__.py` (re-export), `ui/backend/main.py`,
  `ui/backend/builder.py`, `ui/backend/crud.py`, `ui/backend/db/orgs.py`
  (docstring), `ui/backend/admin.py`, `pyproject.toml`, `.env.example`,
  `docs/deployment.md`, and the CLAUDE.md files for `ui/backend`,
  `ui/backend/db`, and `src/bestteam/tools`.
- **Reuse:** `knowledge_bases.py::load_knowledge_base_tools` and its three call
  sites; the `core/loader.py` `extra_tools` override; the `auth.py`
  startup-guard pattern; the `admin.py` `getpass` pattern; the guarded-migration
  convention; `main.py::_dependency_freshness`.

## Verification

- **Unit:** `encrypt` → `decrypt` round-trip; a tampered token fails to decrypt.
- **Resolution + override:** `load_email_tools` returns backend-bound tools for
  a configured org and the friendly not-connected tools for an unconfigured
  org; the returned tools override the env ones by name in a built workflow.
- **Cross-org isolation:** a run for org A resolves org A's mailbox, never org
  B's — the leak this feature exists to prevent — as a dedicated test.
- **Guard:** multi-org **+ per-org store** boots and runs email; multi-org **+
  env** is still refused (`ensure_email_single_org` unchanged).
- **Cache:** connecting a mailbox to a previously-unconfigured org invalidates
  and rebuilds that org's cached workflow (freshness-key test).
- **CLI:** `set-email` stores an encrypted (not plaintext) row; `clear-email`
  removes it; `--test` rejects bad credentials; the password never appears in
  `argv`.
- Full suite green; `fake:`-model workflows unaffected.

## Out of scope (later sub-projects)

- **C + D:** the per-org admin role and the customer-facing self-service
  settings UI.
- OAuth / "connect your inbox"; per-org Microsoft Graph; key-rotation tooling;
  multiple mailboxes per org; sending / SMTP (still never — the toolkit stays
  draft-only).
