import csv
import hashlib
import io
import json
import re
import secrets
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from .csv_import import new_qr_token, parse_google_csv
from .database import BACKUP_DIR, DB_PATH, get_conn, init_db
from .models import (
    CheckInResult,
    GuestOut,
    ImportPreview,
    ImportResult,
    MemberOut,
    RegistrationOut,
    Stats,
)

app = FastAPI(title="Party Gate Check-in")
init_db()

templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")


def _row_to_guest(r: sqlite3.Row) -> GuestOut:
    return GuestOut(
        id=r["id"],
        reg_id=r["reg_id"],
        name=r["name"],
        phone=r["phone"],
        email=r["email"],
        payment_status=r["payment_status"],
        entry_pass=r["entry_pass"] if "entry_pass" in r.keys() else "",
        needs_review=bool(r["needs_review"]) if "needs_review" in r.keys() else False,
        review_reason=r["review_reason"] if "review_reason" in r.keys() else "",
        checked_in_at=r["checked_in_at"],
    )


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/api/stats", response_model=Stats)
def stats():
    conn = get_conn()
    try:
        one = lambda q, *a: conn.execute(q, a).fetchone()["c"]
        total = one("SELECT COUNT(*) c FROM guests")
        paid = one("SELECT COUNT(*) c FROM guests WHERE payment_status='PAID'")
        unpaid = one("SELECT COUNT(*) c FROM guests WHERE payment_status='UNPAID'")
        review = one("SELECT COUNT(*) c FROM guests WHERE payment_status='UNDER REVIEW'")
        checked = one("SELECT COUNT(*) c FROM guests WHERE checked_in_at IS NOT NULL")
        regs = one("SELECT COUNT(*) c FROM registrations")
        return Stats(
            registrations=regs,
            total=total,
            paid=paid,
            unpaid=unpaid,
            under_review=review,
            checked_in=checked,
            paid_remaining=paid - one(
                "SELECT COUNT(*) c FROM guests WHERE payment_status='PAID' AND checked_in_at IS NOT NULL"
            ),
            pending_total=total - checked,
            pct=round(100 * checked / total, 1) if total else 0.0,
        )
    finally:
        conn.close()


