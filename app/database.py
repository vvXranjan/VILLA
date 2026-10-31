import sqlite3
import threading
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "party.db"
BACKUP_DIR = Path(__file__).resolve().parent.parent / "data" / "backups"
DB_PATH.parent.mkdir(exist_ok=True)
BACKUP_DIR.mkdir(exist_ok=True)

_lock = threading.Lock()

GUEST_COLUMNS = {
    "entry_pass": "TEXT DEFAULT ''",
    "needs_review": "INTEGER DEFAULT 0",
    "review_reason": "TEXT DEFAULT ''",
}
REGISTRATION_COLUMNS = {
    "needs_review": "INTEGER DEFAULT 0",
    "review_reason": "TEXT DEFAULT ''",
}


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _ensure_columns(conn: sqlite3.Connection, table: str, cols: dict) -> None:
    existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    for name, decl in cols.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _migrate_group_schema(conn: sqlite3.Connection) -> None:
    """Old schema had `reg_id TEXT UNIQUE` on guests (one guest per registration).
    Group support needs many guests sharing a reg_id — rebuild the table."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='guests'"
    ).fetchone()
    if not row or not row[0] or "reg_id TEXT UNIQUE" not in row[0]:
        return
    conn.executescript(
        """
        BEGIN;
        CREATE TABLE guests_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reg_id TEXT NOT NULL,
            name TEXT NOT NULL,
            phone TEXT DEFAULT '',
            email TEXT DEFAULT '',
            payment_status TEXT NOT NULL DEFAULT 'UNDER REVIEW',
            entry_pass TEXT DEFAULT '',
            needs_review INTEGER DEFAULT 0,
            review_reason TEXT DEFAULT '',
            qr_token TEXT UNIQUE,
            checked_in_at TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        INSERT INTO guests_new
            (id, reg_id, name, phone, email, payment_status, qr_token, checked_in_at, created_at)
            SELECT id, reg_id, name, phone, email, payment_status, qr_token, checked_in_at, created_at
            FROM guests;
        DROP TABLE guests;
        ALTER TABLE guests_new RENAME TO guests;
        CREATE INDEX idx_guests_name ON guests(name);
        CREATE INDEX idx_guests_phone ON guests(phone);
        CREATE INDEX idx_guests_reg ON guests(reg_id);
        CREATE INDEX idx_guests_qr ON guests(qr_token);
        CREATE UNIQUE INDEX idx_guests_reg_name ON guests(reg_id, name);
        COMMIT;
        """
    )


def init_db() -> None:
    with _lock:
        conn = get_conn()
        try:
            _migrate_group_schema(conn)
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS registrations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reg_id TEXT UNIQUE NOT NULL,
                    entry_pass TEXT DEFAULT '',
                    phone TEXT DEFAULT '',
                    email TEXT DEFAULT '',
                    payment_status TEXT NOT NULL DEFAULT 'UNDER REVIEW',
                    needs_review INTEGER DEFAULT 0,
                    review_reason TEXT DEFAULT '',
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
                """
            )
            _ensure_columns(conn, "guests", GUEST_COLUMNS)
            _ensure_columns(conn, "registrations", REGISTRATION_COLUMNS)
            conn.commit()
        finally:
            conn.close()
