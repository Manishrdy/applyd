"""CLI for identity-service operations.

Usage:
    uv run python -m app.cli init-db
    uv run python -m app.cli set-role <email> <user|admin>
    uv run python -m app.cli list-users
"""

from __future__ import annotations

import argparse
import sys

from app.auth import VALID_ROLES, admin_set_user_role, get_user_role
from app.database import get_db, init_db


def cmd_init_db(_: argparse.Namespace) -> int:
    init_db()
    print("identity-service schema initialised")
    return 0


def cmd_set_role(args: argparse.Namespace) -> int:
    role = args.role.strip().lower()
    if role not in VALID_ROLES:
        print(f"invalid role: {role!r} (valid: {sorted(VALID_ROLES)})", file=sys.stderr)
        return 2
    email = args.email.strip().lower()
    with get_db() as conn:
        row = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if row is None:
        print(f"no such user: {email}", file=sys.stderr)
        return 1
    user_id = int(row["id"])
    previous = get_user_role(user_id)
    if previous == role:
        print(f"{email} already has role={role}; nothing to do")
        return 0
    if not admin_set_user_role(user_id, role):
        print("update failed", file=sys.stderr)
        return 1
    print(f"updated {email}: role {previous!r} -> {role!r}")
    return 0


def cmd_list_users(_: argparse.Namespace) -> int:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, email, role, created_at FROM users ORDER BY id"
        ).fetchall()
    if not rows:
        print("(no users yet)")
        return 0
    for r in rows:
        print(f"  {r['id']:>4}  {r['role']:<6}  {r['email']:<40}  {r['created_at']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="identity_service.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init-db", help="create schema if missing")
    p_init.set_defaults(func=cmd_init_db)

    p_set = sub.add_parser("set-role", help="promote or demote a user")
    p_set.add_argument("email")
    p_set.add_argument("role", choices=sorted(VALID_ROLES))
    p_set.set_defaults(func=cmd_set_role)

    p_list = sub.add_parser("list-users", help="dump users + role + created_at")
    p_list.set_defaults(func=cmd_list_users)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
