# Deploying bestteam

bestteam runs as either a **per-customer instance** or a **shared multi-org
platform** — the same code serves both (see `docs/DECISIONS.md`,
"org-scoped multi-tenancy"). A per-customer instance is simply a deployment
with one organization; a shared platform has one org per customer, each
provisioned with the operator CLI (step 4). Each deployment runs the
FastAPI backend (with its own SQLite database) and the React frontend
behind Docker Compose.

> **Shared-platform caveat:** the email-tool environment variables
> (`BESTTEAM_EMAIL_*`) configure ONE mailbox for the whole process — the
> backend refuses to start (and `create-org` refuses a second org) when they
> are set on a multi-org deployment. On a multi-org platform connect each
> customer's mailbox per-org with `admin set-email` instead (§4c). The
> process-wide caveat still applies to any other process-wide integration env.

## 0. First customer, in order

Every step below has its own section with the detail; this is the running
order, because the sections are organised by topic and the order matters in
three places (the checklist runs *before* the first `up`, migrations run
*before* provisioning, and the mailbox type has to be known before you ask the
customer's IT for anything).

| # | Step | Where |
|---|------|-------|
| 1 | Provision a host with Docker, clone the repo, write `.env` | §1 |
| 2 | **Work out the customer's mailbox type** — do not ask them | §0a |
| 3 | Run the launch checklist against the real environment | §1, "Beta launch checklist" |
| 4 | `docker compose build && docker compose up -d` | §2 |
| 5 | `alembic upgrade head` | §3 |
| 6 | `create-org` (skip on a single-customer instance — "default" is seeded) | §4 |
| 7 | `create-user` for the customer's one member | §4 |
| 8 | `create-user --platform` + `promote` for yourself | §4b |
| 9 | `set-email <org> ... --test` | §4c |
| 10 | **If the mailbox is on Microsoft 365**, walk `docs/email-smoke-test.md` §9 against the live tenant, with the customer | §4c |
| 11 | Set a retention period, if the customer wants one bounded | §4, "Keeping and removing run history" |
| 12 | Install the nightly backup cron, and copy backups off the host | "Backup and restore" |
| 13 | Store `BESTTEAM_SECRETS_KEY` in a password manager, **not** beside the backups | "Backup and restore" |
| 14 | Rehearse `scripts/restore.sh` on a throwaway stack — once, before the customer is live | "Backup and restore" |
| 15 | Hand the customer `docs/BETA_NOTES.md` | — |

Steps 12–14 are the ones that get skipped under time pressure and are the ones
that cost the most when skipped. A restore script that has never been run is
not a backup strategy.

### 0a. Which mailbox does the customer have?

**Do not ask the customer "is it Microsoft 365 or IMAP?"** — it is not a
question a non-technical person can answer, and a wrong answer sends their IT
department off configuring the wrong thing. Work it out from the address:

| Address | Verdict |
|---------|---------|
| `@outlook.com`, `@hotmail.com`, `@live.*`, `@msn.com` | **Personal Microsoft account — not supported.** Microsoft disabled basic-auth IMAP for these, and the app-only OAuth path needs an Entra tenant, which a personal account is not in. The customer needs a work mailbox. |
| `@gmail.com` | IMAP with an **app password** (not the account password); the customer needs 2-step verification enabled to create one. |
| `@qq.com`, `@163.com`, `@126.com` and similar | IMAP, with a provider-issued **authorisation code** rather than the login password. The customer enables IMAP in their mailbox settings first. |
| A company domain | Look up the MX record (below). |

For a company domain, one command decides it:

```bash
# Linux/macOS
dig +short MX example.com
# Windows
nslookup -type=mx example.com
```

- Answers pointing at **`*.mail.protection.outlook.com`** → **Microsoft 365**.
  Use `--auth microsoft-oauth` (§4c), and their IT has to create the app
  registration, consent to `IMAP.AccessAsApp`, and grant the app access to
  that one mailbox — see "Microsoft 365 mailboxes" in §4c for the exact
  PowerShell. **Budget real time for this**: it is a change in the customer's
  tenant, made by someone who is not in your meeting.
- Answers pointing at **`*.google.com` / `*.googlemail.com`** → Google
  Workspace. IMAP with an app password, as Gmail above.
- Anything else → ordinary IMAP. Ask their IT for the IMAP hostname, the
  username, and whether the mailbox needs an app-specific password. `--test`
  (§4c) verifies a real login before anything is stored, so a wrong answer
  costs one command, not a support cycle.

Whatever the answer, the customer only ever needs one mailbox: one org, one
mailbox, one automated team (see `docs/BETA_NOTES.md`).

## 1. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and fill in:

- LLM provider keys (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `TAVILY_API_KEY`
  if using web search).
- `BESTTEAM_SECRET_KEY` — the backend refuses to start (in any environment)
  if this is left at the default value; generate a real one with:
  ```bash
  python -c "import secrets; print(secrets.token_hex(32))"
  ```
- `BESTTEAM_CORS_ORIGINS` — the customer's frontend URL(s), comma-separated,
  no trailing slash.
- `VITE_API_BASE` / `VITE_WS_BASE` — the backend's public URL, as reachable
  from the customer's browser (`https://...` / `wss://...`). These are baked
  into the frontend at build time.
- `BESTTEAM_SECRETS_KEY` — encryption key for at-rest secrets (currently the
  per-org mailbox passwords set with `admin set-email`, or connected by
  customers themselves in the wizard; see "Per-org email" below). **One key for
  the whole deployment, not one per customer:** every org's mailbox password is
  encrypted with this same key, which you (the operator) generate and set
  **once** when standing up the server. Customers never see it, never generate
  it, and are never asked for it — it is not a per-user or per-mailbox value.
  Required once any org has connected a mailbox — the backend refuses to start
  if it can't decrypt stored credentials. Must be a **different** key from
  `BESTTEAM_SECRET_KEY`, and it lives in the environment (or a secrets manager)
  — **never in the database.** Storing the key next to the ciphertext it
  protects would defeat the encryption: anyone who obtained a database dump
  would have both the locked passwords and the key to unlock them. Generate
  with:
  ```bash
  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  ```
- `BESTTEAM_DEMO_WORKFLOWS` — **leave unset on a customer deployment.** It
  exposes the shipped demo workflows in `ui/backend/workflows/`, which belong
  to no org, so every user would see and be able to run them. Most return
  hardcoded `fake:` text that reads like a real answer; `email_triage_demo_live`
  reads the `BESTTEAM_EMAIL_*` mailbox. Set it to `1` only on a dev or
  sales-demo instance.

TLS termination (HTTPS/WSS) is assumed to be handled by a reverse proxy or
the hosting platform's load balancer in front of these containers.

**Login throttling and the client address.** `/api/auth/login` throttles
failed attempts per username (5 in 15 minutes) and per client address (20 in
15 minutes) and answers 429 with `Retry-After`. Uvicorn only substitutes the
address from `X-Forwarded-For` for a proxy it trusts, so behind your reverse
proxy set `FORWARDED_ALLOW_IPS` in `.env` to the proxy's address (or `*` if
the backend port is reachable only from the proxy) -- otherwise every login
appears to come from the proxy and the per-address budget is shared by all
users. The per-username budget holds either way.

