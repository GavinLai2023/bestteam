"""Operator CLI for provisioning orgs, users, and admin rights.

There is no public registration and no self-service org creation: customer
organisations, their user accounts, and admin rights are all provisioned
deliberately here by the platform operator. Run inside the deployment, e.g.:

    docker compose exec backend python -m ui.backend.admin create-org acme --display-name "Acme Corp"
    docker compose exec backend python -m ui.backend.admin create-user alice --org acme
    docker compose exec backend python -m ui.backend.admin create-user op --platform
    docker compose exec backend python -m ui.backend.admin delete-user alice
    docker compose exec backend python -m ui.backend.admin move-user alice --to-org beta
    docker compose exec backend python -m ui.backend.admin promote op
    docker compose exec backend python -m ui.backend.admin demote <username>
    docker compose exec backend python -m ui.backend.admin list
    docker compose exec backend python -m ui.backend.admin list-orgs
    docker compose exec backend python -m ui.backend.admin set-email acme --host imap.gmail.com --user support@acme.com --test
    docker compose exec backend python -m ui.backend.admin clear-email acme

`create-user --platform` creates a platform operator (no org); org members
are created with `--org <name>` (default: the `default` org). Admin rights
are granted only via `promote` -- never from an env list or username match
(which would let an attacker pre-claim a configured username).
"""

from __future__ import annotations

import argparse
import getpass
from typing import Optional, Sequence

from . import email_trigger
# Shared with the admin API (admin_api.py); kept under these module-level names
# so the CLI's call sites and tests that monkeypatch them are unchanged.
from .account_memory import open_memory_store as _open_memory_store
from .account_memory import purge_user_memory as _purge_user_memory
from .account_memory import reconcile_legacy_org as _reconcile_legacy_org
from .db.email_credentials import clear_email_credentials, get_email_credentials, set_email_credentials
from .db.models import User
from .db.orgs import (
    DEFAULT_ORG_NAME,
    create_org,
    ensure_email_single_org,
    get_org_by_name,
    list_orgs,
    set_org_active,
)
from .db.users import (
    create_user,
    delete_user,
    get_user_by_username,
    orgs_with_multiple_members,
    set_admin_status,
    set_user_org,
)
from .db_session import SessionLocal


