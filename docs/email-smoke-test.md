# Manual E2E smoke test — email toolkit

A step-by-step runbook for manually exercising the draft-only email toolkit
(PR #13) end to end: bring up the backend + frontend, log in, run the
`email_triage_demo_live` workflow against a real mailbox, and confirm reply
**drafts** appear while **nothing is ever sent**.

This is a manual, browser-driven test. Budget ~15 minutes once you have a
test mailbox and an OpenAI key.

---

## 1. What this exercises

- The four agent-facing tools — `email_find`, `email_read`,
  `email_read_attachment`, `email_draft_reply` — over one env-configured
  mailbox (`src/bestteam/tools/email_client.py`).
- The seeded built-in Skill `email_triage_reply` (platform tier,
  `org_id IS NULL`, visible to every org) that packages the triage playbook +
  those four tools. Note this runs the playbook **as currently stored** in the
  database, which on an existing deployment may lag the shipped default — if
  you're validating a change to the rule itself, adopt it first (see
  `docs/deployment.md` → "Updating built-in skills").
- The full run path: `POST /api/runs` → `RunRegistry` → `Workflow.stream()`
  on a worker thread → live trace over the WebSocket → the frontend
  monitoring dashboard.

**Safety property under test:** there is no send verb and no SMTP anywhere.
The worst outcome is a bad draft a human reviews in their own mail client.

---

## 2. Prerequisites