### Beta launch checklist

Before the first `docker compose up -d` for a beta organisation, run the
checklist against the *actual* environment the backend will see:

```bash
docker compose run --rm --no-deps backend python -m ui.backend.admin check-env
```

It prints one line per variable and exits 1 on any `[FAIL]`. It only reads:
run on a box with no database yet, it leaves it that way. What it holds you
to, and why:

| Item | Level | Why it is on the list |
|------|-------|-----------------------|
| `BESTTEAM_SECRET_KEY` set, not a placeholder | FAIL | The backend refuses to start otherwise; better to learn that here than from a crash loop. |
| `BESTTEAM_SECRETS_KEY` set, a real Fernet key, **different** from the signing key | WARN if unset, FAIL if equal/invalid | Required the moment a mailbox is connected; reusing the signing key would make one leak two. |
| `BESTTEAM_CORS_ORIGINS` exact origins, no `*`, no trailing slash | FAIL | A wildcard is refused; a wrong origin means the frontend cannot call the API at all. |
| `VITE_API_BASE` / `VITE_WS_BASE` set, `https://` / `wss://` | FAIL if unset | Baked into the frontend image at build time — wrong values mean a rebuild. |
| `BESTTEAM_DEMO_WORKFLOWS` **off** | FAIL | Every org user would otherwise see and run the shipped demo teams. |
| `BESTTEAM_EMAIL_*` unset | WARN | Configures one process-wide mailbox; use per-org `admin set-email` instead. |
| `BESTTEAM_RUN_RETENTION_DAYS` set (e.g. `90`) **before** creating the org | WARN | Otherwise the org keeps run history forever, and existing orgs are never retro-fitted. |
| `BESTTEAM_SENTRY_DSN` set | WARN | Without it the container log is the only record of a failure. |
| `BESTTEAM_SENTRY_DSN` a valid DSN | FAIL | A malformed one makes `sentry_sdk.init` raise at import -- a restart loop. |
| `FORWARDED_ALLOW_IPS` set to your proxy | WARN | Otherwise the per-address login budget is shared by everyone behind the proxy. |

