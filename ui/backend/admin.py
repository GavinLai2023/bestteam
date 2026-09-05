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
    docker compose exec backend python -m ui.backend.admin set-email acme --auth microsoft-oauth --user support@acme.com --tenant <directory-id> --client-id <application-id>
    docker compose exec backend python -m ui.backend.admin clear-email acme
    docker compose run --rm --no-deps backend python -m ui.backend.admin check-env

`create-user --platform` creates a platform operator (no org); org members
are created with `--org <name>` (default: the `default` org). Admin rights
are granted only via `promote` -- never from an env list or username match
(which would let an attacker pre-claim a configured username).
"""

from __future__ import annotations

import argparse
import getpass
import os
from typing import Optional, Sequence

from . import email_trigger
# Shared with the admin API (admin_api.py); kept under these module-level names
# so the CLI's call sites and tests that monkeypatch them are unchanged.
from .account_memory import open_memory_store as _open_memory_store
from .account_memory import purge_user_memory as _purge_user_memory
from .account_memory import reconcile_legacy_org as _reconcile_legacy_org
from .db.email_credentials import (
    AUTH_MICROSOFT_OAUTH,
    AUTH_PASSWORD,
    MICROSOFT_IMAP_HOST,
    clear_email_credentials,
    get_email_credentials,
    set_email_credentials,
)
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
from .env_check import (
    check_environment,
    check_model_catalog,
    check_org_retention,
    check_schema,
    default_db_path,
    has_failures,
)


def _open_session():
    # Late-bound on purpose: importing `db_session` creates the database
    # file, its tables and the default rows, and `check-env` is advertised
    # as running before any of that exists (and must not be the thing that
    # creates it). Tests replace this name with an in-memory factory.
    from .db_session import SessionLocal

    return SessionLocal()


def _prompt_password(parser: argparse.ArgumentParser, label: str = "Password") -> str:
    password = getpass.getpass(f"{label}: ")
    if not password:
        parser.error(f"{label} must not be empty")
    if getpass.getpass(f"Repeat {label.lower()}: ") != password:
        parser.error(f"{label}s do not match")
    return password


def _print_findings(findings) -> int:
    """The shared check-* output: one line per finding, exit 1 on any FAIL."""
    for finding in findings:
        print(f"[{finding.level}]{' ' * (5 - len(finding.level))}{finding.name}: {finding.message}")
    failures = sum(1 for f in findings if f.level == "FAIL")
    warnings = sum(1 for f in findings if f.level == "WARN")
    print(f"{failures} failure(s), {warnings} warning(s)" if failures else f"no failures, {warnings} warning(s)")
    return 1 if has_failures(findings) else 0


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
        "set-email",
        help="connect an org's mailbox (prompts for the password or client secret)",
    )
    set_email_p.add_argument("org", help="organization name")
    set_email_p.add_argument(
        "--auth", choices=["password", "microsoft-oauth"], default="password",
        help="password: an app password. microsoft-oauth: Entra app-only credentials "
             "for Exchange Online, which no longer accepts basic auth.",
    )
    set_email_p.add_argument("--host", default=None,
                             help="IMAP host, e.g. imap.gmail.com (fixed for microsoft-oauth)")
    set_email_p.add_argument("--user", required=True, help="IMAP username / email address")
    set_email_p.add_argument("--tenant", default=None,
                             help="Entra Directory (tenant) ID (microsoft-oauth only)")
    set_email_p.add_argument("--client-id", dest="client_id", default=None,
                             help="Entra Application (client) ID (microsoft-oauth only)")
    set_email_p.add_argument("--port", type=int, default=993)
    set_email_p.add_argument("--drafts", default=None, help="Drafts folder name (auto-detected if omitted)")
    set_email_p.add_argument(
        "--test", action="store_true", help="verify the credentials with a login before saving"
    )

    clear_email_p = sub.add_parser("clear-email", help="disconnect an org's mailbox")
    clear_email_p.add_argument("org", help="organization name")

    sub.add_parser(
        "check-env",
        help="print the launch checklist for this process's environment "
             "(FAIL/WARN/OK per variable); exit 1 on any FAIL. Reads only.",
    )

    sub.add_parser(
        "check-health",
        help="print the email trigger's health metrics per org (poll lag, "
             "backlog age, 24h failures, draft latency) as FAIL/WARN/OK; "
             "exit 1 on any FAIL. Run it from cron -- a stalled or dead "
             "poller can't report itself through in-app notifications.",
    )

    args = parser.parse_args(argv)

    if args.command == "check-env":
        # Deliberately before the database is opened (`_open_session` is what
        # imports `db_session`): the checklist must run on a box whose
        # database does not exist yet, and leave it that way.
        db_path = default_db_path(os.environ)
        findings = check_environment(os.environ) + [
            check_schema(db_path),
            check_org_retention(db_path),
            check_model_catalog(db_path),
        ]
        return _print_findings(findings)

    if args.command == "check-health":
        # Guard before `_open_session`: on a box with no database yet, opening
        # the session would CREATE it, and a health check must not.
        db_path = default_db_path(os.environ)
        if str(db_path) != ":memory:" and not db_path.exists():
            print(f"[OK]   triggers: no database at {db_path} yet; nothing to monitor")
            return 0
        from .email_trigger import poll_seconds
        from .trigger_metrics import backlog_alert_seconds, collect, evaluate

        with _open_session() as db:
            metrics = collect(db)
        findings = evaluate(
            metrics,
            poll_interval_seconds=poll_seconds(),
            backlog_threshold_seconds=backlog_alert_seconds(),
        )
        return _print_findings(findings)

    with _open_session() as db:
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
            oauth = args.auth == "microsoft-oauth"
            if oauth:
                if not args.tenant or not args.client_id:
                    parser.error("--auth microsoft-oauth needs --tenant and --client-id.")
                # Exchange Online's endpoint; the OAuth scope is bound to it.
                host = args.host or MICROSOFT_IMAP_HOST
            else:
                if not args.host:
                    parser.error("--host is required with --auth password.")
                if args.tenant or args.client_id:
                    parser.error("--tenant/--client-id only apply with --auth microsoft-oauth.")
                host = args.host
            secret = _prompt_password(parser, "Client secret" if oauth else "Password")
            if args.test:
                # Build the same backend the tools use and attempt a login, so a
                # bad credential is caught here rather than at first run.
                from bestteam.exceptions import ConfigurationError

                from .email_tools import build_imap_backend, token_provider_for

                auth_type = AUTH_MICROSOFT_OAUTH if oauth else AUTH_PASSWORD
                backend = build_imap_backend(
                    host=host, username=args.user, port=args.port, drafts=args.drafts,
                    password=None if oauth else secret,
                    token_provider=token_provider_for(
                        auth_type, tenant_id=args.tenant, client_id=args.client_id,
                        client_secret=secret,
                    ),
                )
                try:
                    conn = backend._connect()
                    conn.logout()
                except ConfigurationError as exc:
                    parser.error(f"Login test failed, not saved: {exc}")
                except OSError as exc:
                    parser.error(f"Could not reach '{host}:{args.port}', not saved: {exc}")
            prior = get_email_credentials(db, org.id)
            prior_identity = (prior.host, prior.username) if prior is not None else None
            try:
                set_email_credentials(
                    db, org.id, host=host, username=args.user, password=secret,
                    port=args.port, drafts_folder=args.drafts,
                    auth_type=AUTH_MICROSOFT_OAUTH if oauth else AUTH_PASSWORD,
                    oauth_tenant_id=args.tenant if oauth else None,
                    oauth_client_id=args.client_id if oauth else None,
                )
            except Exception as exc:  # noqa: BLE001 -- surface a clear CLI error (e.g. missing key)
                parser.error(str(exc))
            email_trigger.on_mailbox_saved(
                db, org.id, host, args.user, prior_identity
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