def _prompt_password(parser: argparse.ArgumentParser) -> str:
    password = getpass.getpass("Password: ")
    if not password:
        parser.error("Password must not be empty")
    if getpass.getpass("Repeat password: ") != password:
        parser.error("Passwords do not match")
    return password


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ui.backend.admin", description="Provision orgs, users, and admins."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    promote = sub.add_parser("promote", help="grant admin to an existing user")
    promote.add_argument("username")
    demote = sub.add_parser("demote", help="revoke admin from a user")
    demote.add_argument("username")
    sub.add_parser("list", help="list current admins")

    create_org_p = sub.add_parser("create-org", help="create a customer organization")
    create_org_p.add_argument("name")
    create_org_p.add_argument("--display-name", default="")
    sub.add_parser("list-orgs", help="list organizations")

    deactivate_org_p = sub.add_parser(
        "deactivate-org", help="deactivate an org (reversible full suspend)"
    )
    deactivate_org_p.add_argument("name")
    activate_org_p = sub.add_parser("activate-org", help="reactivate a deactivated org")
    activate_org_p.add_argument("name")

    create_user_p = sub.add_parser("create-user", help="create a user (prompts for password)")
    create_user_p.add_argument("username")
    org_group = create_user_p.add_mutually_exclusive_group()
    org_group.add_argument(
        "--org", default=DEFAULT_ORG_NAME, help=f"organization name (default: {DEFAULT_ORG_NAME})"
    )
    org_group.add_argument(
        "--platform", action="store_true",
        help="create a platform operator that belongs to no organization",
    )

    delete_user_p = sub.add_parser(
        "delete-user", help="delete a user account (e.g. a duplicate org member)"
    )
    delete_user_p.add_argument("username")

    sub.add_parser(
        "backfill-memory-principals",
        help="bind current users' legacy (NULL-principal) memory rows to their "
        "principal, so existing memory keeps being recalled after upgrade (opt-in)",
    )

    move_user_p = sub.add_parser(
        "move-user", help="move a user to another org or to a platform operator"
    )
    move_user_p.add_argument("username")
    move_group = move_user_p.add_mutually_exclusive_group(required=True)
    move_group.add_argument("--to-org", help="destination organization name")
    move_group.add_argument(
        "--platform", action="store_true", help="make the user a platform operator (no org)"
    )

    set_email_p = sub.add_parser(
        "set-email", help="connect an org's IMAP mailbox (prompts for the password)"
    )
    set_email_p.add_argument("org", help="organization name")
    set_email_p.add_argument("--host", required=True, help="IMAP host, e.g. imap.gmail.com")
    set_email_p.add_argument("--user", required=True, help="IMAP username / email address")
    set_email_p.add_argument("--port", type=int, default=993)
    set_email_p.add_argument("--drafts", default=None, help="Drafts folder name (auto-detected if omitted)")
    set_email_p.add_argument(
        "--test", action="store_true", help="verify the credentials with a login before saving"
    )

    clear_email_p = sub.add_parser("clear-email", help="disconnect an org's mailbox")
    clear_email_p.add_argument("org", help="organization name")

    args = parser.parse_args(argv)

    with SessionLocal() as db:
        if args.command == "list":
            for user in db.query(User).filter_by(is_admin=True).order_by(User.username).all():
                print(user.username)
            return 0
        if args.command == "list-orgs":
            for org in list_orgs(db):
                suffix = f" ({org.display_name})" if org.display_name else ""
                print(f"{org.name}{suffix}")
            return 0
        if args.command == "create-org":
            try:
                # A second org + process-wide email creds would expose the
                # configured mailbox to every tenant (CR-031).
                ensure_email_single_org(db, creating=1)
                org = create_org(db, args.name, args.display_name)
            except (RuntimeError, ValueError) as exc:
                parser.error(str(exc))
            print(f"Created organization '{org.name}'.")
            return 0
        if args.command in ("activate-org", "deactivate-org"):
            active = args.command == "activate-org"
            try:
                set_org_active(db, args.name, active)
            except ValueError as exc:
                parser.error(str(exc))
            print(f"Organization '{args.name}' is now {'active' if active else 'deactivated'}.")
            return 0
        if args.command == "create-user":
            if args.platform:
                org_id = None
            else:
                org = get_org_by_name(db, args.org)
                if org is None:
                    parser.error(
                        f"Unknown organization '{args.org}'. Create it first with create-org."
                    )
                org_id = org.id
            password = _prompt_password(parser)
            try:
                create_user(db, args.username, password, org_id=org_id)
            except ValueError as exc:
                parser.error(str(exc))
            where = "platform operator" if args.platform else f"member of '{args.org}'"
            print(f"Created user '{args.username}' ({where}).")
            return 0
        if args.command == "backfill-memory-principals":
            # Opt-in reconciliation (deletion-lifecycle): rows written before
            # principal stamping have principal_id NULL and aren't recalled by a
            # stamped run. Bind each current user's NULL-principal rows (in their
            # own org scope) to their principal, so their existing memory keeps
            # being recalled. NULL-principal-only + per-(user, org)-scoped, so it
            # can never re-attribute another principal's or another org's rows.
            store = _open_memory_store()
            if store is None:
                print(
                    "BESTTEAM_MEMORY_DB is not set (or its file is absent) for this "
                    "command; nothing to backfill. Re-run with the server's environment."
                )
                return 0
            try:
                total = 0
                for user in db.query(User).all():
                    if user.principal_id is None:
                        continue
                    total += store.assign_null_principal(
                        user.username, user.org_id, user.principal_id
                    )
            finally:
                store.close()
            print(f"Backfilled {total} legacy memory record(s) to their account principal.")
            return 0
        if args.command == "delete-user":
            # Validate BEFORE any mutation (review r3 #4): an unknown username must
            # not purge orphaned memory and then error. (Cross-DB deletion isn't
            # atomic; validate-first + purge-then-delete keeps a failed delete from
            # destroying memory for a still-present account, and fails safe -- if
            # the account row survives, its memory is gone, which is not a leak.)
            target = get_user_by_username(db, args.username)
            if target is None:
                parser.error(f"No such user: {args.username!r}")
            # Fail closed: purge the user's memory AND retire its principal BEFORE
            # releasing the username, so a recreated same-named account can't recall
            # it and an in-flight run's late write is dropped (review r2 #2 +
            # deletion-lifecycle findings 1 & 2).
            try:
                purged = _purge_user_memory(args.username, principal_id=target.principal_id)
            except Exception as exc:  # noqa: BLE001 -- fail closed on any purge error
                parser.error(
                    f"Aborted: could not purge memory for '{args.username}' "
                    f"({exc}); user not deleted."
                )
            delete_user(db, args.username)
            if purged is None:
                # No store configured for THIS invocation -- warn loudly rather
                # than imply memory was cleaned (review r3 #1). If the deployment
                # uses memory, the operator must run with the server's environment.
                print(
                    f"Deleted user '{args.username}'. WARNING: BESTTEAM_MEMORY_DB is not set "
                    f"(or its file is absent) for this command, so NO per-user memory was purged. "
                    f"If this deployment uses per-user memory, re-run delete-user with the same "
                    f"environment as the server before the username is reused."
                )
            else:
                suffix = f" Purged {purged} memory record(s)." if purged else ""
                print(f"Deleted user '{args.username}'.{suffix}")
            return 0
        if args.command == "move-user":
            user = get_user_by_username(db, args.username)
            if user is None:
                parser.error(f"No such user: {args.username!r}")
            source_org_id = user.org_id
            if args.platform:
                org_id = None
                where = "platform operator"
            else:
                org = get_org_by_name(db, args.to_org)
                if org is None:
                    parser.error(f"Unknown organization '{args.to_org}'. Create it first with create-org.")
                org_id = org.id
                where = f"member of '{args.to_org}'"
            # Bind any legacy NULL-org memory to the SOURCE org before moving, so
            # pre-SP-2 rows stay attributable to the org they were created under
            # instead of following the user to a new org (review r3 #3). Fail
            # closed: don't move while memory is inconsistent.
            if source_org_id is not None:
                try:
                    _reconcile_legacy_org(args.username, source_org_id)
                except Exception as exc:  # noqa: BLE001 -- fail closed on reconcile error
                    parser.error(
                        f"Aborted: could not reconcile memory for '{args.username}' "
                        f"({exc}); user not moved."
                    )
            try:
                set_user_org(db, args.username, org_id)
            except ValueError as exc:
                parser.error(str(exc))
            print(f"Moved user '{args.username}' -> {where}.")
            return 0
        if args.command == "set-email":
            org = get_org_by_name(db, args.org)
            if org is None:
                parser.error(f"Unknown organization '{args.org}'. Create it first with create-org.")
            password = _prompt_password(parser)
            if args.test:
                # Build the same backend the tools use and attempt a login, so a
                # bad credential is caught here rather than at first run.
                from bestteam.exceptions import ConfigurationError
                from bestteam.tools.email_client import _ImapBackend

                backend = _ImapBackend(
                    host=args.host, user=args.user, password=password,
                    port=args.port, drafts=args.drafts, restrict_to_public=True,
                )
                try:
                    conn = backend._connect()
                    conn.logout()
                except ConfigurationError as exc:
                    parser.error(f"Login test failed, not saved: {exc}")
                except OSError as exc:
                    parser.error(f"Could not reach '{args.host}:{args.port}', not saved: {exc}")
            prior = get_email_credentials(db, org.id)
            prior_identity = (prior.host, prior.username) if prior is not None else None
            try:
                set_email_credentials(
                    db, org.id, host=args.host, username=args.user, password=password,
                    port=args.port, drafts_folder=args.drafts,
                )
            except Exception as exc:  # noqa: BLE001 -- surface a clear CLI error (e.g. missing key)
                parser.error(str(exc))
            email_trigger.disable_trigger_on_identity_change(
                db, org.id, args.host, args.user, prior_identity
            )
            print(f"Connected mailbox '{args.user}' for organization '{args.org}'.")
            return 0
        if args.command == "clear-email":
            org = get_org_by_name(db, args.org)
            if org is None:
                parser.error(f"Unknown organization '{args.org}'.")
            removed = clear_email_credentials(db, org.id)
            email_trigger.disable_trigger(db, org.id)
            print(
                f"Disconnected mailbox for '{args.org}'." if removed
                else f"No mailbox was connected for '{args.org}'."
            )
            return 0
        try:
            set_admin_status(db, args.username, args.command == "promote")
        except ValueError as exc:
            parser.error(str(exc))
        verb = "now an admin" if args.command == "promote" else "no longer an admin"
        print(f"{args.username} is {verb}.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