Then, once the org exists: connect its mailbox with `--test`
(§4c), and if it is on Microsoft 365, walk `docs/email-smoke-test.md` §9
against the live tenant with the customer before go-live. Hand the customer
`docs/BETA_NOTES.md`.

## 2. Build and start

```bash
docker compose build
docker compose up -d
```

The backend image installs under `requirements.lock`, so a rebuild -- a hot-fix
during the beta included -- gets exactly the dependency versions the previous
build ran on, not whatever was newest that day. Newer upstream versions arrive
only through a deliberate lockfile update (README, "Updating the lockfile").

What the containers do for you (all in `Dockerfile` / `docker-compose.yml`):

- **Both services restart on their own** (`restart: unless-stopped`) after a
  crash or a host reboot, and stay down only after an explicit
  `docker compose stop`. The backend has a `HEALTHCHECK` on `/api/health`,
  which pings the database (`SELECT 1`) and answers 503 when it cannot -- so
  `docker compose ps` shows `healthy`/`unhealthy` rather than only `Up`, and
  the frontend (`depends_on: condition: service_healthy`) is only offered once
  the backend can answer. **Plain Docker does not restart an `unhealthy`
  container** -- `restart: unless-stopped` reacts to the process exiting, not
  to the health status -- so an unhealthy backend is something to look at, not
  something that heals itself; add an autoheal sidecar if you want that. The
  check does not compare the Alembic revision: a process mid-migration is
  healthy and must not be marked otherwise for being behind.
- **The backend runs as an unprivileged user** (`app`, uid 1000). Only the data
  directory -- the SQLite database and knowledge-base uploads, the
  `bestteam_data` volume -- is writable. A volume created by an *earlier*
  root-running image is root-owned; before the first start on this image, run
  once: `docker compose run --rm --no-deps --user root backend chown -R 1000:1000 /app/ui/backend/data`.
- **The backend applies migrations on every start** (`docker-entrypoint.sh`
  runs `alembic upgrade head` before `uvicorn`, and only before `uvicorn` --
  `docker compose run backend python -m ui.backend.admin ...` runs as given, so
  the recovery commands in section 3 are never gated on the migration they
  recover from).
- **Container logs are rotated** (json-file, 5 x 20 MB backend, 3 x 10 MB
  frontend); see "Logs and error reporting" below for where to look and what
  is reported.
- The backend is capped at **2 GB of memory**; raise `deploy.resources.limits`
  before enabling the local reranker.
- **Upload size limits belong on your reverse proxy**, not the frontend nginx:
  the browser talks to port 8000 directly, and a knowledge-base workbook or an
  interview recording (up to 200 MB) has to fit through whatever fronts it.

