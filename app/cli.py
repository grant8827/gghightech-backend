"""
One-off admin commands, run via `python -m app.cli <command>`.

    python -m app.cli create-superadmin \\
        --email you@example.com --password '...' --full-name "Your Name"

Creates (or updates the password/role of, if the email already exists) a
user with role SUPER_ADMIN, under an internal "GG HighTech" organization
created on first use. This needs shell access to the environment's DB
(e.g. `railway ssh`) — see app/api/routes/auth.py's BOOTSTRAP_TOKEN-gated
endpoint for environments where that isn't available.
"""

import argparse
import sys

from app.db.session import SessionLocal
from app.services.local_auth import create_or_update_superadmin


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create-superadmin", help="Create or reset the SUPER_ADMIN account")
    create_parser.add_argument("--email", required=True)
    create_parser.add_argument("--password", required=True)
    create_parser.add_argument("--full-name", required=True)

    args = parser.parse_args()

    if args.command == "create-superadmin":
        if len(args.password) < 12:
            print("Refusing: password must be at least 12 characters.", file=sys.stderr)
            sys.exit(1)
        db = SessionLocal()
        try:
            user = create_or_update_superadmin(db, args.email, args.password, args.full_name)
            print(f"SUPER_ADMIN ready: {user.email} (id={user.id})")
        finally:
            db.close()


if __name__ == "__main__":
    main()
