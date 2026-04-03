#!/usr/bin/env python3
"""CLI admin tool for audioslop."""

import argparse
import sys
from pathlib import Path
from werkzeug.security import generate_password_hash
from db import (
    count_users,
    create_invite,
    create_user,
    get_user_by_name,
    init_db,
    list_invites,
    list_users,
)

DB_PATH = str(Path(__file__).parent / "audioslop.db")


def cmd_create_admin(args):
    """Create an admin user."""
    init_db(DB_PATH)

    if get_user_by_name(DB_PATH, args.name):
        print(f"Error: User '{args.name}' already exists.", file=sys.stderr)
        sys.exit(1)

    password_hash = generate_password_hash(args.password)
    user_id = create_user(DB_PATH, args.name, password_hash, is_admin=1)
    print(f"Created admin user: {args.name} (id: {user_id})")


def cmd_create_invite(args):
    """Generate an invite link."""
    init_db(DB_PATH)

    base_url = args.base_url.rstrip("/")

    # Find the first admin user
    users = list_users(DB_PATH)
    admin = next((u for u in users if u["is_admin"]), None)
    if not admin:
        print("Error: No admin user found. Create one first with 'create-admin'.", file=sys.stderr)
        sys.exit(1)

    invite = create_invite(DB_PATH, admin["id"])
    invite_url = f"{base_url}/signup?invite={invite['token']}"
    print(invite_url)


def cmd_list_users(args):
    """Print all users with name, role, and join date."""
    init_db(DB_PATH)

    users = list_users(DB_PATH)
    if not users:
        print("No users found.")
        return

    print(f"{'Name':<20} {'Role':<10} {'Join Date':<19}")
    print("-" * 49)
    for user in users:
        role = "admin" if user["is_admin"] else "user"
        created_at = user["created_at"] or "N/A"
        print(f"{user['name']:<20} {role:<10} {created_at:<19}")


def cmd_list_invites(args):
    """Print all invites with truncated token and status."""
    init_db(DB_PATH)

    invites = list_invites(DB_PATH)
    if not invites:
        print("No invites found.")
        return

    print(f"{'Token (first 8)':<15} {'Status':<10} {'Created By':<12} {'Used By':<12}")
    print("-" * 49)
    for invite in invites:
        token_prefix = invite["token"][:8]
        status = "used" if invite["used_by"] else "open"
        created_by = invite["created_by"][:12]
        used_by = invite["used_by"] or ""
        print(f"{token_prefix:<15} {status:<10} {created_by:<12} {used_by:<12}")


def main():
    """Parse arguments and dispatch to command functions."""
    parser = argparse.ArgumentParser(
        prog="manage.py",
        description="CLI admin tool for audioslop.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # create-admin
    create_admin_parser = subparsers.add_parser(
        "create-admin",
        help="Create an admin user.",
    )
    create_admin_parser.add_argument("name", help="Admin username")
    create_admin_parser.add_argument("password", help="Admin password")
    create_admin_parser.set_defaults(func=cmd_create_admin)

    # create-invite
    create_invite_parser = subparsers.add_parser(
        "create-invite",
        help="Generate an invite link.",
    )
    create_invite_parser.add_argument(
        "--base-url",
        default="https://audioslop.amditis.tech",
        help="Base URL for invite link (default: https://audioslop.amditis.tech)",
    )
    create_invite_parser.set_defaults(func=cmd_create_invite)

    # list-users
    list_users_parser = subparsers.add_parser(
        "list-users",
        help="List all users.",
    )
    list_users_parser.set_defaults(func=cmd_list_users)

    # list-invites
    list_invites_parser = subparsers.add_parser(
        "list-invites",
        help="List all invites.",
    )
    list_invites_parser.set_defaults(func=cmd_list_invites)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