`docker compose` automatically loads `.env` from the project root to
substitute `${VITE_API_BASE}`/`${VITE_WS_BASE}` in `docker-compose.yml` (this
is separate from the backend's `env_file: .env`), so the values you set in
step 1 are baked into the frontend image at build time.

## 3. Database migrations

The backend container runs `alembic upgrade head` itself every time it starts
(see section 2), so on a normal `docker compose up -d` -- first start or after
pulling an update with a new file under `alembic/versions/` -- there is nothing
to do. The command is still yours to run by hand when you want to migrate
without starting the server, or to see a migration's output on its own:

```bash
docker compose run --rm --no-deps backend alembic upgrade head
```

Migrations are the canonical way the schema is created/updated
(replacing a bare `Base.metadata.create_all()`, which still runs
automatically as a harmless no-op safety net on a brand-new database).

**Why the entrypoint migrates before serving.** `create_all()` never adds a
new index/constraint to a pre-existing table, so a security invariant
introduced by a migration (currently: one member per org) isn't in force until
`alembic upgrade head` runs. As a backstop the backend **refuses to start**
(HTTP) while that invariant is violated — see the recovery procedure below —
so the window can't be served through either way.

### Recovering a legacy multi-member org

A database created under the earlier "multiple members per org" model may still
have such an org. The one-member-per-org migration **refuses** (naming the
offending orgs) rather than deleting accounts, and the backend refuses HTTP
startup for the same reason. Because the main service won't be serving, run
recovery in a throwaway container (`run --rm --no-deps`, not `exec`):

```bash
# See which orgs are affected: the startup error / migration error names them.
# Then, per extra account, either remove it...
docker compose run --rm --no-deps backend python -m ui.backend.admin delete-user <username>
# ...or reassign it to another (empty) org or to a platform operator:
docker compose run --rm --no-deps backend python -m ui.backend.admin move-user <username> --to-org <other>
docker compose run --rm --no-deps backend python -m ui.backend.admin move-user <username> --platform

# Once each org has at most one member, apply the migration and restart:
docker compose run --rm --no-deps backend alembic upgrade head
docker compose up -d
```

## 4. Provision orgs and users (operator CLI)

There is **no public registration** — neither a UI nor an API endpoint.
Organisations and accounts are created deliberately with the operator CLI
(make sure the migrations in step 3 have run first):

```bash
# One org per customer. A single-customer instance can just use the
# auto-seeded "default" org and skip create-org.
docker compose exec backend python -m ui.backend.admin create-org acme --display-name "Acme Corp"

# Org members (prompts for a password; --org defaults to "default"):
docker compose exec backend python -m ui.backend.admin create-user alice --org acme
# NOTE: one member per org is currently enforced -- create-user refuses a
# second member of the same org (org resources such as the shared mailbox have
# no per-member privilege separation yet). Platform operators are exempt.

# Yourself, as a platform operator (belongs to no org):
docker compose exec backend python -m ui.backend.admin create-user op --platform

# list orgs:  ... python -m ui.backend.admin list-orgs
```

## 4b. Grant the first admin

New accounts are always non-admin. The admin surfaces — **Accounts**,
**Advanced** (config), **Memory** and **Trace** — require an admin, granted
only with the operator CLI (never from an env list or by username match):

```bash
docker compose exec backend python -m ui.backend.admin promote op
# list current admins:  ... python -m ui.backend.admin list
# revoke:               ... python -m ui.backend.admin demote <username>
```

Only platform accounts (`create-user --platform`, no org) can be promoted:
admin reaches every org's config, so `promote` refuses org members — create
a separate org-less account for the person instead.

Admin surfaces (`/api/config`, `/api/memory`) work across orgs — mutations
target one explicitly via `?org=<name>`. Org-user surfaces (the wizard,
running workflows) require an org account; a platform operator who wants to
run workflows creates themselves an org user too.

## 4c. Connect each org's mailbox (per-org email)

The email tools (`email_find`/`email_read`/`email_read_attachment`/
`email_draft_reply`) read one mailbox
**per organization** — each customer's agents reach only that customer's inbox.
Requires `BESTTEAM_SECRETS_KEY` set (step 1) — the single deployment-wide key
encrypts every org's password; the passwords are stored encrypted, the key is
not stored in the database at all.

**Customers self-connect in the Team Builder wizard.** When a customer builds a
team that uses email, the wizard shows a "Connect your mailbox" step (soft at
Preview so they can test against their real inbox; a hard gate at Deploy —
`org/email` endpoints, guarded by their own org login). So the operator CLI
below is mainly for onboarding on the customer's behalf; day-to-day, customers
connect themselves.

```bash
# IMAP with an app password (prompts for the password; --test verifies a login
# before saving). Use an app-specific password, not the account password.
docker compose exec backend python -m ui.backend.admin set-email acme \
  --host imap.gmail.com --user support@acme.com --test

# Microsoft 365 / Exchange Online (prompts for the client secret). No --host:
# Exchange Online's IMAP endpoint is fixed. See "Microsoft 365 mailboxes" below
# for what your customer's IT has to set up first.
docker compose exec backend python -m ui.backend.admin set-email acme \
  --auth microsoft-oauth --user support@acme.com \
  --tenant <directory-id> --client-id <application-id> --test

# Disconnect:
docker compose exec backend python -m ui.backend.admin clear-email acme
```

### Microsoft 365 mailboxes

Exchange Online no longer accepts basic authentication, so an app password does
not exist for these mailboxes and the standard IMAP option will always be
rejected. They connect with app-only OAuth instead (SASL XOAUTH2 over IMAP).

The setup happens once, in the **customer's own Azure tenant** — the platform
cannot do it for them:

1. Register an application in Entra ID (Azure AD). Note the **Directory
   (tenant) ID** and **Application (client) ID**, and create a **client
   secret**.
2. Add the API permission **Office 365 Exchange Online → Application →
   `IMAP.AccessAsApp`**, and grant admin consent.
3. In Exchange Online PowerShell, register the service principal and give it
   access to the one mailbox:
   ```powershell
   New-ServicePrincipal -AppId <client-id> -ServiceId <object-id>
   Add-MailboxPermission -Identity <mailbox> -User <object-id> -AccessRights FullAccess
   ```
4. Recommended: restrict the app to that single mailbox with an Exchange
   **Application Access Policy**, so the credentials cannot reach any other
   mailbox in the tenant.

The customer then enters the mailbox address, tenant ID, client ID and client
secret in the wizard (or you run the `--auth microsoft-oauth` command above).
The client secret is encrypted with `BESTTEAM_SECRETS_KEY` exactly like an IMAP
password. **Azure client secrets expire** (typically 6–24 months): when one
does, every automatic run for that org starts failing with a mailbox-kind error
on the Automations page, and the fix is to create a new secret and reconnect.

If a connection attempt fails, the error distinguishes the two causes, which
have different fixes: a rejected *token* means the tenant/client ID or the
secret is wrong; a token that works but a refused *mailbox* means step 2 or 3
is incomplete.

This is the multi-tenant path; the process-wide `BESTTEAM_EMAIL_*` env vars remain the
single-mailbox path for the SDK/CLI and single-customer deployments, and stay
**refused** on a multi-org deployment (one mailbox can't be shared safely
across tenants). An org with no mailbox connected gets a clear "no mailbox
connected" message from the tools rather than an error.

### Automatic runs (autonomous email trigger)

Once a customer's email team is deployed and their mailbox is connected, they
can opt in (Deploy page: "Run automatically when new email arrives") to have
the platform poll their inbox every `BESTTEAM_TRIGGER_POLL_SECONDS` (default
120) and run the team on new mail — no prompt needed. Safety rails:
`BESTTEAM_TRIGGER_DAILY_CAP` automatic runs per org per day (default 50, then
paused until midnight UTC), and `BESTTEAM_TRIGGERS_DISABLED=1` as a
platform-wide operator kill switch. Autonomous runs appear in the org's
activity list attributed to the `email-trigger` user; the team still only
ever saves drafts. Dedup is by IMAP UID baseline, set at enable time, so the
existing mailbox backlog never triggers runs. Design:
`docs/superpowers/specs/2026-07-19-email-trigger-autonomous-runs-design.md`.

Each automatic run handles at most `BESTTEAM_TRIGGER_BATCH_SIZE` messages
(default 20) and is confined to exactly those messages; a larger burst is
processed over successive polls, nothing skipped or reprocessed. **Run the
backend as a single process/worker:** the poller and its overlap protection
are in-process, so multiple ASGI workers would each poll and could double-
process mail. Leader election is future work; until then, one worker — and
the backend now enforces it: startup takes an exclusive OS lock on
`<db>.lock` next to the database file (`ui/backend/process_lock.py`), so a
second process against the same database (`uvicorn --workers N`, a second
replica) refuses to start with a clear error instead of corrupting state.
The lock is released by the OS on any exit, so a crashed process never
blocks the next start.

An invalid `BESTTEAM_TRIGGER_*` value (non-numeric or non-positive) refuses
startup with a clear error instead of silently stopping the poller later.

### Alerts when automation breaks (per-org)

A trigger that starts failing now says so. Alerts appear in the app under
**Activity → Alerts**, and each org can additionally point them at one webhook
(**Activity → Alerts → Where to send alerts**). Four things raise one:

| Condition | When it fires |
|---|---|
| Repeated workflow failures | after `BESTTEAM_TRIGGER_ALERT_THRESHOLD` consecutive failures (default 3, minimum 1) |
| Mailbox unreachable | same threshold |
| A run released by the stale-run watchdog | immediately — it has already been stuck for the full run timeout |
| A Microsoft 365 client secret nearing expiry | 30 days, 7 days, and on expiry |

Alerts fire on **transitions, not occurrences**: once a condition is reported
it stays quiet until it clears, and a recovery is announced once. A successful
mailbox check clears only a mailbox alert — it says nothing about whether the
team still runs.

The Microsoft 365 expiry date is entered by the admin when connecting the
mailbox (optional; Azure shows it beside the secret being copied). It is
deliberately **not** read from Entra: that would need `Application.Read.All`,
a directory-wide read over every app registration in the tenant. With no date
recorded, no expiry alerts are sent.

**Webhook contract.** `POST` with `Content-Type: application/json`,
`X-BestTeam-Delivery: <notification id>`, and — when a signing secret is set —
`X-BestTeam-Signature: sha256=<hex>`, an HMAC-SHA256 over the exact request
body. Any 2xx is success; otherwise it is retried on later poll cycles up to
five attempts, after which it is marked failed and remains readable in-app.

```json
{
  "id": 12,
  "org_id": 3,
  "kind": "trigger_health",
  "severity": "error",
  "title": "Automatic email replies are failing",
  "body": "The last 3 automatic runs failed, so no replies are being drafted.",
  "fingerprint": "workflow",
  "created_at": "2026-08-17T09:14:00+00:00"
}
```

Verifying a delivery (Python):

```python
import hashlib, hmac
expected = "sha256=" + hmac.new(secret.encode(), request.body, hashlib.sha256).hexdigest()
assert hmac.compare_digest(expected, request.headers["X-BestTeam-Signature"])
```

The payload carries **health information only** — never a subject, address or
message body. Webhook URLs must be HTTPS and must resolve to a public address:
alerts are not a route into the deployment's own network, so self-hosted
internal endpoints are not supported. There is no email delivery channel and
there will not be one — this product has no SMTP anywhere by design.

### Keeping and removing run history (per-org)

A team that reads email produces runs whose output contains customer content —
names, subjects, excerpts of what people wrote. Raw message bodies are already
redacted before anything is stored, but a team's *answer* is the product, so it
cannot be redacted. The lever is time.

Each org sets its own period on **Activity → Data**: keep forever (the
default), or 30/90/180/365 days. Nothing is deleted until someone chooses a
period — upgrading this software never removes a customer's history on its own.
`BESTTEAM_RUN_RETENTION_DAYS` sets the period a **newly created** org starts
with; existing orgs are never retro-fitted.

**What a cleanup removes** — the run's input and output, its step-by-step
trace, and any automation result's extracted payload.
**What it keeps** — that the run happened, when, what it cost, and which
messages already had a reply drafted. That last one is not a compromise: a
retry uses it to avoid drafting a second reply to a message that already has
one.

Three operational points worth knowing:

- **Export before you enable it.** `Download export` on the same tab (or
  `GET /api/org/export`) returns exactly what a cleanup would remove, as JSON.
  A bundle capped by `BESTTEAM_EXPORT_MAX_RUNS` (default 5000) says
  `"truncated": true`, so a partial export cannot be mistaken for a whole one.
- **The cleanup rides the email poller's timer**, so it runs within one poll
  cycle of becoming due. Setting `BESTTEAM_TRIGGERS_DISABLED=1` pauses
  automatic runs but **not** cleanups — pausing automation is not a decision to
  stop deleting data.
- **A cleanup is not a secure erase.** SQLite leaves the old page contents on
  disk until `VACUUM`, which nothing here runs. It is adequate for "we stop
  keeping this"; it is not adequate against an adversary holding the database
  file.

Deleting on request: **Activity → Runs → a run → Delete this run's content**
(`POST /api/runs/{id}/purge`), or `Delete now` on the Data tab for everything
older than a stated window. Deletion **by email address** is not offered — the
address is not stored anywhere indexed, only inside free text the model may
have paraphrased, so matching it would both miss and over-delete. Tell
customers that plainly rather than implying it works.

## 5. Verify

- `curl http://localhost:8000/api/health` → `200 {"status": "ok", "database": "ok"}`
  (public, no auth required; `503 {"status": "degraded", "database": "error"}`
  means the backend cannot reach its SQLite file).
- `curl http://localhost:8000/api/workflows` → `401` (auth required).
- `curl http://localhost:8000/api/workflows -H "Authorization: Bearer <access_token>"`
  → `200`.
- Open the frontend in a browser — you'll be redirected to `/login`. Log in
  with the user created above; you should land on a page with a "Log out"
  link in the nav. Which page depends on the account: `/` routes an org
  member to `/activity` (the Dashboard), or to `/wizard` if the org has
  nothing deployed yet, and a platform admin to `/advanced`.

## Updating built-in skills on an existing deployment

Built-in skills (e.g. `email_triage_reply`) are seeded on boot **only if the
row is absent** — seeding never overwrites an existing row, so an admin's
edits are never clobbered. The flip side: when a new release ships an improved
built-in, a deployment that already has the row keeps the old version.

To adopt an updated built-in on an existing database you need the **new**
shipped definition — the Advanced UI shows the *stored* (old) row, so opening
and saving it just re-writes the old value. Print the current shipped default:

```bash
docker compose exec backend python -c "import json; from ui.backend.skills import DEFAULT_SKILLS; print(json.dumps(next(s.to_raw() for s in DEFAULT_SKILLS if s.name == 'email_triage_reply'), indent=2))"
```

Then paste that JSON into Advanced UI → Skills → `email_triage_reply` and
**Save** (or `PUT /api/config/skills/email_triage_reply` with the org query
omitted — that targets the platform tier). Deleting the row and restarting
re-seeds the same default.

**If you have customized that skill locally, both paths overwrite your edits** —
there is no automatic version-and-overwrite (distinguishing a stock row from a
customized one would need stored versioning, not warranted at this scale), so
diff the printed default against your stored value and merge by hand.

## Data persistence

Everything the deployment owns lives in the `bestteam_data` named volume,
mounted at `/app/ui/backend/data` in the backend container, and survives
`docker compose restart` / `docker compose down` (without `-v`):

- `bestteam.db` — the SQLite database: organisations, users, teams and their
  version history, skills, knowledge-base chunks, run history and traces,
  usage, the inbox ledger, settings. This is the part a restore cannot do
  without. It runs in WAL mode, so `bestteam.db-wal` and `bestteam.db-shm`
  sit beside it while the backend is up: never copy or delete the `.db` on
  its own while the process is running — use the scripts below.
- `knowledge_base_uploads/<org>/<name>/<version>/` — the original documents
  behind every knowledge base. Retrieval is served from the database, so a
  collection still answers without these; they are what a re-index or a
  "which file was this" question falls back on.
- `builder_sessions/` — per-session wizard workspace directories (the
  `source` a session's spec is validated against; mostly empty), plus the
  per-user memory store if the operator pointed `BESTTEAM_MEMORY_DB` into this
  directory.

## Logs and error reporting

**Where the logs are.** Both containers log to stdout, which Docker keeps as
rotated json-file logs (backend 5 x 20 MB, frontend 3 x 10 MB -- about a week
of an active beta at INFO). Read them with `docker compose logs -f backend`
(add `--since 1h` / `--tail 500`); they survive a container restart but not
`docker compose down -v` or a host rebuild, so if you need them longer, ship
them with the log driver of your choice (`logging:` in `docker-compose.yml`)
or a host agent that tails `/var/lib/docker/containers/*/*-json.log`. Every
application record is `timestamp LEVEL logger: message`; `BESTTEAM_LOG_LEVEL`
lowers or raises the floor (INFO by default). Uvicorn's own access log is
separate and untouched.

**One error channel, opt-in.** Set `BESTTEAM_SENTRY_DSN` (Sentry's free tier
is enough for a beta; any Sentry-protocol collector such as GlitchTip works)
and the backend reports exactly two kinds of event -- an *unhandled* request
exception (the 500 the customer saw) and a *failed run* (the workflow's own
failure or a crash on the worker thread) -- tagged with the run id, workflow
name, method and route template (never a concrete path, whose parameter can
be a share token). The exception's type and stack go; its *message* does
not (a parser error quotes the model's output, an HTTP error the URL a tool
fetched), and neither does a failed run's reason -- both are in the run's
trace on the box, keyed by the run id in the report. Nothing else is
captured: no ERROR-log mirroring, no request bodies, no stack-frame locals,
no performance tracing, no user data (`send_default_pii` is off). This is
deliberate -- the process handles customers' email and documents, and a
report has to be safe to leave the box. A malformed DSN stops the backend at
start-up (`check-env` flags it first). `BESTTEAM_ENVIRONMENT`
(default `production`) and `BESTTEAM_RELEASE` label the events; unset the DSN
to turn reporting off. `sentry-sdk` ships in the image; on a bare
`pip install`, it is part of the `ui` extra.

## Backup and restore

A backup is **two files**, taken by two scripts, both safe to run while the
backend is running:

```bash
./scripts/backup-db.sh       # the database, via SQLite's online backup API
./scripts/backup-files.sh    # everything else on the data volume, as a .tgz
# or with explicit paths:
./scripts/backup-db.sh    /path/to/backups/bestteam-2026-06-17.db
./scripts/backup-files.sh /path/to/backups/bestteam-files-2026-06-17.tgz
```

They are separate on purpose: a database in use must be copied through
SQLite's backup API (a raw copy can catch a half-written page), while the
uploads directory is ordinary files for which `tar` is exactly right — so
`backup-files.sh` excludes `bestteam.db` and its `-wal`/`-shm` siblings, and
`backup-db.sh` never looks at anything else. The database alone restores a
working deployment; the files archive restores the original documents behind
each knowledge base (see "Data persistence" above for what lives where).

**Schedule both.** Nothing in the containers backs anything up on its own.
On the Docker host, one nightly cron entry (adjust the paths; the scripts must
run from the checkout so `docker compose` finds the project) is enough for the
beta:

```cron
15 3 * * * cd /opt/bestteam && ./scripts/backup-db.sh /var/backups/bestteam/bestteam-$(date +\%F).db >> /var/log/bestteam-backup.log 2>&1 && ./scripts/backup-files.sh /var/backups/bestteam/bestteam-files-$(date +\%F).tgz >> /var/log/bestteam-backup.log 2>&1
```

Prune old files with whatever you already use (`find /var/backups/bestteam
-mtime +30 -delete` in the same crontab is the simplest), and copy the
directory off the host — a backup on the disk that fails with the database is
not a backup.

**Back up `BESTTEAM_SECRETS_KEY` separately and securely** (a password manager
or secrets vault — NOT alongside the database dump). Stored mailbox passwords
are encrypted with it, so a database backup is useless for email without the
key. If the key is lost or changed, the backend refuses to start (it names the
affected org ids), but the **operator CLI still runs** — recover by clearing
and re-entering the affected mailboxes:

```bash
docker compose run --rm --no-deps backend python -m ui.backend.admin clear-email <org>
docker compose run --rm --no-deps backend python -m ui.backend.admin set-email <org> --host ... --user ... --test
```

There is no in-place re-encrypt/rekey command yet; rotating the key means
clearing and re-entering each org's mailbox under the new key.

To restore from a backup, run the restore script with the database file and,
if you have one, the files archive:

```bash
./scripts/restore.sh /path/to/backups/bestteam-2026-06-17.db /path/to/backups/bestteam-files-2026-06-17.tgz
```

It performs the steps below in order and finishes by waiting for
`/api/health` to answer 200. **Rehearse it once before the first beta customer
is on the box** — against a throwaway `docker compose` stack, not production —
so the first restore you do is not the one that matters. The manual
equivalent, if you would rather see each step:

1. Stop the backend so nothing writes to the database during restore:
   ```bash
   docker compose stop backend
   ```
2. Remove the old database's WAL/journal siblings (SQLite would otherwise
   replay them over the restored file), then copy the backup into the
   container, overwriting the live database:
   ```bash
   docker compose run --rm --no-deps --user root backend sh -c 'rm -f /app/ui/backend/data/bestteam.db-wal /app/ui/backend/data/bestteam.db-shm /app/ui/backend/data/bestteam.db-journal'
   docker compose cp /path/to/backups/bestteam-2026-06-17.db backend:/app/ui/backend/data/bestteam.db
   ```
   `docker cp` writes the file as **root**, and the backend runs as uid 1000
   -- it could read the restored database but not write to it (or migrate
   it), so hand it back before starting:
   ```bash
   docker compose run --rm --no-deps --user root backend chown 1000:1000 /app/ui/backend/data/bestteam.db
   ```
3. Restart the backend:
   ```bash
   docker compose start backend
   ```
4. Verify: `curl http://localhost:8000/api/health` returns `200`, and a
   login with a known user from before the backup succeeds.
