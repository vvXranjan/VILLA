"""Database layer — dual-backend via SQLAlchemy.

- DATABASE_URL unset      → local SQLite at data/party.db (existing venue workflow)
- DATABASE_URL set (pg)   → PostgreSQL (Supabase in production, local PG for testing)

Timestamps are stored as TEXT in "YYYY-MM-DD HH:MM:SS" UTC format on both
backends (matches SQLite's datetime('now') output) so the UI, export CSV and
check-in messages are byte-identical regardless of backend.
"""
import json
import os
import shutil
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "party.db"
BACKUP_DIR = ROOT / "data" / "backups"

VILLA_SLUG = "villa-diwali-bling"
VILLA_TITLE = "VILLA — Desi Diwali Bling"

_lock = threading.RLock()  # reentrant: init_db holds it while calling get_engine()
_engine: Engine | None = None
_initialized = False


def utc_now_str() -> str:
    """Same format as SQLite datetime('now'): UTC, second precision."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _make_engine() -> Engine:
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        # Postgres (Supabase pooler-safe): no pooled/held connections.
        return create_engine(url, poolclass=NullPool, pool_pre_ping=True)
    DB_PATH.parent.mkdir(exist_ok=True)
    BACKUP_DIR.mkdir(exist_ok=True)
    eng = create_engine(
        f"sqlite:///{DB_PATH}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )

    @event.listens_for(eng, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return eng


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        with _lock:
            if _engine is None:
                _engine = _make_engine()
    return _engine


def is_postgres() -> bool:
    return get_engine().dialect.name == "postgresql"


@contextmanager
def get_conn():
    """Connection context: commit on success, rollback on error, always close."""
    if not _initialized:
        init_db()  # lazy safety net (Vercel may not fire lifespan events)
    conn = get_engine().connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


_POSTGRES_DDL = """
CREATE TABLE IF NOT EXISTS registrations (
    id SERIAL PRIMARY KEY,
    reg_id TEXT UNIQUE NOT NULL,
    entry_pass TEXT DEFAULT '',
    phone TEXT DEFAULT '',
    email TEXT DEFAULT '',
    payment_status TEXT NOT NULL DEFAULT 'UNDER REVIEW',
    needs_review INTEGER DEFAULT 0,
    review_reason TEXT DEFAULT '',
    expected_capacity INTEGER DEFAULT 0,
    created_at TEXT DEFAULT to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS')
);
CREATE TABLE IF NOT EXISTS guests (
    id SERIAL PRIMARY KEY,
    reg_id TEXT NOT NULL,
    name TEXT NOT NULL,
    phone TEXT DEFAULT '',
    email TEXT DEFAULT '',
    payment_status TEXT NOT NULL DEFAULT 'UNDER REVIEW',
    entry_pass TEXT DEFAULT '',
    needs_review INTEGER DEFAULT 0,
    review_reason TEXT DEFAULT '',
    added_by TEXT DEFAULT 'csv',
    qr_token TEXT UNIQUE,
    checked_in_at TEXT,
    created_at TEXT DEFAULT to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS')
);
CREATE INDEX IF NOT EXISTS idx_guests_name ON guests(name);
CREATE INDEX IF NOT EXISTS idx_guests_phone ON guests(phone);
CREATE INDEX IF NOT EXISTS idx_guests_reg ON guests(reg_id);
CREATE INDEX IF NOT EXISTS idx_guests_qr ON guests(qr_token);
CREATE UNIQUE INDEX IF NOT EXISTS idx_guests_reg_name ON guests(reg_id, name);
CREATE TABLE IF NOT EXISTS staged_imports (
    token TEXT PRIMARY KEY,
    file_hash TEXT NOT NULL,
    file_name TEXT DEFAULT '',
    payload TEXT NOT NULL,
    created_at TEXT DEFAULT to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS')
);
CREATE TABLE IF NOT EXISTS import_log (
    id SERIAL PRIMARY KEY,
    file_hash TEXT NOT NULL,
    file_name TEXT DEFAULT '',
    registrations INTEGER,
    guests INTEGER,
    imported_at TEXT DEFAULT to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS')
);
CREATE TABLE IF NOT EXISTS backup_snapshots (
    id SERIAL PRIMARY KEY,
    label TEXT NOT NULL,
    guests_json TEXT NOT NULL,
    registrations_json TEXT NOT NULL,
    created_at TEXT DEFAULT to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS')
);
CREATE TABLE IF NOT EXISTS events (
    id SERIAL PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    poster_url TEXT DEFAULT '',
    venue TEXT DEFAULT '',
    city TEXT DEFAULT '',
    starts_at TEXT DEFAULT '',
    ends_at TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT DEFAULT to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS')
);
CREATE TABLE IF NOT EXISTS ticket_types (
    id SERIAL PRIMARY KEY,
    event_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    price_inr INTEGER NOT NULL DEFAULT 0,
    group_size INTEGER NOT NULL DEFAULT 1,
    capacity INTEGER NOT NULL DEFAULT 0,
    sold INTEGER NOT NULL DEFAULT 0,
    sale_open INTEGER NOT NULL DEFAULT 1,
    created_at TEXT DEFAULT to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS'),
    UNIQUE (event_id, name)
);
CREATE INDEX IF NOT EXISTS idx_events_status ON events(status);
CREATE INDEX IF NOT EXISTS idx_ticket_types_event ON ticket_types(event_id);
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    is_owner INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS')
);
CREATE TABLE IF NOT EXISTS event_members (
    event_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    PRIMARY KEY (event_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_event_members_user ON event_members(user_id);
CREATE TABLE IF NOT EXISTS invites (
    code TEXT PRIMARY KEY,
    event_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    created_by INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    used_by INTEGER
);
CREATE INDEX IF NOT EXISTS idx_invites_event ON invites(event_id);
CREATE TABLE IF NOT EXISTS login_attempts (
    key TEXT PRIMARY KEY,
    fail_count INTEGER NOT NULL DEFAULT 0,
    window_start TEXT NOT NULL,
    last_attempt TEXT NOT NULL
);
"""

_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS registrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reg_id TEXT UNIQUE NOT NULL,
    entry_pass TEXT DEFAULT '',
    phone TEXT DEFAULT '',
    email TEXT DEFAULT '',
    payment_status TEXT NOT NULL DEFAULT 'UNDER REVIEW',
    needs_review INTEGER DEFAULT 0,
    review_reason TEXT DEFAULT '',
    expected_capacity INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS guests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reg_id TEXT NOT NULL,
    name TEXT NOT NULL,
    phone TEXT DEFAULT '',
    email TEXT DEFAULT '',
    payment_status TEXT NOT NULL DEFAULT 'UNDER REVIEW',
    entry_pass TEXT DEFAULT '',
    needs_review INTEGER DEFAULT 0,
    review_reason TEXT DEFAULT '',
    added_by TEXT DEFAULT 'csv',
    qr_token TEXT UNIQUE,
    checked_in_at TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_guests_name ON guests(name);
CREATE INDEX IF NOT EXISTS idx_guests_phone ON guests(phone);
CREATE INDEX IF NOT EXISTS idx_guests_reg ON guests(reg_id);
CREATE INDEX IF NOT EXISTS idx_guests_qr ON guests(qr_token);
CREATE UNIQUE INDEX IF NOT EXISTS idx_guests_reg_name ON guests(reg_id, name);
CREATE TABLE IF NOT EXISTS staged_imports (
    token TEXT PRIMARY KEY,
    file_hash TEXT NOT NULL,
    file_name TEXT DEFAULT '',
    payload TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS import_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_hash TEXT NOT NULL,
    file_name TEXT DEFAULT '',
    registrations INTEGER,
    guests INTEGER,
    imported_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    poster_url TEXT DEFAULT '',
    venue TEXT DEFAULT '',
    city TEXT DEFAULT '',
    starts_at TEXT DEFAULT '',
    ends_at TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS ticket_types (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    price_inr INTEGER NOT NULL DEFAULT 0,
    group_size INTEGER NOT NULL DEFAULT 1,
    capacity INTEGER NOT NULL DEFAULT 0,
    sold INTEGER NOT NULL DEFAULT 0,
    sale_open INTEGER NOT NULL DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE (event_id, name)
);
CREATE INDEX IF NOT EXISTS idx_events_status ON events(status);
CREATE INDEX IF NOT EXISTS idx_ticket_types_event ON ticket_types(event_id);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    is_owner INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS event_members (
    event_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    PRIMARY KEY (event_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_event_members_user ON event_members(user_id);
CREATE TABLE IF NOT EXISTS invites (
    code TEXT PRIMARY KEY,
    event_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    created_by INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    used_by INTEGER
);
CREATE INDEX IF NOT EXISTS idx_invites_event ON invites(event_id);
CREATE TABLE IF NOT EXISTS login_attempts (
    key TEXT PRIMARY KEY,
    fail_count INTEGER NOT NULL DEFAULT 0,
    window_start TEXT NOT NULL,
    last_attempt TEXT NOT NULL
);
"""


def _column_exists(conn, table: str, column: str) -> bool:
    if conn.dialect.name == "postgresql":
        row = conn.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema=current_schema() AND table_name=:t AND column_name=:c"
            ),
            {"t": table, "c": column},
        ).fetchone()
        return row is not None
    rows = conn.execute(text(f"PRAGMA table_info({table})")).mappings().all()
    return any(r["name"] == column for r in rows)


