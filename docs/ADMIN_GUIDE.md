# Admin Guide — Organizations, Users & Accounts

How a **platform operator** provisions and manages customer organizations and
their user accounts on a `bestteam` deployment.

There is **no public registration and no self-service org creation** — orgs,
users, and admin rights are all granted deliberately, out-of-band.

Two ways to do it:

- **Web UI (Accounts page).** Platform admins can do everyday provisioning —
  create organizations, deactivate/reactivate them, and create/reset-password/
  move/delete each org's member — from the **Accounts** page in the admin nav.
- **Operator CLI (`python -m ui.backend.admin`).** The full surface, and the
  **only** way to grant admin (`promote`/`demote`), manage platform
  operator/admin accounts, and **bootstrap the first admin** (chicken-and-egg:
  you must already be an admin to reach the Accounts page).

This guide documents the CLI (the complete surface); the Accounts page mirrors
the org/user subset of it.

---

## 1. Concepts

| Term | What it is | `org_id` | `is_admin` |
|------|-----------|:--------:|:----------:|
| **Organization** | A customer tenant. All customer data (teams, runs, mailbox, memory) is row-isolated by org. The `default` org is seeded automatically. | — | — |
| **Org member** | A customer login that belongs to exactly one organization. Uses the customer UI: *Build a team*, *My teams*, *Run a team*, *Activity*. | set | `false` |
| **Platform operator** | An org-less account. Candidate for admin; runs the deployment rather than using a team. | `NULL` | `false` until promoted |
| **Platform admin** | A platform operator that has been `promote`d. Gets the **Advanced** (config) and **Memory** pages and can target any org's config via `?org=`. | `NULL` | `true` |