@app.get("/api/guests", response_model=list[GuestOut])
def search_guests(q: str = ""):
    conn = get_conn()
    try:
        like = f"%{q.strip()}%"
        if not q.strip():
            rows = conn.execute("SELECT * FROM guests ORDER BY name LIMIT 50").fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM guests
                   WHERE name LIKE ? OR phone LIKE ? OR reg_id LIKE ?
                   ORDER BY reg_id, name LIMIT 100""",
                (like, like, like),
            ).fetchall()
        return [_row_to_guest(r) for r in rows]
    finally:
        conn.close()


@app.get("/api/registrations", response_model=list[RegistrationOut])
def search_registrations(q: str = ""):
    """Grouped search: returns registrations with all their members."""
    conn = get_conn()
    try:
        like = f"%{q.strip()}%"
        if not q.strip():
            regs = conn.execute(
                "SELECT * FROM registrations ORDER BY reg_id LIMIT 50"
            ).fetchall()
        else:
            regs = conn.execute(
                """SELECT DISTINCT r.* FROM registrations r
                   JOIN guests g ON g.reg_id = r.reg_id
                   WHERE g.name LIKE ? OR r.phone LIKE ? OR r.reg_id LIKE ?
                   ORDER BY r.reg_id LIMIT 50""",
                (like, like, like),
            ).fetchall()
        out = []
        for r in regs:
            members = conn.execute(
                "SELECT id, name, checked_in_at FROM guests WHERE reg_id=? ORDER BY id",
                (r["reg_id"],),
            ).fetchall()
            out.append(
                RegistrationOut(
                    reg_id=r["reg_id"],
                    entry_pass=r["entry_pass"],
                    phone=r["phone"],
                    email=r["email"],
                    payment_status=r["payment_status"],
                    needs_review=bool(r["needs_review"]),
                    review_reason=r["review_reason"],
                    members=[MemberOut(id=m["id"], name=m["name"], checked_in_at=m["checked_in_at"]) for m in members],
                )
            )
        return out
    finally:
        conn.close()


@app.get("/api/guests/{guest_id}", response_model=GuestOut)
def get_guest(guest_id: int):
    conn = get_conn()
    try:
        r = conn.execute("SELECT * FROM guests WHERE id=?", (guest_id,)).fetchone()
        if not r:
            raise HTTPException(404, "Guest not found")
        return _row_to_guest(r)
    finally:
        conn.close()


@app.post("/api/guests/{guest_id}/checkin", response_model=CheckInResult)
def checkin(guest_id: int):
    conn = get_conn()
    try:
        r = conn.execute("SELECT * FROM guests WHERE id=?", (guest_id,)).fetchone()
        if not r:
            return CheckInResult(ok=False, message="Invalid registration", guest=None)
        if r["checked_in_at"]:
            return CheckInResult(
                ok=False,
                message=f"ALREADY CHECKED IN at {r['checked_in_at']} (UTC)",
                guest=_row_to_guest(r),
            )
        if r["payment_status"] != "PAID":
            return CheckInResult(
                ok=False,
                message=f"PAYMENT NOT VERIFIED ({r['payment_status']}) — entry not allowed",
                guest=_row_to_guest(r),
            )
        cur = conn.execute(
            "UPDATE guests SET checked_in_at=datetime('now') WHERE id=? AND checked_in_at IS NULL",
            (guest_id,),
        )
        conn.commit()
        if cur.rowcount == 0:
            r2 = conn.execute("SELECT * FROM guests WHERE id=?", (guest_id,)).fetchone()
            return CheckInResult(
                ok=False,
                message=f"ALREADY CHECKED IN at {r2['checked_in_at']} (UTC)",
                guest=_row_to_guest(r2),
            )
        r2 = conn.execute("SELECT * FROM guests WHERE id=?", (guest_id,)).fetchone()
        return CheckInResult(ok=True, message=f"CHECKED IN — welcome, {r2['name']}!", guest=_row_to_guest(r2))
    finally:
        conn.close()


@app.post("/api/guests/{guest_id}/undo", response_model=CheckInResult)
def undo(guest_id: int):
    conn = get_conn()
    try:
        conn.execute("UPDATE guests SET checked_in_at=NULL WHERE id=?", (guest_id,))
        conn.commit()
        r = conn.execute("SELECT * FROM guests WHERE id=?", (guest_id,)).fetchone()
        if not r:
            raise HTTPException(404)
        return CheckInResult(ok=True, message="Check-in reset", guest=_row_to_guest(r))
    finally:
        conn.close()


@app.get("/api/list/arrived", response_model=list[GuestOut])
def arrived():
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM guests WHERE checked_in_at IS NOT NULL ORDER BY checked_in_at"
        ).fetchall()
        return [_row_to_guest(r) for r in rows]
    finally:
        conn.close()


@app.get("/api/list/pending", response_model=list[GuestOut])
def pending():
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM guests WHERE checked_in_at IS NULL ORDER BY name"
        ).fetchall()
        return [_row_to_guest(r) for r in rows]
    finally:
        conn.close()


@app.get("/api/list/manual_review", response_model=list[RegistrationOut])
def manual_review():
    conn = get_conn()
    try:
        regs = conn.execute(
            "SELECT * FROM registrations WHERE needs_review=1 ORDER BY reg_id"
        ).fetchall()
        out = []
        for r in regs:
            members = conn.execute(
                "SELECT id, name, checked_in_at FROM guests WHERE reg_id=? ORDER BY id",
                (r["reg_id"],),
            ).fetchall()
            out.append(
                RegistrationOut(
                    reg_id=r["reg_id"],
                    entry_pass=r["entry_pass"],
                    phone=r["phone"],
                    email=r["email"],
                    payment_status=r["payment_status"],
                    needs_review=True,
                    review_reason=r["review_reason"],
                    members=[MemberOut(id=m["id"], name=m["name"], checked_in_at=m["checked_in_at"]) for m in members],
                )
            )
        return out
    finally:
        conn.close()


@app.get("/api/export.csv")
def export():
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT reg_id, name, entry_pass, phone, email, payment_status,
                      needs_review, checked_in_at
               FROM guests ORDER BY reg_id, id"""
        ).fetchall()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(
            ["reg_id", "name", "entry_pass", "phone", "email", "payment_status", "needs_review", "checked_in_at"]
        )
        for r in rows:
            w.writerow(
                [
                    r["reg_id"],
                    r["name"],
                    r["entry_pass"],
                    r["phone"],
                    r["email"],
                    r["payment_status"],
                    "YES" if r["needs_review"] else "",
                    r["checked_in_at"] or "",
                ]
            )
        return PlainTextResponse(
            buf.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=attendance.csv"},
        )
    finally:
        conn.close()


