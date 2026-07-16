"""Operator CLI for provisioning orgs, users, and admin rights.

There is no public registration and no self-service org creation: customer
organisations, their user accounts, and admin rights are all provisioned
deliberately here by the platform operator. Run inside the deployment, e.g.:

    docker compose exec backend python -m ui.backend.admin create-org acme --display-name "Acme Corp"
    docker compose exec backend python -m ui.backend.admin create-user alice --org acme
    docker compose exec backend python -m ui.backend.admin create-user op --platform
    docker compose exec backend python -m ui.backend.admin promote op
    docker compose exec backend python -m ui.backend.admin demote <username>
    docker compose exec backend python -m ui.backend.admin list
    docker compose exec backend python -m ui.backend.admin list-orgs

`create-user --platform` creates a platform operator (no org); org members
are created with `--org <name>` (default: the `default` org). Admin rights
are granted only via `promote` -- never from an env list or username match
(which would let an attacker pre-claim a configured username).
"""

from __future__ import annotations

import argparse
import getpass
from typing import Optional, Sequence

from .db.models import User
from .db.orgs import (
    DEFAULT_ORG_NAME,
    create_org,
    ensure_email_single_org,
    get_org_by_name,
    list_orgs,
)
from .db.users import create_user, set_admin_status
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
        try:
            set_admin_status(db, args.username, args.command == "promote")
        except ValueError as exc:
            parser.error(str(exc))
        verb = "now an admin" if args.command == "promote" else "no longer an admin"
        print(f"{args.username} is {verb}.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
