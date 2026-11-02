#!/usr/bin/env python3
"""One-time migration: SQLite party.db -> PostgreSQL.

Usage (from repo root):
    DATABASE_URL='postgresql+psycopg://user:pass@host:6543/dbname' \
        python scripts/migrate_sqlite_to_postgres.py

Idempotent: re-running upserts the same rows (keys: reg_id / reg_id+name,
slug, event_id+ticket name) without duplicating. Guest and registration IDs
are preserved; sequences are reset to MAX(id) afterwards. Existing cloud-side
check-ins and admin-added companions are never overwritten (the upsert column
list excludes checked_in_at / added_by).

Event scoping (Phase 1): copies events/ticket_types when the source has
them, remaps registration/guest event_ids through source->target slug map,
and back-fills any NULL event_id to the VILLA seed event.

Aadhaar safety: the SQLite schema must contain no aadhaar-like column —
the script asserts this before copying anything.
"""
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if not os.environ.get("DATABASE_URL", "").strip():
    sys.exit("ERROR: set DATABASE_URL to your PostgreSQL connection string first.")

import re  # noqa: E402

from sqlalchemy import text  # noqa: E402

from app import database as db  # noqa: E402
from app.database import backfill_event_ids, get_villa_event_id  # noqa: E402

AADHAAR_RE = re.compile(r"aadhaar|aadhar|uid", re.I)
SQLITE_DB = ROOT / "data" / "party.db"