**Core rules the CLI enforces** (you'll see these as errors if you cross them):

- **Usernames are globally unique** across all orgs (they key the login token and per-user memory).
- **One member per org.** Creating or moving a second user into an org is refused. (Org resources such as the shared mailbox have no per-member privilege separation yet, so a second member would mean unprivileged co-management.) Platform operators are exempt — there can be many.
- **Admin and org membership are mutually exclusive** (CR-030). You cannot promote an org member, and you cannot move an admin into an org without demoting first. To get an admin, create a `--platform` account and promote it.
- **`email-trigger` is a reserved username** (used for autonomous runs) and cannot be created.

---

## 2. Running the CLI

Run it **with the same environment and database as the backend** (same
`BESTTEAM_DB_PATH`, and — if the deployment uses per-user memory —
`BESTTEAM_MEMORY_DB`; see §6). All commands take the form:

```
python -m ui.backend.admin <command> [args]
```

**In a container deployment:**

```bash
docker compose exec backend python -m ui.backend.admin list-orgs
```

**Local / dev (Windows venv):**

```powershell
.\.venv\Scripts\python.exe -m ui.backend.admin list-orgs
```

Password-taking commands (`create-user`, `set-email`) prompt interactively and
never accept the password as an argument — so it never lands in shell history.

---

## 3. Command reference

| Command | What it does |
|---------|--------------|
| `create-org <name> [--display-name "Nice Name"]` | Create a customer organization. |
| `list-orgs` | List organizations (name + display name). |
| `deactivate-org <name>` | Suspend an org (reversible): its member can't log in, its org-scoped surfaces 403, and its autonomous email trigger pauses. Data is kept. |
| `activate-org <name>` | Reactivate a deactivated org. |
| `create-user <username> [--org <name>` \| `--platform]` | Create a login. `--org` (default: `default`) = org member; `--platform` = platform operator. Prompts for the password. |
| `move-user <username> (--to-org <name>` \| `--platform)` | Move a user to another org, or convert to a platform operator. |
| `delete-user <username>` | Delete an account (and purge its per-user memory — see §6). |
| `promote <username>` | Grant admin. Target must be a platform operator (org-less). |
| `demote <username>` | Revoke admin. Always allowed. |
| `list` | List current admins. |
| `set-email <org> --host <h> --user <u> [--port 993] [--drafts <folder>] [--test]` | Connect an org's IMAP mailbox for the email tools. Prompts for the password. `--test` verifies with a real login before saving. |
| `clear-email <org>` | Disconnect an org's mailbox (also disables its autonomous email trigger). |

> **Note on visibility:** there is intentionally no "list all users" or "list org
> members" command. `list` shows admins; `list-orgs` shows orgs. To see a
> specific user, act on them by name (an unknown name gives a clear error).

---

## 4. Common workflows

### 4.1 Bootstrap the first admin

A fresh deployment has no admin. Create an org-less account and promote it:

```bash
python -m ui.backend.admin create-user op --platform      # prompts for a password
python -m ui.backend.admin promote op
python -m ui.backend.admin list                            # -> op
```

`op` can now log in and reach the **Advanced** and **Memory** pages. (A platform
operator is routed to *Advanced* as their home; the customer pages are hidden
from them because they have no org.)

### 4.2 Onboard a customer organization

```bash
python -m ui.backend.admin create-org acme --display-name "Acme Corp"
python -m ui.backend.admin create-user alice --org acme    # prompts for a password
```

`alice` can now log in and use *Build a team* / *My teams* / *Run a team* /
*Activity* within Acme. If Acme's teams use email, connect its mailbox (§5).

### 4.3 Single-customer deployment

The `default` org exists out of the box, so you can skip `create-org`:

```bash
python -m ui.backend.admin create-user alice               # defaults to --org default
```

### 4.4 Reset a customer's password / replace a user

There is no password-reset command; recreate the account:

```bash
python -m ui.backend.admin delete-user alice               # frees the org's single slot + purges memory
python -m ui.backend.admin create-user alice --org acme    # set the new password at the prompt
```

### 4.5 Move a user between orgs

```bash
python -m ui.backend.admin move-user alice --to-org beta   # destination must have no member
```

To turn an org member into an operator (or vice-versa):

```bash
python -m ui.backend.admin move-user alice --platform      # alice becomes org-less
python -m ui.backend.admin promote alice                   # (optional) make them an admin
```

To move an **admin** into an org, demote first:

```bash
python -m ui.backend.admin demote alice
python -m ui.backend.admin move-user alice --to-org acme
```

---

## 5. Connecting an organization's mailbox

Teams that use the email tools need the org's own IMAP mailbox connected.
Operators can do it from the CLI; customers can also self-serve it in the UI.

```bash
python -m ui.backend.admin set-email acme \
  --host imap.gmail.com --user support@acme.com --test
# prompts for the mailbox password; --test does a real login before saving
```

- The password is stored **encrypted** (requires `BESTTEAM_SECRETS_KEY`, a key
  distinct from the login-token secret). It is never returned in plaintext.
- Replacing or clearing a mailbox automatically disables that org's autonomous
  email trigger, so a just-disconnected mailbox is never polled.
- `clear-email acme` disconnects it.

> **Multi-org constraint (CR-031):** the process-wide `BESTTEAM_EMAIL_*` env
> vars are refused once a deployment has more than one org — each org must use
> its own stored mailbox instead, so one tenant can't reach another's inbox.

---

## 6. Per-user memory and account deletion

If the deployment runs per-user memory (`BESTTEAM_MEMORY_DB` set), account
lifecycle commands also manage that memory — **so run them with the server's
environment**:

- **`delete-user`** purges the account's memory *before* releasing the username,
  so a later account that reuses the name can't recall the old data. It **fails
  closed**: if the purge errors, the user is *not* deleted. If `BESTTEAM_MEMORY_DB`
  is unset (or its file is absent) for the command, it deletes the account but
  prints a loud warning that **no memory was purged** — re-run with the server's
  environment before the username is reused.
- **`move-user`** binds the user's legacy (pre-org-scoping) memory to their
  *source* org before moving, so old records stay attributable to the org they
  were created under rather than following the user.

Admins can also view, search, and delete a user's memory, and perform org-level
erasure, from the **Memory** page (`/api/memory`).

---

## 7. Environment prerequisites

| Variable | Needed for |
|----------|-----------|
| `BESTTEAM_SECRET_KEY` | **Required to boot.** Signs login tokens. |
| `BESTTEAM_DB_PATH` | The deployment database. The CLI must point at the same DB as the backend (default `ui/backend/data/bestteam.db`). |
| `BESTTEAM_SECRETS_KEY` | Encrypting stored mailbox passwords (`set-email`). |
| `BESTTEAM_MEMORY_DB` | Per-user memory. Set it for `delete-user`/`move-user` so memory is purged/reconciled correctly (§6). |

---

## 8. Troubleshooting — the errors you'll hit

| Message | Cause / fix |
|---------|-------------|
| `Username '<x>' is already taken` | Usernames are global. Pick another, or `delete-user` the old one first. |
| `Organization already has a member ('<y>')` | One member per org. Move/delete the existing member, or use a different org. |
| `User '<x>' belongs to an organization; admin is platform-wide…` | You tried to `promote` an org member. Create a `--platform` account instead. |
| `User '<x>' is an admin; admin … can't be org-bound` | You tried to `move-user … --to-org` an admin. `demote` first. |
| `Unknown organization '<x>'. Create it first with create-org.` | The org doesn't exist. `create-org` it, or check `list-orgs`. |
| `'email-trigger' is reserved for autonomous runs.` | That username is reserved. Pick another. |
| `Login test failed, not saved` / `Could not reach '<host>:<port>'` | `set-email --test` couldn't authenticate/connect. Fix the credentials or host/port; nothing was saved. |

---

*All provisioning is CLI-only by design — there is no registration endpoint and
no way to grant admin from an env list or username match, so the first admin and
every org are created deliberately by an operator with deployment access.*
