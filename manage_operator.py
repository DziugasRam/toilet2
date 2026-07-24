"""Explicit provisioning CLI for toilet-local operator accounts."""

from __future__ import annotations

import argparse
import getpass
import os

from .database import Database
from .service import Actor, MutationService, ServiceError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create or update a toilet-local administrator/proctor."
    )
    parser.add_argument("username")
    parser.add_argument("--display-name")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("TOILET_DATABASE_URL", ""),
        help="defaults to TOILET_DATABASE_URL",
    )
    parser.add_argument("--admin", action="store_true")
    parser.add_argument("--proctor", action="store_true")
    parser.add_argument("--all-classes", action="store_true")
    parser.add_argument("--class-scope", action="append", default=[])
    parser.add_argument("--disabled", action="store_true")
    parser.add_argument(
        "--keep-password",
        action="store_true",
        help="retain the password of an existing account",
    )
    parser.add_argument(
        "--password-env",
        metavar="NAME",
        help="read the password from this explicitly named environment variable",
    )
    return parser


def _password(args: argparse.Namespace) -> str | None:
    if args.keep_password:
        if args.password_env:
            raise SystemExit("--keep-password and --password-env are mutually exclusive")
        return None
    if args.password_env:
        value = os.environ.get(args.password_env)
        if value is None:
            raise SystemExit(f"environment variable {args.password_env!r} is not set")
        return value
    first = getpass.getpass("Operator password: ")
    second = getpass.getpass("Repeat operator password: ")
    if first != second:
        raise SystemExit("passwords do not match")
    return first


def main() -> int:
    args = _parser().parse_args()
    if not args.database_url:
        raise SystemExit("--database-url or TOILET_DATABASE_URL is required")
    roles = [
        role
        for role, selected in (("admin", args.admin), ("proctor", args.proctor))
        if selected
    ]
    if not roles:
        raise SystemExit("select --admin and/or --proctor")
    database = Database(args.database_url)
    try:
        database.initialize()
        service = MutationService(database)
        result = service.upsert_operator(
            username=args.username,
            display_name=args.display_name or args.username,
            password=_password(args),
            roles=roles,
            class_scope=args.class_scope,
            all_classes=args.all_classes,
            enabled=not args.disabled,
            actor=Actor.system("operator-provisioning-cli"),
        )
    except ServiceError as exc:
        raise SystemExit(str(exc)) from exc
    finally:
        database.dispose()
    action = "created" if result.value["created"] else "updated"
    print(f"{action} operator {result.value['username']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