@app.get("/api/qr/{token}", response_model=GuestOut)
def qr_lookup(token: str):
    conn = get_conn()
    try:
        r = conn.execute("SELECT * FROM guests WHERE qr_token=?", (token,)).fetchone()
        if not r:
            raise HTTPException(404, "Invalid QR token")
        return _row_to_guest(r)
    finally:
        conn.close()


# ---------------------------------------------------------------- import ----

@app.post("/api/import/preview", response_model=ImportPreview)
async def import_preview(file: UploadFile = File(...)):
    raw_bytes = await file.read()
    file_hash = hashlib.sha256(raw_bytes).hexdigest()
    text = raw_bytes.decode("utf-8-sig", errors="replace")
    try:
        parsed = parse_google_csv(text)
    except ValueError as e:
        raise HTTPException(400, str(e))

    regs = parsed["registrations"]
    counts = {"Solo": 0, "Couple": 0, "Duo": 0, "Trio": 0, "Other": 0}
    paid = unpaid = review = manual = 0
    cases = []
    individuals = 0
    for r in regs:
        individuals += len(r["names"])
        p = r["entry_pass"].capitalize()
        counts[p if p in counts else "Other"] += 1
        if r["payment_status"] == "PAID":
            paid += 1
        elif r["payment_status"] == "UNPAID":
            unpaid += 1
        else:
            review += 1
        if r["needs_review"]:
            manual += 1
            cases.append(
                {
                    "reg_id": r["reg_id"],
                    "entry_pass": r["entry_pass"],
                    "names": r["names"],
                    "reason": r["review_reason"],
                    "phone": r["phone"],
                }
            )

    conn = get_conn()
    dup_msg = None
    try:
        prior = conn.execute(
            "SELECT imported_at FROM import_log WHERE file_hash=? ORDER BY imported_at DESC LIMIT 1",
            (file_hash,),
        ).fetchone()
        if prior:
            dup_msg = (
                f"This exact file was already imported on {prior['imported_at']} UTC. "
                "Re-importing will UPDATE existing registrations (check-in status is preserved)."
            )
        token = secrets.token_hex(16)
        conn.execute(
            "INSERT INTO staged_imports (token, file_hash, file_name, payload) VALUES (?,?,?,?)",
            (token, file_hash, file.filename or "upload.csv", json.dumps(regs)),
        )
        conn.commit()
    finally:
        conn.close()

    return ImportPreview(
        token=token,
        file_name=file.filename or "upload.csv",
        registration_rows=len(regs),
        blank_rows_skipped=parsed["blank_rows_skipped"],
        individual_guests=individuals,
        solo=counts["Solo"],
        couple=counts["Couple"],
        duo=counts["Duo"],
        trio=counts["Trio"],
        other_pass=counts["Other"],
        paid_regs=paid,
        unpaid_regs=unpaid,
        review_regs=review,
        manual_review_regs=manual,
        manual_review_cases=cases,
        duplicate_file_warning=dup_msg,
        aadhaar_columns_ignored=parsed["aadhaar_columns_ignored"],
        sample_guests=[
            {"reg_id": r["reg_id"], "entry_pass": r["entry_pass"], "names": r["names"], "payment_status": r["payment_status"]}
            for r in regs[:5]
        ],
    )


