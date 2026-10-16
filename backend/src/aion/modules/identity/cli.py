import argparse
from datetime import UTC, datetime, timedelta
from getpass import getpass
from uuid import UUID, uuid4

from pydantic import EmailStr, TypeAdapter
from sqlalchemy import create_engine, text

from aion.modules.identity.security import hash_password, new_token, token_hash
from aion.settings import Settings, get_settings


def normalized_email(value: str) -> str:
    return str(TypeAdapter(EmailStr).validate_python(value)).lower()


def bootstrap_admin(settings: Settings, email: str, password: str, name: str) -> UUID:
    if not 12 <= len(password) <= 256 or not name.strip() or len(name) > 80:
        raise ValueError("Use a 12–256 character password and a non-empty name (up to 80).")
    email = normalized_email(email)
    encoded = hash_password(password)
    engine = create_engine(settings.migration_database_url)
    user_id = uuid4()
    try:
        with engine.begin() as connection:
            connection.execute(text("SELECT pg_advisory_xact_lock(6490802)"))
            exists = connection.scalar(
                text("SELECT EXISTS (SELECT 1 FROM app.users WHERE role='admin')")
            )
            if exists:
                raise ValueError("Administrator already exists. Use login or reset-link.")
            connection.execute(
                text("""
                INSERT INTO app.users (id,email,password_hash,role)
                VALUES (:id,:email,:password,'admin')
            """),
                {"id": user_id, "email": email, "password": encoded},
            )
            connection.execute(
                text("""
                INSERT INTO app.user_profiles (user_id,display_name) VALUES (:id,:name)
            """),
                {"id": user_id, "name": name.strip()},
            )
            connection.execute(
                text("INSERT INTO app.user_limits (user_id) VALUES (:id)"), {"id": user_id}
            )
    finally:
        engine.dispose()
    return user_id


def recovery_link(settings: Settings, email: str) -> str:
    token = new_token()
    engine = create_engine(settings.migration_database_url)
    try:
        with engine.begin() as connection:
            owner = connection.scalar(
                text("""
                SELECT id FROM app.users WHERE email=:email AND status='active' FOR UPDATE
            """),
                {"email": normalized_email(email)},
            )
            if owner is None:
                raise ValueError("Active account not found.")
            connection.execute(
                text("""
                UPDATE app.password_resets SET used_at=now()
                WHERE user_id=:owner AND used_at IS NULL
            """),
                {"owner": owner},
            )
            connection.execute(
                text("""
                INSERT INTO app.password_resets (id,user_id,token_hash,expires_at)
                VALUES (:id,:owner,:hash,:expires)
            """),
                {
                    "id": uuid4(),
                    "owner": owner,
                    "hash": token_hash(token),
                    "expires": datetime.now(UTC) + timedelta(minutes=30),
                },
            )
    finally:
        engine.dispose()
    return f"{settings.public_origin.rstrip('/')}/reset#{token}"


def console_invite(settings: Settings, email: str, hours: int) -> str:
    if not 1 <= hours <= 168:
        raise ValueError("Invite lifetime must be 1–168 hours.")
    token = new_token()
    email = normalized_email(email)
    engine = create_engine(settings.migration_database_url)
    try:
        with engine.begin() as connection:
            admin = connection.scalar(
                text("""
                SELECT id FROM app.users WHERE role='admin' AND status='active'
                ORDER BY created_at LIMIT 1
            """)
            )
            if admin is None:
                raise ValueError("Bootstrap an administrator first.")
            if connection.scalar(
                text("SELECT id FROM app.users WHERE email=:email"), {"email": email}
            ):
                raise ValueError("Account already exists.")
            connection.execute(
                text("""
                INSERT INTO app.invites (id,email,token_hash,created_by,expires_at)
                VALUES (:id,:email,:hash,:admin,:expires)
            """),
                {
                    "id": uuid4(),
                    "email": email,
                    "hash": token_hash(token),
                    "admin": admin,
                    "expires": datetime.now(UTC) + timedelta(hours=hours),
                },
            )
    finally:
        engine.dispose()
    return f"{settings.public_origin.rstrip('/')}/join#{token}"


def main() -> None:
    parser = argparse.ArgumentParser(description="AION console administration (migration role).")
    commands = parser.add_subparsers(dest="command", required=True)
    bootstrap = commands.add_parser("bootstrap", help="Create the first administrator")
    bootstrap.add_argument("--email", required=True)
    bootstrap.add_argument("--name", default="Администратор")
    invite = commands.add_parser("invite", help="Issue a single-use invitation")
    invite.add_argument("--email", required=True)
    invite.add_argument("--hours", type=int, default=48)
    revoke = commands.add_parser("revoke-invite", help="Revoke an unused invitation")
    revoke.add_argument("--id", required=True, type=UUID)
    recovery = commands.add_parser("reset-link", help="Issue a 30-minute password recovery link")
    recovery.add_argument("--email", required=True)
    args = parser.parse_args()
    settings = get_settings()
    try:
        if args.command == "bootstrap":
            password = getpass("Password (12+ characters): ")
            if password != getpass("Repeat password: "):
                raise ValueError("Passwords do not match.")
            user_id = bootstrap_admin(settings, args.email, password, args.name)
            print(f"Administrator created: {user_id}")
        elif args.command == "invite":
            print(console_invite(settings, args.email, args.hours))
        elif args.command == "reset-link":
            print(recovery_link(settings, args.email))
        elif args.command == "revoke-invite":
            engine = create_engine(settings.migration_database_url)
            try:
                with engine.begin() as connection:
                    found = connection.scalar(
                        text("""
                        UPDATE app.invites SET revoked_at=now()
                        WHERE id=:id AND used_at IS NULL AND revoked_at IS NULL RETURNING id
                    """),
                        {"id": args.id},
                    )
                    if found is None:
                        raise ValueError("Active invitation not found.")
            finally:
                engine.dispose()
            print("Invitation revoked.")
    except ValueError as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