| Need | How to get it | Why |
|---|---|---|
| Project venv | `./.venv/Scripts/python.exe` exists | runs backend + CLI |
| Frontend deps | `ui/frontend/node_modules` present (else `cd ui/frontend && npm install`) | Vite dev server |
| A test mailbox | a compatible IMAP account **or** an M365/Graph app registration — see [§15](#15-provider-notes--mailbox-compatibility) | the tools need a real inbox |
| `OPENAI_API_KEY` | a working OpenAI key with quota | the demo uses `openai:gpt-4o-mini`; `fake:` models never call tools |
| DB at migration head | see step 4 | boots clean, no seeding warning |
| `BESTTEAM_DEMO_WORKFLOWS=1` | set at backend startup (step 7) | the bundled demo workflows are off by default; the flag exposes `email_triage_demo_live` |

> **Cost note:** the demo makes real OpenAI calls (a handful of small
> `gpt-4o-mini` requests per run). Reading is `BODY.PEEK`/read-only for IMAP,
> so triaging never marks messages as seen.

---

## 3. Prepare the test mailbox

> **Which mailbox works?** Not every provider is compatible with the current
> toolkit — notably **personal Hotmail/Outlook.com will not authenticate**.
> Read [§15 Provider notes](#15-provider-notes--mailbox-compatibility) before
> you pick one. Short version: **Gmail with an app password** is the reliable
> choice for testing.

Before running, seed the inbox so the agent has something to triage:

1. Send **2–3 emails** to the test mailbox from another account. Make at
   least one clearly "needs a reply" (e.g. *"Hi, what are your refund
   terms?"*) and one obviously informational (e.g. a newsletter) so you can
   see the agent categorize differently.
2. **Attach a file to the needs-reply one** — a small PDF or `.docx` quote,
   or a `.xlsx`/`.csv` price list. Put a distinctive phrase inside it (e.g.
   *"callout fee £95"*) that appears **nowhere in the email body**, so §10 can
   tell reading the attachment apart from reading the message. One attachment
   is enough; a second on the same message shows the manifest listing more
   than one. Keep them under 10 MB each (25 MB across one message) — above
   either limit the tool refuses to read them, which is itself a fine thing to
   try deliberately.
3. Leave them **unread** — `email_find` with no query lists unread messages.
4. Note the mailbox's Drafts folder; that's where results land.

---

## 4. Confirm the database is at migration head

```powershell
.\.venv\Scripts\python.exe -m alembic current
```

Expect `c9d0e1f2a3b4 (head)`. If it's behind, run:

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
```

The default dev DB (`ui/backend/data/bestteam.db`) is already at head.

---

## 5. Provision the login account

Public registration was removed — accounts are operator-provisioned. This test
runs the workflow as one org user; no admin account is needed. `create-user`
prompts for a password interactively.

| Account | Role | Used for |
|---|---|---|
| `demo` | org user (`default` org) | running the workflow |

Pick your own password when prompted (the examples below use `demo` as the
username; choose any password you like — it only guards this local account):

```powershell
.\.venv\Scripts\python.exe -m ui.backend.admin create-user demo --org default
```

Non-interactive, if you'd rather script it (substitute your own password):

```powershell
.\.venv\Scripts\python.exe -c "from ui.backend.db_session import SessionLocal; from ui.backend.db.users import create_user; from ui.backend.db.orgs import get_or_create_org; db=SessionLocal(); oid=get_or_create_org(db,'default').id; create_user(db,'demo','choose-a-password',org_id=oid); db.close(); print('provisioned')"
```

---

## 6. Start the frontend

```powershell
cd ui\frontend
npm run dev
```

Wait for `VITE ... ready`. It serves `http://localhost:5173` and talks to the
backend on `:8000` (allowed by the default CORS origins).

---

## 7. Start the backend (with mailbox credentials)

Run this in a **separate terminal** so your credentials stay in your own
shell (not in any transcript). Set the env vars **before** starting uvicorn,
in the same shell: a running process can't pick up variables you export in
another window, so exporting them afterward has no effect (the email backend
reads them from the process environment on each tool call, not from a config
file).

First generate a signing key for this session — don't reuse a published value:

```powershell
$env:BESTTEAM_SECRET_KEY = $(.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_hex(32))")
```

**IMAP mailbox:**

```powershell
$env:OPENAI_API_KEY="sk-..."
$env:BESTTEAM_DEMO_WORKFLOWS="1"
$env:BESTTEAM_EMAIL_BACKEND="imap"
$env:BESTTEAM_IMAP_HOST="imap.yourhost.com"
$env:BESTTEAM_IMAP_PORT="993"
$env:BESTTEAM_IMAP_USER="you@yourhost.com"
$env:BESTTEAM_IMAP_PASSWORD="your-app-password"
.\.venv\Scripts\python.exe -m uvicorn ui.backend.main:app --port 8000 --host 127.0.0.1
```

**Microsoft 365 / Graph mailbox:** same signing key (above) plus the
`OPENAI_API_KEY` and `BESTTEAM_DEMO_WORKFLOWS` lines, then:

```powershell
$env:BESTTEAM_EMAIL_BACKEND="graph"
$env:BESTTEAM_GRAPH_TENANT_ID="..."
$env:BESTTEAM_GRAPH_CLIENT_ID="..."
$env:BESTTEAM_GRAPH_CLIENT_SECRET="..."
$env:BESTTEAM_GRAPH_MAILBOX="support@yourcompany.com"
.\.venv\Scripts\python.exe -m uvicorn ui.backend.main:app --port 8000 --host 127.0.0.1
```

Notes:
- Generate your own `BESTTEAM_SECRET_KEY` (above) rather than reusing any
  value from the repo. The startup guard only rejects the two known
  placeholders (`bestteam-dev-secret-change-me` and the `.env.example`
  default); it does not vouch for anything else, so treat the key as a secret.
- `BESTTEAM_DEMO_WORKFLOWS="1"` is **required for this test**. The bundled
  YAML workflows (including `email_triage_demo_live`) are off by default —
  they're demo fixtures, not customer teams — so without this flag the
  workflow won't appear in the list and running it by name returns 404. Leave
  it unset on a real deployment. See `.env.example` / `docs/deployment.md`.
- This is a **single-org** deployment (only the `default` org), so the CR-031
  guard permits `BESTTEAM_EMAIL_*`. If you ever add a second org to this DB,
  the backend will refuse to start while email is configured — by design.

---

## 8. Verify services are healthy

```bash
curl http://127.0.0.1:8000/api/health
```

Expect a 200. Then confirm login works and `demo` is scoped to the `default`
org:

```bash
curl -s -X POST http://127.0.0.1:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"demo","password":"<the password you set in step 5>"}'
# -> {"access_token":"...","token_type":"bearer"}
```

---

## 9. Run the workflow (browser — the primary path)

1. Open `http://localhost:5173` and log in as **`demo`** with the password you
   set in step 5. Logging in lands you on the Dashboard (or the wizard, if the
   org has nothing deployed) — click **Run a team** in the nav to reach the
   run page at `/run`.
2. On the run page, select the **`email_triage_demo_live`**
   workflow. It's a bundled YAML demo (`ui/backend/workflows/`), so it only
   appears when `BESTTEAM_DEMO_WORKFLOWS="1"` is set — which you did in step 7.
   If it's missing from the list, that flag isn't set on the running backend.
3. Enter an input such as:
   > `Triage my unread emails and draft replies.`
4. Start the run and watch the live trace stream.

### What you should see in the trace

The agent works the playbook, and these tool calls appear in order (repeating
per message):

- `email_find` (no query) → lists unread messages
- `email_read` → fetches one message body, ending in an
  `Attachments (N):` block naming each file, its type and its size — this is
  a *list*, never the contents
- `email_read_attachment` → only for the message you attached a file to, and
  only when the playbook judges the attachment worth opening (the step is
  deliberately conditional, so a run that skips an obviously irrelevant
  attachment is correct behaviour, not a failure)
- `email_draft_reply` → saves a draft for a *needs-reply* message only

The run ends with a `run_completed` event whose summary lists every message
seen, its category (needs-reply / FYI / spam / escalate), and whether a draft
was created.

The trace shows *that* an attachment was read, never what it said: the
`email_read_attachment` event is redacted to a summary + message id, exactly
like the other email tools. Extracted text appearing in the trace is a bug.

---

## 10. Verify the outcome

- **In the mailbox:** open the **Drafts** folder — there should be a threaded
  reply draft for each *needs-reply* message (correct `In-Reply-To` /
  `References`, or a Graph `createReply` draft).
- **Nothing sent:** check Sent Items — it must be **empty** of anything from
  this run. The toolkit has no send path.
- **Nothing marked read (IMAP):** the unread messages should still be unread
  (`BODY.PEEK` is read-only).
- **The attachment was really read (IMAP):** if the agent opened it, the
  distinctive phrase you hid inside the file (§3 step 2) should turn up in the
  draft or the run summary. If it doesn't, check the trace: no
  `email_read_attachment` call at all means the playbook judged it not worth
  opening — re-run with an input that asks for the attachment's contents
  explicitly. **On the Graph backend there is no attachment reading at all**:
  `email_read` renders no manifest and the tool answers "This mailbox
  connection cannot read attachments." That is the current, documented
  limitation, not a failure of this test.

If all of these hold, the smoke test passes.

---

## 11. Optional — scripted run via the API

Instead of the browser, drive it with the token from step 8 (an org-user
token; `POST /api/runs` is an org-user surface):

```bash
TOKEN="<access_token from step 8>"

# list workflows the org can see (includes the YAML demo when
# BESTTEAM_DEMO_WORKFLOWS=1 is set on the backend, per step 7)
curl -s http://127.0.0.1:8000/api/workflows -H "Authorization: Bearer $TOKEN"

# start the run
curl -s -X POST http://127.0.0.1:8000/api/runs \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"workflow":"email_triage_demo_live","input":"Triage my unread emails and draft replies."}'
# -> {"run_id":"..."}

# poll the run (events accumulate on the record)
curl -s http://127.0.0.1:8000/api/runs/<run_id> -H "Authorization: Bearer $TOKEN"
```

---

## 12. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Backend `RuntimeError: BESTTEAM_SECRET_KEY ... placeholder` on boot | secret key unset or a known placeholder | set a non-placeholder `BESTTEAM_SECRET_KEY` (step 7) |
| Backend `RuntimeError` about email + more than one org | CR-031 guard: `BESTTEAM_EMAIL_*` set with >1 org | keep a single org, or unset the email vars |
| Boot warning "Skipping default-data seeding ... schema predates the latest migration" | DB behind head | `alembic upgrade head`, restart |
| `email_triage_demo_live` missing from the workflow list (or `POST /api/runs` returns 404 for it) | `BESTTEAM_DEMO_WORKFLOWS` not set — bundled demos are off by default | set `$env:BESTTEAM_DEMO_WORKFLOWS="1"` before starting the backend (step 7) |
| Login returns 401 | wrong password, or account not provisioned | re-run step 5 |
| Run fails with "Unknown skill 'email_triage_reply'" | skill not seeded (DB predates the skill, or seeding was skipped on a pre-migration schema) | restart the backend — built-in skills are seeded on boot; if it persists, run `alembic upgrade head` first |
| Trace shows the agent answering **without** any `email_*` tool calls | `OPENAI_API_KEY` missing/invalid, or a `fake:` model | set a real key; `fake:` models never call tools |
| `email_find` errors with auth failure | bad IMAP/Graph credentials | verify creds; for Graph, confirm `Mail.ReadWrite` **application** permission + Application Access Policy scoped to the mailbox |
| `email_find` errors with a TLS/certificate verification failure | IMAP server presents an untrusted (e.g. private/self-signed) certificate — the client always verifies TLS, there is no verify-off switch by design | trust the server's CA: point `SSL_CERT_FILE` at a PEM bundle that includes it before starting the backend (public providers like Gmail/O365 need nothing) |
| `email_read` lists no `Attachments (N):` block on a message that has one | Graph backend (no attachment support), or the part carries no filename | use the IMAP backend for this check — see §10 |
| `email_read_attachment` answers "couldn't be read" or names a type it cannot read | the file's suffix is unsupported (archives are refused, never expanded), or it lies about its format and the parser gave up | attach a `.pdf`/`.docx`/`.xlsx`/`.csv`/`.txt` instead; PDF/Excel/Word need `pip install 'bestteam[tools-files]'` |
| Frontend loads but every API call fails | backend not on `:8000`, or CORS | confirm backend port; default CORS allows `localhost:5173` |
| Drafts don't appear but the run completed | wrong Drafts folder detected (IMAP) | set `BESTTEAM_IMAP_DRAFTS` to the exact folder name |

---

## 13. Teardown

- Stop the backend (Ctrl-C in its terminal) and the frontend dev server.
- The `demo` account and any drafts you created persist; delete the drafts
  from your mail client if you don't want them.
- Nothing needs to be reverted in the repo — no code changes are part of
  running this test.

---

## 14. Out of scope

- **Sending** email (no send verb exists in the process — deliberate; the
  containment argument for the draft-only toolkit depends on it).
- **Ambient triggering** on new mail. This is built (the per-org autonomous
  email trigger, `ui/backend/email_trigger.py`), but it runs on the per-org
  platform path, not the process-wide env path this runbook exercises — its
  own checks live in the "Microsoft 365 / Exchange Online mailbox (per-org,
  OAuth)" section at the end of this document.
- **Per-org mailboxes.** This test uses the process-wide `BESTTEAM_EMAIL_*`
  mailbox — the single-org / SDK path. A multi-org platform instead connects
  each customer's mailbox per-org with `admin set-email <org>` (encrypted at
  rest), which is what a deployed workflow uses at run time; the process-wide
  env vars stay refused on a multi-org deployment (CR-031). This runbook
  exercises the single-org env path; see `docs/deployment.md` §4c for the
  per-org setup.

---

## 15. Provider notes — mailbox compatibility

The toolkit has two backends, and each speaks a specific auth protocol, so
not every provider works. The IMAP backend does **basic auth** — plain
`conn.login(user, password)` (`src/bestteam/tools/email_client.py`), no
OAuth2/XOAUTH2. The Graph backend does **app-only client-credentials OAuth**
against an Azure AD tenant (`login.microsoftonline.com/<tenant>/oauth2/...`).

| Provider | Backend | Works? | Notes |
|---|---|---|---|
| **Gmail** | `imap` | ✅ Yes (recommended) | IMAP basic auth via a Google **app password** (needs 2-Step Verification on) |
| **Fastmail / Yahoo / self-hosted IMAP** | `imap` | ✅ Usually | Works if the server still allows password IMAP; most now require an app-specific password |
| **Microsoft 365 (work/school)** | `graph` | ✅ Yes | App registration + `Mail.ReadWrite` **application** permission + Exchange Application Access Policy scoped to the mailbox |
| **Hotmail / Outlook.com / Live (personal)** | — | ❌ No | Basic-auth IMAP is disabled by Microsoft (Modern Auth/OAuth required for personal accounts since late 2024); the Graph app-only flow is tenant-only, not available for personal accounts |

### Gmail (recommended for testing)

```powershell
$env:BESTTEAM_EMAIL_BACKEND="imap"
$env:BESTTEAM_IMAP_HOST="imap.gmail.com"
$env:BESTTEAM_IMAP_PORT="993"
$env:BESTTEAM_IMAP_USER="youraddress@gmail.com"
$env:BESTTEAM_IMAP_PASSWORD="<16-char app password>"
$env:BESTTEAM_IMAP_DRAFTS="[Gmail]/Drafts"
```

Create the app password at **Google Account → Security → 2-Step Verification
→ App passwords**. Gmail's Drafts folder is `[Gmail]/Drafts` (not `Drafts`).

### Microsoft 365 (work/school) — Graph backend

```powershell
$env:BESTTEAM_EMAIL_BACKEND="graph"
$env:BESTTEAM_GRAPH_TENANT_ID="..."
$env:BESTTEAM_GRAPH_CLIENT_ID="..."
$env:BESTTEAM_GRAPH_CLIENT_SECRET="..."
$env:BESTTEAM_GRAPH_MAILBOX="support@yourcompany.com"
```

Least privilege: grant the `Mail.ReadWrite` **application** permission and
restrict it to the single test mailbox with an Exchange Application Access
Policy.

### Hotmail / Outlook.com / Live (personal) — not supported

A personal Microsoft account can't be tested with the toolkit as it stands:

- **IMAP backend:** the `outlook.office365.com:993` server no longer accepts
  password login for personal accounts (Microsoft disabled basic auth /
  removed app passwords for consumer mailboxes), so `conn.login(...)` fails
  with `AUTHENTICATE failed`. For reference, the settings *would* be
  `BESTTEAM_IMAP_HOST=outlook.office365.com`, `BESTTEAM_IMAP_DRAFTS=Drafts`.
- **Graph backend:** the app-only client-credentials flow requires an Azure
  AD tenant you control; a personal Hotmail isn't in such a tenant.

XOAUTH2 now exists in the IMAP backend (see the next section), but only with
the **app-only client-credentials** grant, which needs an Azure tenant. A
personal account would need the *delegated* authorisation-code flow instead,
which is not built. Use Gmail (or a work M365 mailbox) to run this test.

---

## 9. Microsoft 365 / Exchange Online mailbox (per-org, OAuth)

**This section is the only verification that a live Exchange Online tenant
accepts this flow.** Every test in the repository runs against fakes: they
prove the SASL string, the token lifecycle, the storage round-trip and the
error mapping, but nothing in CI can prove that Microsoft accepts the resulting
`AUTHENTICATE XOAUTH2`. Run this before selling M365 support to a customer.

Budget ~30 minutes, most of it the Azure setup.

### 9.1 Set up the app registration

Follow `docs/deployment.md` → "Microsoft 365 mailboxes", steps 1–4. You need a
work/school M365 tenant you administer, and a test mailbox in it.

### 9.2 Connect it in the wizard

1. Log in as an org member and reach the "Connect your mailbox" step.
2. Choose **Microsoft 365 / Outlook (Exchange Online)**. Confirm the server
   address and port fields disappear.
3. Enter the mailbox address, Directory (tenant) ID, Application (client) ID
   and client secret, then **Test connection**.

### 9.3 Confirm each failure mode says something actionable

Each of these has a different fix, so each must produce a different message.
Test them by changing one value at a time:

| Change | Expected message |
|---|---|
| Wrong client secret | names the Application (client) ID / client secret |
| Wrong tenant ID | names the Directory (tenant) ID |
| Correct credentials, but skip `Add-MailboxPermission` | names `IMAP.AccessAsApp`, `New-ServicePrincipal` and `Add-MailboxPermission` |
| Mailbox address not in the tenant | the same access message, naming that address |

The third row is the important one — a working token with a refused mailbox is
the likeliest outcome of a half-finished setup, and it is indistinguishable
from a wrong secret unless the message says so.

### 9.4 Run the triage end to end

With the mailbox connected, repeat sections 5–7 above against it: send a test
message to the mailbox, run the email team, and confirm a reply **draft**
appears in its Drafts folder and **nothing is sent**. Then enable the automatic
trigger and confirm a new message starts a run on its own.

Watch for two things specific to this path:

- **Drafts folder resolution.** Exchange Online reports its Drafts folder via
  SPECIAL-USE, but a mailbox with a localised display name is worth confirming.
- **Token refresh.** Leave the trigger running for over an hour and confirm
  runs still succeed. The access token lasts about an hour and is refreshed
  60 seconds before it expires; a stall at the one-hour mark would mean the
  refresh is not happening.