@app.post("/api/import/confirm", response_model=ImportResult)
async def import_confirm(token: str, wipe_existing: bool = False):
    conn = get_conn()
    conn.isolation_level = None  # manual transaction control
    try:
        staged = conn.execute(
            "SELECT * FROM staged_imports WHERE token=?", (token,)
        ).fetchone()
        if not staged:
            raise HTTPException(400, "Import token missing or already used. Upload the CSV again.")
        regs = json.loads(staged["payload"])
        file_hash = staged["file_hash"]
        file_name = staged["file_name"]

        # Safety: refuse to wipe if not explicitly requested, and always back up first.
        backup_file = ""
        if wipe_existing:
            ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
            dest = BACKUP_DIR / f"party-backup-{ts}.db"
            conn.execute("PRAGMA wal_checkpoint(FULL)")
            shutil.copy2(DB_PATH, dest)
            backup_file = str(dest.name)

        try:
            conn.execute("BEGIN")
            if wipe_existing:
                conn.execute("DELETE FROM guests")
                conn.execute("DELETE FROM registrations")

            inserted_regs = inserted_guests = updated = 0
            for r in regs:
                existed = conn.execute(
                    "SELECT 1 FROM registrations WHERE reg_id=?", (r["reg_id"],)
                ).fetchone()
                conn.execute(
                    """INSERT INTO registrations
                       (reg_id, entry_pass, phone, email, payment_status, needs_review, review_reason)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(reg_id) DO UPDATE SET
                         entry_pass=excluded.entry_pass,
                         phone=excluded.phone,
                         email=excluded.email,
                         payment_status=excluded.payment_status,
                         needs_review=excluded.needs_review,
                         review_reason=excluded.review_reason""",
                    (
                        r["reg_id"],
                        r["entry_pass"],
                        r["phone"],
                        r["email"],
                        r["payment_status"],
                        r["needs_review"],
                        r["review_reason"],
                    ),
                )
                if not existed:
                    inserted_regs += 1
                for name in r["names"]:
                    existed = conn.execute(
                        "SELECT 1 FROM guests WHERE reg_id=? AND name=?",
                        (r["reg_id"], name),
                    ).fetchone()
                    conn.execute(
                        """INSERT INTO guests
                           (reg_id, name, phone, email, payment_status, entry_pass,
                            needs_review, review_reason, qr_token)
                           VALUES (?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(reg_id, name) DO UPDATE SET
                             phone=excluded.phone,
                             email=excluded.email,
                             payment_status=excluded.payment_status,
                             entry_pass=excluded.entry_pass,
                             needs_review=excluded.needs_review,
                             review_reason=excluded.review_reason""",
                        (
                            r["reg_id"],
                            name,
                            r["phone"],
                            r["email"],
                            r["payment_status"],
                            r["entry_pass"],
                            r["needs_review"],
                            r["review_reason"],
                            new_qr_token(),
                        ),
                    )
                    if existed:
                        updated += 1
                    else:
                        inserted_guests += 1

            conn.execute(
                "INSERT INTO import_log (file_hash, file_name, registrations, guests) VALUES (?,?,?,?)",
                (file_hash, file_name, len(regs), inserted_guests),
            )
            conn.execute("DELETE FROM staged_imports WHERE token=?", (token,))
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise HTTPException(500, f"Import rolled back, database unchanged: {e}")

        return ImportResult(
            ok=True,
            message=(
                f"Imported {len(regs)} registrations / {inserted_guests} guests"
                + (f" ({updated} existing updated)" if updated else "")
                + (f". Backup saved as {backup_file}." if backup_file else ".")
            ),
            registrations=len(regs),
            guests=inserted_guests,
            updated=updated,
            backup_file=backup_file,
        )
    finally:
        conn.close()


# ----------------------------------------------------------------- admin ----

@app.post("/api/admin/reset_all")
def reset_all():
    conn = get_conn()
    try:
        n = conn.execute(
            "UPDATE guests SET checked_in_at=NULL WHERE checked_in_at IS NOT NULL"
        ).rowcount
        conn.commit()
        return {"reset": n}
    finally:
        conn.close()


@app.post("/api/admin/reset_guest/{guest_id}")
def reset_guest(guest_id: int):
    return undo(guest_id)


@app.post("/api/admin/wipe_guests")
def wipe_guests():
    """Explicit destructive action: remove all guest/registration data."""
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_DIR / f"party-backup-{ts}.db"
    conn = get_conn()
    try:
        conn.execute("PRAGMA wal_checkpoint(FULL)")
        shutil.copy2(DB_PATH, dest)
        n = conn.execute("SELECT COUNT(*) c FROM guests").fetchone()["c"]
        conn.execute("DELETE FROM guests")
        conn.execute("DELETE FROM registrations")
        conn.execute("DELETE FROM staged_imports")
        conn.commit()
        return {"wiped_guests": n, "backup_file": dest.name}
    finally:
        conn.close()