def _ensure_column(conn, table: str, column: str, decl: str) -> None:
    """ALTER TABLE ADD COLUMN — no-op when the column already exists."""
    if not _column_exists(conn, table, column):
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {decl}"))


def get_villa_event_id(conn) -> int:
    """Seed the VILLA event if missing (idempotent) and return its id."""
    conn.execute(
        text(
            "INSERT INTO events (slug, title, status) VALUES (:slug, :title, 'draft') "
            "ON CONFLICT(slug) DO NOTHING"
        ),
        {"slug": VILLA_SLUG, "title": VILLA_TITLE},
    )
    return conn.execute(
        text("SELECT id FROM events WHERE slug=:s"), {"s": VILLA_SLUG}
    ).scalar()


def backfill_event_ids(conn) -> None:
    """Point every row with a NULL event_id at the VILLA event (safe twice)."""
    vid = get_villa_event_id(conn)
    for table in ("registrations", "guests"):
        conn.execute(
            text(f"UPDATE {table} SET event_id=:e WHERE event_id IS NULL"),
            {"e": vid},
        )


def init_db() -> None:
    """Idempotent schema creation. Safe to call on every cold start."""
    global _initialized
    with _lock:
        engine = get_engine()
        ddl = _POSTGRES_DDL if engine.dialect.name == "postgresql" else _SQLITE_DDL
        with engine.begin() as conn:
            for stmt in ddl.split(";"):
                s = stmt.strip()
                if s:
                    conn.execute(text(s))
            _ensure_column(conn, "registrations", "event_id", "INTEGER")
            _ensure_column(conn, "guests", "event_id", "INTEGER")
            backfill_event_ids(conn)
        _initialized = True


def do_backup() -> str:
    """Backup before destructive writes. Returns a label for the UI message.

    Postgres (serverless): logical snapshot row in backup_snapshots.
    SQLite (local): timestamped file copy in data/backups/ (previous behavior).
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    if is_postgres():
        with get_conn() as conn:
            guests = [dict(r) for r in conn.execute(text("SELECT * FROM guests")).mappings()]
            regs = [dict(r) for r in conn.execute(text("SELECT * FROM registrations")).mappings()]
            label = f"snapshot-{ts}-{uuid.uuid4().hex[:6]}"
            conn.execute(
                text(
                    "INSERT INTO backup_snapshots (label, guests_json, registrations_json) "
                    "VALUES (:l, :g, :r)"
                ),
                {"l": label, "g": json.dumps(guests), "r": json.dumps(regs)},
            )
        return label
    dest = BACKUP_DIR / f"party-backup-{ts}.db"
    with get_conn() as conn:
        conn.execute(text("PRAGMA wal_checkpoint(FULL)"))
    shutil.copy2(DB_PATH, dest)
    return dest.name
