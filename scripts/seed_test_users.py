"""Seed one user per role (plus a non-member) and an expired invite into a
LOCAL test database (SQLite sandbox, or local Postgres) for the auth tests.

Safety guard: refuses to run when a configured DATABASE_URL targets anything
other than 127.0.0.1/localhost — this script must never touch the live
Supabase database.

Run:  VILLA_TEST_PASSWORD=... .venv/bin/python scripts/seed_test_users.py
"""
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app.auth import create_user, hash_password
from app.database import get_conn, get_villa_event_id, init_db

DEFAULT_TEST_PASSWORD = "TestPass123!"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def refuse(msg: str) -> None:
    print(f"REFUSING TO RUN: {msg}")
    sys.exit(2)


def check_target_is_local() -> None:
    db_url = os.environ.get("DATABASE_URL", "").strip()
    if db_url:
        # Anything other than localhost is the dangerous case (live Supabase).
        netloc = re.sub(r"^[a-z0-9+.-]+://", "", db_url).split("/")[0]
        host = netloc.rsplit("@", 1)[-1].split(":")[0]
        if host not in LOCAL_HOSTS:
            refuse(
                f"target host {host!r} is not 127.0.0.1/localhost — "
                "seed_test_users never touches a remote database."
            )


def main() -> None:
    check_target_is_local()
    password = os.environ.get("VILLA_TEST_PASSWORD", DEFAULT_TEST_PASSWORD)
    init_db()

    users = [
        ("Sandbox Owner", "owner@sandbox.test", True, None),
        ("Sandbox Co-Host", "cohost@sandbox.test", False, "co_host"),
        ("Sandbox Gate", "gate@sandbox.test", False, "gate_staff"),
        ("Sandbox Nobody", "nobody@sandbox.test", False, None),
    ]

    with get_conn() as conn:
        event_id = get_villa_event_id(conn)
        for name, email, is_owner, role in users:
            existing = conn.execute(
                text("SELECT id FROM users WHERE email=:e"), {"e": email}
            ).fetchone()
            if existing:
                uid = existing[0]
                conn.execute(
                    text(
                        "UPDATE users SET name=:n, password_hash=:h, is_owner=:o "
                        "WHERE id=:i"
                    ),
                    {"n": name, "h": hash_password(password), "o": 1 if is_owner else 0, "i": uid},
                )
                action = "updated"
            else:
                uid = create_user(conn, name, email, password, is_owner=is_owner)
                action = "created"
            if role:
                conn.execute(
                    text(
                        "INSERT INTO event_members (event_id, user_id, role) "
                        "VALUES (:e, :u, :r) ON CONFLICT (event_id, user_id) DO NOTHING"
                    ),
                    {"e": event_id, "u": uid, "r": role},
                )
            print(f"  {action} {email} (id={uid}) role={role or ('owner' if is_owner else 'none')}")

        # An already-expired invite for the "expired" test path.
        conn.execute(
            text(
                """INSERT INTO invites (code, event_id, role, created_by, expires_at)
                   VALUES (:c, :e, 'gate_staff', :b, '2000-01-01 00:00:00')
                   ON CONFLICT (code) DO NOTHING"""
            ),
            {"c": "expired-test-invite", "e": event_id, "b": 1},
        )
        print("  ensured expired invite code: expired-test-invite")

    print(
        "Seeded sandbox users (password comes from VILLA_TEST_PASSWORD, "
        f"default {'set' if os.environ.get('VILLA_TEST_PASSWORD') else 'used'}) "
        f"on event_id={event_id}"
    )


if __name__ == "__main__":
    main()
