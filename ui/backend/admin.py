"""Operator CLI for granting/revoking admin rights.

Admin is the only role that can reach the Advanced config page and the per-user
memory-management API, so it's provisioned deliberately here rather than from an
env list or public registration (which would let an attacker pre-claim a
configured username). Run inside the deployment, e.g.:

    docker compose exec backend python -m ui.backend.admin promote <username>
    docker compose exec backend python -m ui.backend.admin demote <username>
    docker compose exec backend python -m ui.backend.admin list

The user must already exist (create it via `POST /api/auth/register` first).
"""

from __future__ import annotations

import argparse
from typing import Optional, Sequence

from .db.models import User
from .db.users import set_admin_status
from .db_session import SessionLocal


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ui.backend.admin", description="Manage admin users."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    promote = sub.add_parser("promote", help="grant admin to an existing user")
    promote.add_argument("username")
    demote = sub.add_parser("demote", help="revoke admin from a user")
    demote.add_argument("username")
    sub.add_parser("list", help="list current admins")

    args = parser.parse_args(argv)

    with SessionLocal() as db:
        if args.command == "list":
            for user in db.query(User).filter_by(is_admin=True).order_by(User.username).all():
                print(user.username)
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
