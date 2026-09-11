"""
One-off admin commands, run via `python -m app.cli <command>`.

    python -m app.cli create-superadmin \\
        --email you@example.com --password '...' --full-name "Your Name"

Creates (or updates the password/role of, if the email already exists) a
user with role SUPER_ADMIN, under an internal "GG HighTech" organization
created on first use. This is how the very first login exists, before
there's an admin session to invite anyone through the API.
"""

import argparse
import sys

from app.db.session import SessionLocal
from app.models.organization import Organization
from app.models.user import User
from app.services.local_auth import hash_password

INTERNAL_ORG_NAME = "GG HighTech (Internal)"
INTERNAL_ORG_DOMAIN = "gghightech.internal"


def create_superadmin(email: str, password: str, full_name: str) -> None:
    db = SessionLocal()
    try:
        org = db.query(Organization).filter(Organization.domain == INTERNAL_ORG_DOMAIN).first()
        if not org:
            org = Organization(name=INTERNAL_ORG_NAME, domain=INTERNAL_ORG_DOMAIN, plan_tier="INTERNAL")
            db.add(org)
            db.flush()

        user = db.query(User).filter(User.email == email).first()
        if user:
            user.password_hash = hash_password(password)
            user.role = "SUPER_ADMIN"
            user.full_name = full_name
            print(f"Updated existing user {email} -> SUPER_ADMIN with a new password.")
        else:
            user = User(
                org_id=org.id,
                email=email,
                password_hash=hash_password(password),
                full_name=full_name,
                role="SUPER_ADMIN",
            )
            db.add(user)
            print(f"Created SUPER_ADMIN user {email}.")
        db.commit()
    finally:
        db.close()


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
        create_superadmin(args.email, args.password, args.full_name)


if __name__ == "__main__":
    main()
