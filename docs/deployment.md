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

## 2. Build and start

```bash
docker compose build
docker compose up -d
```

`docker compose` automatically loads `.env` from the project root to
substitute `${VITE_API_BASE}`/`${VITE_WS_BASE}` in `docker-compose.yml` (this
is separate from the backend's `env_file: .env`), so the values you set in
step 1 are baked into the frontend image at build time.

## 3. Apply database migrations

```bash
docker compose exec backend alembic upgrade head
```

Run this once after the first `docker compose up -d`, and again after
pulling any update that includes a new file under `alembic/versions/`. This
is the canonical way the database schema is created/updated going forward
(replacing a bare `Base.metadata.create_all()`, which still runs
automatically as a harmless no-op safety net on a brand-new database).

**Run migrations promptly after an upgrade.** `create_all()` never adds a new
index/constraint to a pre-existing table, so a security invariant introduced by
a migration (currently: one member per org) isn't in force until
`alembic upgrade head` runs. As a backstop the backend **refuses to start**
(HTTP) while that invariant is violated — see the recovery procedure below —
so the window can't be served through, but you still complete the upgrade by
running the migration.

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

The email tools (`email_find`/`email_read`/`email_draft_reply`) read one mailbox
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
process mail. Leader election is future work; until then, one worker.

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

## 5. Verify

- `curl http://localhost:8000/api/health` → `200 {"status": "ok"}` (public,
  no auth required).
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

The SQLite database (agents, teams, workflows, users — config persistence,
not run history) lives in the `bestteam_data` named volume, mounted at
`/app/ui/backend/data` in the backend container. It survives
`docker compose restart` / `docker compose down` (without `-v`).

## Backup and restore

Back up the live database (safe to run while the backend is running):

```bash
./scripts/backup-db.sh
# or with an explicit path:
./scripts/backup-db.sh /path/to/backups/bestteam-2026-06-17.db
```

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

To restore from a backup:

1. Stop the backend so nothing writes to the database during restore:
   ```bash
   docker compose stop backend
   ```
2. Copy the backup file into the container, overwriting the live database:
   ```bash
   docker compose cp /path/to/backups/bestteam-2026-06-17.db backend:/app/ui/backend/data/bestteam.db
   ```
3. Restart the backend:
   ```bash
   docker compose start backend
   ```
4. Verify: `curl http://localhost:8000/api/health` returns `200`, and a
   login with a known user from before the backup succeeds.
