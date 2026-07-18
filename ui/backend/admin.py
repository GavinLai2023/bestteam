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

from .db.email_credentials import clear_email_credentials, set_email_credentials
from .db.models import User
from .db.orgs import (
    DEFAULT_ORG_NAME,
    create_org,
    ensure_email_single_org,
    get_org_by_name,
    list_orgs,
)
from .db.users import (
    create_user,
    delete_user,
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
        if args.command == "delete-user":
            try:
                delete_user(db, args.username)
            except ValueError as exc:
                parser.error(str(exc))
            print(f"Deleted user '{args.username}'.")
            return 0
        if args.command == "move-user":
            if args.platform:
                org_id = None
                where = "platform operator"
            else:
                org = get_org_by_name(db, args.to_org)
                if org is None:
                    parser.error(f"Unknown organization '{args.to_org}'. Create it first with create-org.")
                org_id = org.id
                where = f"member of '{args.to_org}'"
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
            try:
                set_email_credentials(
                    db, org.id, host=args.host, username=args.user, password=password,
                    port=args.port, drafts_folder=args.drafts,
                )
            except Exception as exc:  # noqa: BLE001 -- surface a clear CLI error (e.g. missing key)
                parser.error(str(exc))
            print(f"Connected mailbox '{args.user}' for organization '{args.org}'.")
            return 0
        if args.command == "clear-email":
            org = get_org_by_name(db, args.org)
            if org is None:
                parser.error(f"Unknown organization '{args.org}'.")
            removed = clear_email_credentials(db, org.id)
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