def _source_tables(src: sqlite3.Connection) -> set[str]:
    return {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _copy_events(src: sqlite3.Connection, tables: set[str]) -> dict[int, int]:
    """Copy source events into target; returns source_id -> target_id map."""
    if "events" not in tables:
        return {}
    id_map: dict[int, int] = {}
    with db.get_engine().begin() as conn:
        for e in src.execute("SELECT * FROM events ORDER BY id"):
            row = dict(e)
            conn.execute(
                text(
                    """INSERT INTO events
                       (slug, title, description, poster_url, venue, city,
                        starts_at, ends_at, status, created_at)
                       VALUES (:slug, :title, :description, :poster_url, :venue, :city,
                               :starts_at, :ends_at, :status, :created_at)
                       ON CONFLICT(slug) DO UPDATE SET
                         title=excluded.title,
                         description=excluded.description,
                         poster_url=excluded.poster_url,
                         venue=excluded.venue,
                         city=excluded.city,
                         starts_at=excluded.starts_at,
                         ends_at=excluded.ends_at,
                         status=excluded.status"""
                ),
                {
                    "slug": row["slug"],
                    "title": row["title"],
                    "description": row.get("description") or "",
                    "poster_url": row.get("poster_url") or "",
                    "venue": row.get("venue") or "",
                    "city": row.get("city") or "",
                    "starts_at": row.get("starts_at") or "",
                    "ends_at": row.get("ends_at") or "",
                    "status": row.get("status") or "draft",
                    "created_at": row.get("created_at") or db.utc_now_str(),
                },
            )
            target_id = conn.execute(
                text("SELECT id FROM events WHERE slug=:s"), {"s": row["slug"]}
            ).scalar()
            id_map[row["id"]] = target_id
    return id_map


def _copy_ticket_types(
    src: sqlite3.Connection, tables: set[str], event_id_map: dict[int, int]
) -> int:
    if "ticket_types" not in tables:
        return 0
    count = 0
    with db.get_engine().begin() as conn:
        for t in src.execute("SELECT * FROM ticket_types ORDER BY id"):
            row = dict(t)
            target_eid = event_id_map.get(row["event_id"])
            if target_eid is None:
                continue  # orphan ticket type — source event missing
            conn.execute(
                text(
                    """INSERT INTO ticket_types
                       (event_id, name, price_inr, group_size, capacity, sold,
                        sale_open, created_at)
                       VALUES (:event_id, :name, :price_inr, :group_size, :capacity,
                               :sold, :sale_open, :created_at)
                       ON CONFLICT(event_id, name) DO UPDATE SET
                         price_inr=excluded.price_inr,
                         group_size=excluded.group_size,
                         capacity=excluded.capacity,
                         sold=excluded.sold,
                         sale_open=excluded.sale_open"""
                ),
                {
                    "event_id": target_eid,
                    "name": row["name"],
                    "price_inr": row.get("price_inr") or 0,
                    "group_size": row.get("group_size") or 1,
                    "capacity": row.get("capacity") or 0,
                    "sold": row.get("sold") or 0,
                    "sale_open": row.get("sale_open", 1),
                    "created_at": row.get("created_at") or db.utc_now_str(),
                },
            )
            count += 1
    return count


def _mapped_event_id(row: dict, has_col: bool, event_id_map: dict[int, int], villa_id: int) -> int:
    if not has_col or row.get("event_id") is None:
        return villa_id
    return event_id_map.get(row["event_id"], villa_id)


def main() -> None:
    if not SQLITE_DB.exists():
        sys.exit(f"ERROR: SQLite database not found at {SQLITE_DB}")

    src = sqlite3.connect(f"file:{SQLITE_DB}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row

    for table in ("guests", "registrations"):
        cols = [r[1] for r in src.execute(f"PRAGMA table_info({table})")]
        bad = [c for c in cols if AADHAAR_RE.search(c)]
        if bad:
            sys.exit(f"REFUSING TO MIGRATE: table {table} has Aadhaar-like columns: {bad}")

    tables = _source_tables(src)
    reg_cols = [r[1] for r in src.execute("PRAGMA table_info(registrations)")]
    guest_cols = [r[1] for r in src.execute("PRAGMA table_info(guests)")]
    has_reg_event = "event_id" in reg_cols
    has_guest_event = "event_id" in guest_cols

    regs = [dict(r) for r in src.execute("SELECT * FROM registrations ORDER BY id")]
    guests = [dict(r) for r in src.execute("SELECT * FROM guests ORDER BY id")]
    logs = [dict(r) for r in src.execute("SELECT * FROM import_log ORDER BY id")]
    n_src_events = (
        src.execute("SELECT COUNT(*) c FROM events").fetchone()["c"] if "events" in tables else 0
    )
    n_src_tt = (
        src.execute("SELECT COUNT(*) c FROM ticket_types").fetchone()["c"]
        if "ticket_types" in tables
        else 0
    )

    print(f"Source: {SQLITE_DB}")
    print(
        f"  registrations={len(regs)}  guests={len(guests)}  import_log={len(logs)}  "
        f"events={n_src_events}  ticket_types={n_src_tt}"
    )

    db.init_db()
    print(f"Target: {'PostgreSQL' if db.is_postgres() else '??? (DATABASE_URL not pg!)'} — schema ready")

    event_id_map = _copy_events(src, tables)
    print(f"  events copied: {len(event_id_map)}")

    with db.get_conn() as conn:
        villa_id = get_villa_event_id(conn)
    tt_copied = _copy_ticket_types(src, tables, event_id_map)
    print(f"  ticket_types copied: {tt_copied}")

    with db.get_engine().begin() as conn:
        for r in regs:
            conn.execute(
                text(
                    """INSERT INTO registrations
                       (id, reg_id, entry_pass, phone, email, payment_status,
                        needs_review, review_reason, expected_capacity, created_at, event_id)
                       VALUES (:id, :reg_id, :entry_pass, :phone, :email, :payment_status,
                               :needs_review, :review_reason, :expected_capacity,
                               :created_at, :event_id)
                       ON CONFLICT(reg_id) DO UPDATE SET
                         entry_pass=excluded.entry_pass,
                         phone=excluded.phone,
                         email=excluded.email,
                         payment_status=excluded.payment_status,
                         needs_review=excluded.needs_review,
                         review_reason=excluded.review_reason,
                         expected_capacity=excluded.expected_capacity"""
                ),
                {
                    "id": r["id"],
                    "reg_id": r["reg_id"],
                    "entry_pass": r["entry_pass"],
                    "phone": r["phone"],
                    "email": r["email"],
                    "payment_status": r["payment_status"],
                    "needs_review": r["needs_review"],
                    "review_reason": r["review_reason"],
                    "expected_capacity": r.get("expected_capacity") or 0,
                    "created_at": r["created_at"],
                    "event_id": _mapped_event_id(r, has_reg_event, event_id_map, villa_id),
                },
            )
        for g in guests:
            conn.execute(
                text(
                    """INSERT INTO guests
                       (id, reg_id, name, phone, email, payment_status, entry_pass,
                        needs_review, review_reason, added_by, qr_token, checked_in_at,
                        created_at, event_id)
                       VALUES (:id, :reg_id, :name, :phone, :email, :payment_status,
                               :entry_pass, :needs_review, :review_reason, :added_by,
                               :qr_token, :checked_in_at, :created_at, :event_id)
                       ON CONFLICT(reg_id, name) DO UPDATE SET
                         phone=excluded.phone,
                         email=excluded.email,
                         payment_status=excluded.payment_status,
                         entry_pass=excluded.entry_pass,
                         needs_review=excluded.needs_review,
                         review_reason=excluded.review_reason"""
                ),
                {
                    "id": g["id"],
                    "reg_id": g["reg_id"],
                    "name": g["name"],
                    "phone": g["phone"],
                    "email": g["email"],
                    "payment_status": g["payment_status"],
                    "entry_pass": g.get("entry_pass") or "",
                    "needs_review": g["needs_review"],
                    "review_reason": g["review_reason"],
                    "added_by": g.get("added_by") or "csv",
                    "qr_token": g["qr_token"],
                    "checked_in_at": g["checked_in_at"],
                    "created_at": g["created_at"],
                    "event_id": _mapped_event_id(g, has_guest_event, event_id_map, villa_id),
                },
            )
        for lg in logs:
            conn.execute(
                text(
                    """INSERT INTO import_log (file_hash, file_name, registrations, guests, imported_at)
                       SELECT :h, :fn, :regs, :guests, :at
                       WHERE NOT EXISTS (
                           SELECT 1 FROM import_log WHERE file_hash=:h AND imported_at=:at
                       )"""
                ),
                {
                    "h": lg["file_hash"],
                    "fn": lg["file_name"],
                    "regs": lg["registrations"],
                    "guests": lg["guests"],
                    "at": lg["imported_at"],
                },
            )
        for table in ("registrations", "guests", "import_log"):
            conn.execute(
                text(
                    f"SELECT setval(pg_get_serial_sequence('{table}','id'), "
                    f"COALESCE((SELECT MAX(id) FROM {table}), 1))"
                )
            )

    with db.get_conn() as conn:
        backfill_event_ids(conn)
    src.close()

    with db.get_conn() as conn:
        n_regs = conn.execute(text("SELECT COUNT(*) FROM registrations")).scalar()
        n_guests = conn.execute(text("SELECT COUNT(*) FROM guests")).scalar()
        paid = conn.execute(
            text("SELECT COUNT(*) FROM guests WHERE payment_status='PAID'")
        ).scalar()
        unpaid = conn.execute(
            text("SELECT COUNT(*) FROM guests WHERE payment_status='UNPAID'")
        ).scalar()
        src_paid = sum(1 for g in guests if g["payment_status"] == "PAID")
        src_unpaid = sum(1 for g in guests if g["payment_status"] == "UNPAID")
        next_id = conn.execute(text("SELECT nextval(pg_get_serial_sequence('guests','id'))")).scalar()
        villa_rows = conn.execute(
            text("SELECT COUNT(*) FROM events WHERE slug=:s"), {"s": db.VILLA_SLUG}
        ).scalar()
        null_events = conn.execute(
            text(
                "SELECT (SELECT COUNT(*) FROM registrations WHERE event_id IS NULL) + "
                "(SELECT COUNT(*) FROM guests WHERE event_id IS NULL)"
            )
        ).scalar()
        n_events = conn.execute(text("SELECT COUNT(*) FROM events")).scalar()
        n_tt = conn.execute(text("SELECT COUNT(*) FROM ticket_types")).scalar()

    ok = True
    if n_regs != len(regs):
        print(f"FAIL: registrations {n_regs} != {len(regs)}"); ok = False
    if n_guests != len(guests):
        print(f"FAIL: guests {n_guests} != {len(guests)}"); ok = False
    if paid != src_paid or unpaid != src_unpaid:
        print(f"FAIL: payment counts paid={paid}/{src_paid} unpaid={unpaid}/{src_unpaid}"); ok = False
    if next_id <= max(g["id"] for g in guests):
        print(f"FAIL: guests sequence not advanced (nextval={next_id})"); ok = False
    if villa_rows != 1:
        print(f"FAIL: VILLA seed event count = {villa_rows}, expected 1"); ok = False
    if null_events != 0:
        print(f"FAIL: {null_events} rows still have NULL event_id"); ok = False

    print(f"Target: registrations={n_regs} guests={n_guests} paid={paid} unpaid={unpaid}")
    print(f"events={n_events} ticket_types={n_tt} villa_event_rows={villa_rows} null_event_ids={null_events}")
    print(f"Sequence check: next guests id will be {next_id}")
    if not ok:
        sys.exit("MIGRATION VALIDATION FAILED")
    print("MIGRATION OK — all counts, sequences and event links verified.")


if __name__ == "__main__":
    main()
