"""Create (or reset) the global owner account — the Phase 2 deployment script.

Works against SQLite (local) or DATABASE_URL (Supabase Postgres in
production). Runs init_db() first so the auth tables exist, then prompts for
name/email/password (password is read with getpass, never echoed, never
printed back — only a PBKDF2 hash is stored).

Run:  .venv/bin/python scripts/create_owner.py
"""
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app.auth import hash_password, user_by_email
from app.database import get_conn, get_engine, init_db, is_postgres


def main() -> None:
    init_db()
    backend = "PostgreSQL (DATABASE_URL)" if is_postgres() else "SQLite (data/party.db)"
    print(f"Backend: {backend}")

    email = input("Owner email: ").strip().lower()
    if "@" not in email:
        sys.exit("Invalid email")
    name = input("Owner name: ").strip() or "Owner"

    password = getpass.getpass("Password (min 8 characters): ")
    if len(password) < 8:
        sys.exit("Password must be at least 8 characters")
    confirm = getpass.getpass("Repeat password: ")
    if password != confirm:
        sys.exit("Passwords do not match")

    with get_conn() as conn:
        existing = user_by_email(conn, email)
        if existing:
            print(f"User {email} already exists (id={existing['id']}).")
            answer = input("Overwrite password and promote to owner? [y/N]: ").strip().lower()
            if answer != "y":
                sys.exit("Aborted — nothing changed.")
            conn.execute(
                text(
                    "UPDATE users SET password_hash=:h, is_owner=:t, name=:n "
                    "WHERE id=:i"
                ),
                {"h": hash_password(password), "t": 1, "n": name, "i": existing["id"]},
            )
            uid = existing["id"]
            action = "updated"
        else:
            uid = conn.execute(
                text(
                    """INSERT INTO users (name, email, password_hash, is_owner)
                       VALUES (:n, :e, :h, :t) RETURNING id"""
                ),
                {"n": name, "e": email, "h": hash_password(password), "t": 1},
            ).scalar()
            action = "created"

    print(f"Owner {action}: id={uid} email={email} backend={backend}")
    print("Login at the app root with this email/password.")


if __name__ == "__main__":
    main()
