import csv
import hashlib
import io
import json
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import bindparam, text

from . import auth
from .csv_import import new_qr_token, parse_google_csv
from .database import ROOT, do_backup, get_conn, get_engine, get_villa_event_id, init_db, utc_now_str
from .models import (
    CheckInResult,
    EventCreateIn,
    EventEditIn,
    EventMemberOut,
    EventOut,
    GuestOut,
    ImportPreview,
    ImportResult,
    InviteAcceptIn,
    InviteCreateIn,
    InviteOut,
    InviteStatusOut,
    LoginIn,
    MemberAddIn,
    MemberOut,
    MemberRoleIn,
    RegistrationOut,
    Stats,
    TicketTypeCreateIn,
    TicketTypeEditIn,
    TicketTypeOut,
)

EVENT_STATUSES = {"draft", "published", "closed"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()  # retry if the import-time init failed (e.g. DB briefly down)
    yield


app = FastAPI(title="Party Gate Check-in", lifespan=lifespan)

try:
    init_db()
except Exception:
    pass  # surfaced properly via lifespan /health, not at import time

app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.middleware("http")
async def session_middleware(request: Request, call_next):
    """Default-deny for /api/*: a valid session cookie is required everywhere
    except the public allowlist (health/login/logout/me/invites). Static
    files and the index page are never gated."""
    path = request.url.path
    if path.startswith("/api/"):
        user = auth.load_user_from_request(request)
        request.state.user = user
        if user is None and not auth.is_public(path):
            # Same body for unknown-email and no-cookie: no oracle.
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    return await call_next(request)


def _row_to_guest(r) -> GuestOut:
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
        added_by=r["added_by"] if "added_by" in r.keys() else "csv",
        checked_in_at=r["checked_in_at"],
        event_id=r["event_id"] if "event_id" in r.keys() else None,
    )


def _reg_out(r, members) -> RegistrationOut:
    expected = r["expected_capacity"] if "expected_capacity" in r.keys() else 0
    return RegistrationOut(
        reg_id=r["reg_id"],
        entry_pass=r["entry_pass"],
        phone=r["phone"],
        email=r["email"],
        payment_status=r["payment_status"],
        needs_review=bool(r["needs_review"]),
        review_reason=r["review_reason"],
        expected_capacity=expected or len(members),
        named_count=len(members),
        members=[MemberOut(id=m["id"], name=m["name"], checked_in_at=m["checked_in_at"]) for m in members],
        event_id=r["event_id"] if "event_id" in r.keys() else None,
    )


def _tt_out(t) -> TicketTypeOut:
    return TicketTypeOut(
        id=t["id"],
        event_id=t["event_id"],
        name=t["name"],
        price_inr=t["price_inr"],
        group_size=t["group_size"],
        capacity=t["capacity"],
        sold=t["sold"],
        sale_open=bool(t["sale_open"]),
    )


def _event_out(e, ticket_types) -> EventOut:
    return EventOut(
        id=e["id"],
        slug=e["slug"],
        title=e["title"],
        description=e["description"] or "",
        poster_url=e["poster_url"] or "",
        venue=e["venue"] or "",
        city=e["city"] or "",
        starts_at=e["starts_at"] or "",
        ends_at=e["ends_at"] or "",
        status=e["status"],
        created_at=e["created_at"] or "",
        ticket_types=[_tt_out(t) for t in ticket_types],
    )


def _load_event(conn, event_id: int) -> EventOut:
    e = conn.execute(
        text("SELECT * FROM events WHERE id=:i"), {"i": event_id}
    ).mappings().fetchone()
    if not e:
        raise HTTPException(404, "Event not found")
    tts = conn.execute(
        text("SELECT * FROM ticket_types WHERE event_id=:e ORDER BY id"),
        {"e": event_id},
    ).mappings().all()
    return _event_out(e, tts)


def slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def _q(sql: str):
    """text() with an expanding :ids bindparam (portable IN across SQLite/PG)."""
    return text(sql).bindparams(bindparam("ids", expanding=True))


def _scoped_ids(conn, user, action) -> list[int]:
    """Event ids the user may act on, or 403 when there are none."""
    ids = list(auth.accessible_event_ids(conn, user, action))
    if not ids:
        raise HTTPException(403, "Your role does not allow this action")
    return ids


@app.get("/", response_class=FileResponse)
def index():
    return FileResponse(ROOT / "index.html", media_type="text/html")


@app.get("/api/health")
def health():
    try:
        with get_conn() as conn:
            conn.execute(text("SELECT 1"))
        return {"ok": True, "db": get_engine().dialect.name}
    except Exception as e:
        raise HTTPException(503, f"Database unavailable: {e}")


# ------------------------------------------------------------------ auth ----

_BAD_CREDENTIALS = "Invalid email or password"


@app.post("/api/login")
def login(body: LoginIn, request: Request):
    ip = auth.client_ip(request)
    email = body.email.strip().lower()
    outcome = None
    user_out = None
    with get_conn() as conn:
        try:
            auth.check_rate_limit(conn, ip, email)
        except HTTPException:
            outcome = "rate_limited"
        if outcome is None:
            user = auth.user_by_email(conn, email)
            if user and auth.verify_password(body.password, user["password_hash"]):
                auth.clear_failures(conn, email)
                outcome = "ok"
                user_out = user
            else:
                if not user:
                    # Unknown email: burn one PBKDF2 so timing matches
                    # a wrong password (no user enumeration).
                    auth.burn_password_time(body.password)
                auth.record_failure(conn, ip, email)
                outcome = "bad_credentials"
    # HTTPExceptions are raised AFTER the with-block so record_failure commits
    # (get_conn rolls back on an exception raised inside it).
    if outcome == "rate_limited":
        raise HTTPException(
            429,
            f"Too many failed attempts — try again in {auth.RATE_LIMIT_WINDOW_MIN} minutes",
        )
    if outcome == "bad_credentials":
        raise HTTPException(401, _BAD_CREDENTIALS)
    response = JSONResponse(
        {
            "ok": True,
            "user": {
                "id": user_out["id"],
                "name": user_out["name"],
                "email": user_out["email"],
                "is_owner": user_out["is_owner"],
            },
        }
    )
    auth.set_session_cookie(response, request, user_out["id"])
    return response


@app.post("/api/logout")
def logout(request: Request):
    response = JSONResponse({"ok": True})
    auth.clear_session_cookie(response, request)
    return response


@app.get("/api/me")
def me(request: Request):
    user = getattr(request.state, "user", None)
    if not user:
        return {"user": None}
    with get_conn() as conn:
        memberships = [
            {"event_id": r["event_id"], "role": r["role"]}
            for r in conn.execute(
                text("SELECT event_id, role FROM event_members WHERE user_id=:u ORDER BY event_id"),
                {"u": user["id"]},
            ).mappings().all()
        ]
    return {"user": {**user, "memberships": memberships}}


@app.get("/api/stats", response_model=Stats)
def stats(request: Request):
    user = auth.require_user(request)
    with get_conn() as conn:
        ids = _scoped_ids(conn, user, "view")
        params = {"ids": ids}
        one = lambda q: conn.execute(_q(q), params).scalar() or 0
        total = one("SELECT COUNT(*) FROM guests WHERE event_id IN :ids")
        paid = one("SELECT COUNT(*) FROM guests WHERE payment_status='PAID' AND event_id IN :ids")
        unpaid = one("SELECT COUNT(*) FROM guests WHERE payment_status='UNPAID' AND event_id IN :ids")
        review = one("SELECT COUNT(*) FROM guests WHERE payment_status='UNDER REVIEW' AND event_id IN :ids")
        checked = one("SELECT COUNT(*) FROM guests WHERE checked_in_at IS NOT NULL AND event_id IN :ids")
        regs = one("SELECT COUNT(*) FROM registrations WHERE event_id IN :ids")
        capacity = one("SELECT COALESCE(SUM(expected_capacity),0) FROM registrations WHERE event_id IN :ids")
        incomplete = one("SELECT COUNT(*) FROM registrations WHERE needs_review=1 AND event_id IN :ids")
        return Stats(
            registrations=regs,
            total=total,
            expected_capacity=capacity,
            paid=paid,
            unpaid=unpaid,
            under_review=review,
            checked_in=checked,
            paid_remaining=paid - one(
                "SELECT COUNT(*) FROM guests WHERE payment_status='PAID' AND checked_in_at IS NOT NULL AND event_id IN :ids"
            ),
            pending_total=total - checked,
            incomplete_groups=incomplete,
            pct=round(100 * checked / total, 1) if total else 0.0,
        )


@app.get("/api/guests", response_model=list[GuestOut])
def search_guests(request: Request, q: str = ""):
    user = auth.require_user(request)
    with get_conn() as conn:
        ids = _scoped_ids(conn, user, "search")
        params = {"ids": ids}
        if not q.strip():
            rows = conn.execute(
                _q("SELECT * FROM guests WHERE event_id IN :ids ORDER BY name LIMIT 50"), params
            ).mappings().all()
        else:
            like = f"%{q.strip()}%"
            rows = (
                conn.execute(
                    _q(
                        """SELECT * FROM guests
                           WHERE event_id IN :ids AND (lower(name) LIKE lower(:like)
                              OR lower(phone) LIKE lower(:like) OR lower(reg_id) LIKE lower(:like))
                           ORDER BY reg_id, name LIMIT 100"""
                    ),
                    {**params, "like": like},
                )
                .mappings()
                .all()
            )
        return [_row_to_guest(r) for r in rows]


@app.get("/api/registrations", response_model=list[RegistrationOut])
def search_registrations(request: Request, q: str = ""):
    """Grouped search: returns registrations with all their members."""
    user = auth.require_user(request)
    with get_conn() as conn:
        ids = _scoped_ids(conn, user, "search")
        params = {"ids": ids}
        if not q.strip():
            regs = conn.execute(
                _q("SELECT * FROM registrations WHERE event_id IN :ids ORDER BY reg_id LIMIT 50"),
                params,
            ).mappings().all()
        else:
            like = f"%{q.strip()}%"
            regs = (
                conn.execute(
                    _q(
                        """SELECT DISTINCT r.* FROM registrations r
                           JOIN guests g ON g.reg_id = r.reg_id
                           WHERE r.event_id IN :ids AND (lower(g.name) LIKE lower(:like)
                              OR lower(r.phone) LIKE lower(:like) OR lower(r.reg_id) LIKE lower(:like))
                           ORDER BY r.reg_id LIMIT 50"""
                    ),
                    {**params, "like": like},
                )
                .mappings()
                .all()
            )
        out = []
        for r in regs:
            members = (
                conn.execute(
                    text("SELECT id, name, checked_in_at FROM guests WHERE reg_id=:reg ORDER BY id"),
                    {"reg": r["reg_id"]},
                )
                .mappings()
                .all()
            )
            out.append(_reg_out(r, members))
        return out


@app.get("/api/guests/{guest_id}", response_model=GuestOut)
def get_guest(guest_id: int, request: Request):
    user = auth.require_user(request)
    with get_conn() as conn:
        r = (
            conn.execute(text("SELECT * FROM guests WHERE id=:id"), {"id": guest_id})
            .mappings()
            .fetchone()
        )
        if not r:
            raise HTTPException(404, "Guest not found")
        auth.require_action(conn, user, "view", _guest_event_id(conn, r))
        return _row_to_guest(r)


def _guest_event_id(conn, r) -> int:
    """Event a guest row belongs to (falls back to the VILLA event)."""
    if "event_id" in r.keys() and r["event_id"]:
        return r["event_id"]
    return get_villa_event_id(conn)


@app.post("/api/guests/{guest_id}/checkin", response_model=CheckInResult)
def checkin(guest_id: int, request: Request):
    user = auth.require_user(request)
    with get_conn() as conn:
        r = (
            conn.execute(text("SELECT * FROM guests WHERE id=:id"), {"id": guest_id})
            .mappings()
            .fetchone()
        )
        if not r:
            return CheckInResult(ok=False, message="Invalid registration", guest=None)
        auth.require_action(conn, user, "checkin", _guest_event_id(conn, r))
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
            text(
                "UPDATE guests SET checked_in_at=:ts WHERE id=:id AND checked_in_at IS NULL"
            ),
            {"ts": utc_now_str(), "id": guest_id},
        )
        if cur.rowcount == 0:
            r2 = (
                conn.execute(text("SELECT * FROM guests WHERE id=:id"), {"id": guest_id})
                .mappings()
                .fetchone()
            )
            return CheckInResult(
                ok=False,
                message=f"ALREADY CHECKED IN at {r2['checked_in_at']} (UTC)",
                guest=_row_to_guest(r2),
            )
        r2 = (
            conn.execute(text("SELECT * FROM guests WHERE id=:id"), {"id": guest_id})
            .mappings()
            .fetchone()
        )
        return CheckInResult(ok=True, message=f"CHECKED IN — welcome, {r2['name']}!", guest=_row_to_guest(r2))


@app.post("/api/guests/{guest_id}/undo", response_model=CheckInResult)
def undo(guest_id: int, request: Request):
    user = auth.require_user(request)
    with get_conn() as conn:
        r0 = (
            conn.execute(text("SELECT * FROM guests WHERE id=:id"), {"id": guest_id})
            .mappings()
            .fetchone()
        )
        if not r0:
            raise HTTPException(404)
        auth.require_action(conn, user, "undo", _guest_event_id(conn, r0))
        conn.execute(text("UPDATE guests SET checked_in_at=NULL WHERE id=:id"), {"id": guest_id})
        r = (
            conn.execute(text("SELECT * FROM guests WHERE id=:id"), {"id": guest_id})
            .mappings()
            .fetchone()
        )
        if not r:
            raise HTTPException(404)
        return CheckInResult(ok=True, message="Check-in reset", guest=_row_to_guest(r))


@app.get("/api/list/arrived", response_model=list[GuestOut])
def arrived(request: Request):
    user = auth.require_user(request)
    with get_conn() as conn:
        ids = _scoped_ids(conn, user, "view")
        rows = (
            conn.execute(
                _q("SELECT * FROM guests WHERE checked_in_at IS NOT NULL AND event_id IN :ids ORDER BY checked_in_at"),
                {"ids": ids},
            )
            .mappings()
            .all()
        )
        return [_row_to_guest(r) for r in rows]


@app.get("/api/list/pending", response_model=list[GuestOut])
def pending(request: Request):
    user = auth.require_user(request)
    with get_conn() as conn:
        ids = _scoped_ids(conn, user, "view")
        rows = (
            conn.execute(
                _q("SELECT * FROM guests WHERE checked_in_at IS NULL AND event_id IN :ids ORDER BY name"),
                {"ids": ids},
            )
            .mappings()
            .all()
        )
        return [_row_to_guest(r) for r in rows]


@app.get("/api/list/manual_review", response_model=list[RegistrationOut])
def manual_review(request: Request):
    user = auth.require_user(request)
    with get_conn() as conn:
        ids = _scoped_ids(conn, user, "view")
        regs = (
            conn.execute(
                _q("SELECT * FROM registrations WHERE needs_review=1 AND event_id IN :ids ORDER BY reg_id"),
                {"ids": ids},
            )
            .mappings()
            .all()
        )
        out = []
        for r in regs:
            members = (
                conn.execute(
                    text("SELECT id, name, checked_in_at FROM guests WHERE reg_id=:reg ORDER BY id"),
                    {"reg": r["reg_id"]},
                )
                .mappings()
                .all()
            )
            out.append(_reg_out(r, members))
        return out


@app.get("/api/export.csv")
def export(request: Request):
    user = auth.require_user(request)
    with get_conn() as conn:
        ids = _scoped_ids(conn, user, "export")
        rows = (
            conn.execute(
                _q(
                    """SELECT reg_id, name, entry_pass, phone, email, payment_status,
                              needs_review, added_by, checked_in_at
                       FROM guests WHERE event_id IN :ids ORDER BY reg_id, id"""
                ),
                {"ids": ids},
            )
            .mappings()
            .all()
        )
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(
            [
                "reg_id",
                "name",
                "entry_pass",
                "phone",
                "email",
                "payment_status",
                "check_in_status",
                "checked_in_at",
                "added_by",
                "incomplete_group",
            ]
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
                    "CHECKED IN" if r["checked_in_at"] else "PENDING",
                    r["checked_in_at"] or "",
                    r["added_by"] if "added_by" in r.keys() else "csv",
                    "YES" if r["needs_review"] else "",
                ]
            )
        return PlainTextResponse(
            buf.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=attendance.csv"},
        )


@app.get("/api/qr/{token}", response_model=GuestOut)
def qr_lookup(token: str, request: Request):
    user = auth.require_user(request)
    with get_conn() as conn:
        r = (
            conn.execute(text("SELECT * FROM guests WHERE qr_token=:t"), {"t": token})
            .mappings()
            .fetchone()
        )
        if not r:
            raise HTTPException(404, "Invalid QR token")
        auth.require_action(conn, user, "view", _guest_event_id(conn, r))
        return _row_to_guest(r)


# ---------------------------------------------------------------- import ----

@app.post("/api/import/preview", response_model=ImportPreview)
async def import_preview(request: Request, file: UploadFile = File(...), event_id: Optional[int] = None):
    user = auth.require_user(request)
    with get_conn() as conn:
        target = event_id or get_villa_event_id(conn)
        _require_event(conn, target)
        auth.require_action(conn, user, "import", target)
    raw_bytes = await file.read()
    file_hash = hashlib.sha256(raw_bytes).hexdigest()
    text_content = raw_bytes.decode("utf-8-sig", errors="replace")
    try:
        parsed = parse_google_csv(text_content)
    except ValueError as e:
        raise HTTPException(400, str(e))

    regs = parsed["registrations"]
    counts = {"Solo": 0, "Couple": 0, "Duo": 0, "Trio": 0, "Other": 0}
    paid = unpaid = review = manual = 0
    cases = []
    individuals = 0
    capacity = 0
    for r in regs:
        individuals += len(r["names"])
        capacity += r["expected_capacity"]
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
                    "expected": r["expected_capacity"],
                    "reason": r["review_reason"],
                    "phone": r["phone"],
                    "payment_status": r["payment_status"],
                }
            )

    with get_conn() as conn:
        prior = (
            conn.execute(
                text(
                    "SELECT imported_at FROM import_log WHERE file_hash=:h "
                    "ORDER BY imported_at DESC LIMIT 1"
                ),
                {"h": file_hash},
            )
            .mappings()
            .fetchone()
        )
        dup_msg = None
        if prior:
            dup_msg = (
                f"This exact file was already imported on {prior['imported_at']} UTC. "
                "Re-importing will UPDATE existing registrations (check-in status is preserved)."
            )
        token = secrets.token_hex(16)
        conn.execute(
            text(
                "INSERT INTO staged_imports (token, file_hash, file_name, payload) "
                "VALUES (:tok, :h, :fn, :p)"
            ),
            {
                "tok": token,
                "h": file_hash,
                "fn": file.filename or "upload.csv",
                "p": json.dumps({"event_id": target, "regs": regs}),
            },
        )

    return ImportPreview(
        token=token,
        file_name=file.filename or "upload.csv",
        registration_rows=len(regs),
        blank_rows_skipped=parsed["blank_rows_skipped"],
        individual_guests=individuals,
        expected_capacity=capacity,
        incomplete_groups=manual,
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
async def import_confirm(request: Request, token: str, wipe_existing: bool = False):
    user = auth.require_user(request)
    with get_conn() as conn:
        staged = (
            conn.execute(text("SELECT * FROM staged_imports WHERE token=:tok"), {"tok": token})
            .mappings()
            .fetchone()
        )
        if not staged:
            raise HTTPException(400, "Import token missing or already used. Upload the CSV again.")
        data = json.loads(staged["payload"])
        if isinstance(data, dict):
            target = data.get("event_id") or get_villa_event_id(conn)
            regs = data.get("regs", [])
        else:  # legacy staged payload (a plain list of registrations)
            target = get_villa_event_id(conn)
            regs = data
        _require_event(conn, target)
        auth.require_action(conn, user, "import", target)
        file_hash = staged["file_hash"]
        file_name = staged["file_name"]

    # ALWAYS back up the current data before writing (timestamped snapshot),
    # whether this is an update or an explicit wipe.
    backup_file = do_backup()

    try:
        with get_engine().begin() as conn:
            event_id = target
            if wipe_existing:
                conn.execute(text("DELETE FROM guests WHERE event_id=:e"), {"e": event_id})
                conn.execute(text("DELETE FROM registrations WHERE event_id=:e"), {"e": event_id})

            inserted_regs = inserted_guests = updated = 0
            for r in regs:
                existed = conn.execute(
                    text("SELECT 1 FROM registrations WHERE reg_id=:reg"), {"reg": r["reg_id"]}
                ).fetchone()
                conn.execute(
                    text(
                        """INSERT INTO registrations
                           (reg_id, entry_pass, phone, email, payment_status,
                            needs_review, review_reason, expected_capacity, event_id)
                           VALUES (:reg_id, :entry_pass, :phone, :email, :payment_status,
                                   :needs_review, :review_reason, :expected_capacity, :event_id)
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
                        "reg_id": r["reg_id"],
                        "entry_pass": r["entry_pass"],
                        "phone": r["phone"],
                        "email": r["email"],
                        "payment_status": r["payment_status"],
                        "needs_review": r["needs_review"],
                        "review_reason": r["review_reason"],
                        "expected_capacity": r["expected_capacity"],
                        "event_id": event_id,
                    },
                )
                if not existed:
                    inserted_regs += 1
                for name in r["names"]:
                    existed = conn.execute(
                        text("SELECT 1 FROM guests WHERE reg_id=:reg AND name=:name"),
                        {"reg": r["reg_id"], "name": name},
                    ).fetchone()
                    conn.execute(
                        text(
                            """INSERT INTO guests
                               (reg_id, name, phone, email, payment_status, entry_pass,
                                needs_review, review_reason, qr_token, event_id)
                               VALUES (:reg_id, :name, :phone, :email, :payment_status, :entry_pass,
                                       :needs_review, :review_reason, :qr_token, :event_id)
                               ON CONFLICT(reg_id, name) DO UPDATE SET
                                 phone=excluded.phone,
                                 email=excluded.email,
                                 payment_status=excluded.payment_status,
                                 entry_pass=excluded.entry_pass,
                                 needs_review=excluded.needs_review,
                                 review_reason=excluded.review_reason"""
                        ),
                        {
                            "reg_id": r["reg_id"],
                            "name": name,
                            "phone": r["phone"],
                            "email": r["email"],
                            "payment_status": r["payment_status"],
                            "entry_pass": r["entry_pass"],
                            "needs_review": r["needs_review"],
                            "review_reason": r["review_reason"],
                            "qr_token": new_qr_token(),
                            "event_id": event_id,
                        },
                    )
                    if existed:
                        updated += 1
                    else:
                        inserted_guests += 1

            conn.execute(
                text(
                    "INSERT INTO import_log (file_hash, file_name, registrations, guests) "
                    "VALUES (:h, :fn, :regs, :guests)"
                ),
                {"h": file_hash, "fn": file_name, "regs": len(regs), "guests": inserted_guests},
            )
            conn.execute(text("DELETE FROM staged_imports WHERE token=:tok"), {"tok": token})
    except HTTPException:
        raise
    except Exception as e:
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


# ----------------------------------------------------------------- admin ----

@app.post("/api/admin/reset_all")
def reset_all(request: Request):
    user = auth.require_user(request)
    with get_conn() as conn:
        auth.require_any_action(conn, user, "reset_all")
        n = conn.execute(
            text("UPDATE guests SET checked_in_at=NULL WHERE checked_in_at IS NOT NULL")
        ).rowcount
        return {"reset": n}


@app.post("/api/admin/reset_guest/{guest_id}")
def reset_guest(guest_id: int, request: Request):
    return undo(guest_id, request)


@app.post("/api/admin/wipe_guests")
def wipe_guests(request: Request):
    """Explicit destructive action: remove all guest/registration data."""
    user = auth.require_user(request)
    with get_conn() as conn:
        auth.require_any_action(conn, user, "wipe")
    backup_file = do_backup()
    with get_conn() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM guests")).scalar() or 0
        conn.execute(text("DELETE FROM guests"))
        conn.execute(text("DELETE FROM registrations"))
        conn.execute(text("DELETE FROM staged_imports"))
        return {"wiped_guests": n, "backup_file": backup_file}


@app.post("/api/admin/add_companion")
def add_companion(request: Request, reg_id: str, name: str):
    """Admin-only: add a missing companion name to an incomplete group.

    The added guest is recorded as added_by='admin' (not CSV-supplied),
    inherits the registration's payment status / pass type, and gets its
    own independent check-in status.
    """
    user = auth.require_user(request)
    names = [n.strip() for n in name.replace("\n", ",").split(",") if n.strip()]
    if not names:
        raise HTTPException(400, "Provide at least one companion name")

    with get_conn() as conn:
        reg = (
            conn.execute(text("SELECT * FROM registrations WHERE reg_id=:reg"), {"reg": reg_id})
            .mappings()
            .fetchone()
        )
        if not reg:
            raise HTTPException(404, "Registration not found")
        reg_event = reg["event_id"] if "event_id" in reg.keys() and reg["event_id"] else get_villa_event_id(conn)
        auth.require_action(conn, user, "add_companion", reg_event)
        expected = reg["expected_capacity"] or 0
        event_id = reg["event_id"] or get_villa_event_id(conn)
        added = []
        for n in names:
            exists = conn.execute(
                text("SELECT 1 FROM guests WHERE reg_id=:reg AND lower(name)=lower(:n)"),
                {"reg": reg_id, "n": n},
            ).fetchone()
            if exists:
                continue
            named_now = conn.execute(
                text("SELECT COUNT(*) FROM guests WHERE reg_id=:reg"), {"reg": reg_id}
            ).scalar() or 0
            if expected and named_now >= expected:
                raise HTTPException(
                    400,
                    f"{reg_id}: capacity full ({named_now}/{expected} named) — not adding '{n}'",
                )
            conn.execute(
                text(
                    """INSERT INTO guests
                       (reg_id, name, phone, email, payment_status, entry_pass,
                        needs_review, review_reason, qr_token, added_by, event_id)
                       VALUES (:reg_id, :name, :phone, :email, :payment_status, :entry_pass,
                               :needs_review, :review_reason, :qr_token, 'admin', :event_id)"""
                ),
                {
                    "reg_id": reg_id,
                    "name": n,
                    "phone": reg["phone"],
                    "email": reg["email"],
                    "payment_status": reg["payment_status"],
                    "entry_pass": reg["entry_pass"],
                    "needs_review": reg["needs_review"],
                    "review_reason": reg["review_reason"],
                    "qr_token": new_qr_token(),
                    "event_id": event_id,
                },
            )
            added.append(n)
        if not added:
            raise HTTPException(400, "Name(s) already present in this registration")

        # If the group is now fully named, clear the incomplete-group flag.
        named_now = conn.execute(
            text("SELECT COUNT(*) FROM guests WHERE reg_id=:reg"), {"reg": reg_id}
        ).scalar() or 0
        if expected and named_now >= expected:
            conn.execute(
                text("UPDATE registrations SET needs_review=0, review_reason='' WHERE reg_id=:reg"),
                {"reg": reg_id},
            )
            conn.execute(
                text("UPDATE guests SET needs_review=0, review_reason='' WHERE reg_id=:reg"),
                {"reg": reg_id},
            )

        reg2 = (
            conn.execute(text("SELECT * FROM registrations WHERE reg_id=:reg"), {"reg": reg_id})
            .mappings()
            .fetchone()
        )
        members = (
            conn.execute(
                text("SELECT id, name, checked_in_at FROM guests WHERE reg_id=:reg ORDER BY id"),
                {"reg": reg_id},
            )
            .mappings()
            .all()
        )
        return {
            "ok": True,
            "added": added,
            "added_by": "admin",
            "registration": _reg_out(reg2, members).model_dump(),
        }


# --------------------------------------------------------------- events ----

@app.get("/api/admin/events", response_model=list[EventOut])
def admin_list_events(request: Request):
    user = auth.require_user(request)
    with get_conn() as conn:
        if user["is_owner"]:
            events = conn.execute(text("SELECT * FROM events ORDER BY id")).mappings().all()
        else:
            # Non-owners only see events they are a member of.
            events = (
                conn.execute(
                    text(
                        """SELECT e.* FROM events e
                           JOIN event_members m ON m.event_id = e.id
                           WHERE m.user_id = :u ORDER BY e.id"""
                    ),
                    {"u": user["id"]},
                )
                .mappings()
                .all()
            )
        out = []
        for e in events:
            tts = conn.execute(
                text("SELECT * FROM ticket_types WHERE event_id=:e ORDER BY id"),
                {"e": e["id"]},
            ).mappings().all()
            out.append(_event_out(e, tts))
        return out


@app.post("/api/admin/events", response_model=EventOut)
def admin_create_event(body: EventCreateIn, request: Request):
    user = auth.require_user(request)
    with get_conn() as conn:
        auth.require_any_action(conn, user, "create_event")
    if body.status not in EVENT_STATUSES:
        raise HTTPException(400, f"status must be one of {sorted(EVENT_STATUSES)}")
    if not body.title.strip():
        raise HTTPException(400, "title is required")
    slug = body.slug.strip() or slugify(body.title)
    if not slug:
        raise HTTPException(400, "Could not derive a slug from the title")
    with get_conn() as conn:
        dup = conn.execute(
            text("SELECT 1 FROM events WHERE slug=:s"), {"s": slug}
        ).fetchone()
        if dup:
            raise HTTPException(409, f"slug already exists: {slug}")
        event_id = conn.execute(
            text(
                """INSERT INTO events
                   (slug, title, description, poster_url, venue, city, starts_at, ends_at, status)
                   VALUES (:slug, :title, :description, :poster_url, :venue, :city,
                           :starts_at, :ends_at, :status)
                   RETURNING id"""
            ),
            {
                "slug": slug,
                "title": body.title.strip(),
                "description": body.description,
                "poster_url": body.poster_url,
                "venue": body.venue,
                "city": body.city,
                "starts_at": body.starts_at,
                "ends_at": body.ends_at,
                "status": body.status,
            },
        ).scalar()
        return _load_event(conn, event_id)


@app.patch("/api/admin/events/{event_id}", response_model=EventOut)
def admin_edit_event(event_id: int, body: EventEditIn, request: Request):
    user = auth.require_user(request)
    allowed = {"title", "description", "poster_url", "venue", "city", "starts_at", "ends_at"}
    fields = {k: v for k, v in body.model_dump(exclude_unset=True).items() if k in allowed}
    if not fields:
        raise HTTPException(400, "No updatable fields supplied")
    if "title" in fields and fields.get("title") is not None and not fields["title"].strip():
        raise HTTPException(400, "title must not be empty")
    with get_conn() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM events WHERE id=:i"), {"i": event_id}
        ).fetchone()
        if not exists:
            raise HTTPException(404, "Event not found")
        auth.require_action(conn, user, "edit_event", event_id)
        sets = ", ".join(f"{k}=:v_{k}" for k in fields)
        params = {f"v_{k}": v for k, v in fields.items()}
        params["id"] = event_id
        conn.execute(text(f"UPDATE events SET {sets} WHERE id=:id"), params)
        return _load_event(conn, event_id)


@app.post("/api/admin/events/{event_id}/publish", response_model=EventOut)
def admin_publish_event(event_id: int, request: Request):
    return _set_event_status(event_id, "published", request)


@app.post("/api/admin/events/{event_id}/close", response_model=EventOut)
def admin_close_event(event_id: int, request: Request):
    return _set_event_status(event_id, "closed", request)


def _set_event_status(event_id: int, status: str, request: Request) -> EventOut:
    user = auth.require_user(request)
    with get_conn() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM events WHERE id=:i"), {"i": event_id}
        ).fetchone()
        if not exists:
            raise HTTPException(404, "Event not found")
        auth.require_action(conn, user, "edit_event", event_id)
        cur = conn.execute(
            text("UPDATE events SET status=:s WHERE id=:i"), {"s": status, "i": event_id}
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "Event not found")
        return _load_event(conn, event_id)


def _validate_ticket_fields(
    price_inr: Optional[int] = None,
    group_size: Optional[int] = None,
    capacity: Optional[int] = None,
) -> None:
    if price_inr is not None and price_inr < 0:
        raise HTTPException(400, "price_inr must be >= 0")
    if group_size is not None and group_size < 1:
        raise HTTPException(400, "group_size must be >= 1")
    if capacity is not None and capacity < 0:
        raise HTTPException(400, "capacity must be >= 0")


@app.post("/api/admin/events/{event_id}/ticket-types", response_model=EventOut)
def admin_create_ticket_type(event_id: int, body: TicketTypeCreateIn, request: Request):
    user = auth.require_user(request)
    if not body.name.strip():
        raise HTTPException(400, "name is required")
    _validate_ticket_fields(body.price_inr, body.group_size, body.capacity)
    with get_conn() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM events WHERE id=:i"), {"i": event_id}
        ).fetchone()
        if not exists:
            raise HTTPException(404, "Event not found")
        auth.require_action(conn, user, "manage_tickets", event_id)
        dup = conn.execute(
            text("SELECT 1 FROM ticket_types WHERE event_id=:e AND name=:n"),
            {"e": event_id, "n": body.name.strip()},
        ).fetchone()
        if dup:
            raise HTTPException(409, f"ticket type already exists: {body.name.strip()}")
        conn.execute(
            text(
                """INSERT INTO ticket_types
                   (event_id, name, price_inr, group_size, capacity, sale_open)
                   VALUES (:event_id, :name, :price_inr, :group_size, :capacity, :sale_open)"""
            ),
            {
                "event_id": event_id,
                "name": body.name.strip(),
                "price_inr": body.price_inr,
                "group_size": body.group_size,
                "capacity": body.capacity,
                "sale_open": 1 if body.sale_open else 0,
            },
        )
        return _load_event(conn, event_id)


@app.patch("/api/admin/ticket-types/{tt_id}", response_model=EventOut)
def admin_edit_ticket_type(tt_id: int, body: TicketTypeEditIn, request: Request):
    user = auth.require_user(request)
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, "No updatable fields supplied")
    if "name" in fields and not (fields.get("name") or "").strip():
        raise HTTPException(400, "name must not be empty")
    _validate_ticket_fields(
        fields.get("price_inr"),
        fields.get("group_size"),
        fields.get("capacity"),
    )
    if "sale_open" in fields:
        fields["sale_open"] = 1 if fields["sale_open"] else 0
    with get_conn() as conn:
        row = conn.execute(
            text("SELECT event_id FROM ticket_types WHERE id=:i"), {"i": tt_id}
        ).mappings().fetchone()
        if not row:
            raise HTTPException(404, "Ticket type not found")
        auth.require_action(conn, user, "manage_tickets", row["event_id"])
        sets = ", ".join(f"{k}=:v_{k}" for k in fields)
        params = {f"v_{k}": v for k, v in fields.items()}
        params["id"] = tt_id
        conn.execute(text(f"UPDATE ticket_types SET {sets} WHERE id=:id"), params)
        return _load_event(conn, row["event_id"])


# --------------------------------------------------------------- invites ----

_INVITE_ROLES = {"gate_staff", "co_host"}


def _invite_row(conn, code: str):
    return (
        conn.execute(text("SELECT * FROM invites WHERE code=:c"), {"c": code})
        .mappings()
        .fetchone()
    )


def _invite_state(inv) -> tuple[bool, str]:
    """(valid, reason). Codes are single-use with a 7-day expiry."""
    if not inv:
        return False, "unknown"
    if inv["used_by"]:
        return False, "used"
    if (inv["expires_at"] or "") <= utc_now_str():
        return False, "expired"
    return True, ""


@app.get("/api/invites/{code}", response_model=InviteStatusOut)
def invite_status(code: str):
    """Public: lets the invite landing page show valid/expired before signup."""
    with get_conn() as conn:
        inv = _invite_row(conn, code)
        valid, reason = _invite_state(inv)
        title = ""
        if inv:
            row = conn.execute(
                text("SELECT title FROM events WHERE id=:i"), {"i": inv["event_id"]}
            ).fetchone()
            title = row[0] if row else ""
        return InviteStatusOut(
            code=code,
            valid=valid,
            reason=reason,
            role=inv["role"] if inv else "",
            event_id=inv["event_id"] if inv else None,
            event_title=title,
        )


@app.post("/api/invites/{code}/accept")
def invite_accept(code: str, body: InviteAcceptIn, request: Request):
    """Single-use invite: signup (new account) or login (existing), then join
    the event as the invited role. Sets the session cookie on success."""
    ip = auth.client_ip(request)
    email = body.email.strip().lower()
    auth.check_password_strength(body.password)
    outcome = None
    payload = None
    with get_conn() as conn:
        inv = _invite_row(conn, code)
        valid, reason = _invite_state(inv)
        if not valid:
            raise HTTPException(400, f"Invite invalid or {reason}")
        try:
            auth.check_rate_limit(conn, ip, email)
        except HTTPException:
            outcome = "rate_limited"
        if outcome is None:
            user = auth.user_by_email(conn, email)
            if user:
                if not auth.verify_password(body.password, user["password_hash"]):
                    auth.record_failure(conn, ip, email)
                    outcome = "bad_credentials"
            else:
                if not body.name.strip():
                    raise HTTPException(400, "Name is required for a new account")
                uid = auth.create_user(conn, body.name, email, body.password)
                user = {"id": uid, "name": body.name.strip(), "email": email, "is_owner": False}
            if outcome is None:
                auth.clear_failures(conn, email)
                # Join the event (never downgrade an existing membership).
                conn.execute(
                    text(
                        """INSERT INTO event_members (event_id, user_id, role)
                           VALUES (:e, :u, :r)
                           ON CONFLICT (event_id, user_id) DO NOTHING"""
                    ),
                    {"e": inv["event_id"], "u": user["id"], "r": inv["role"]},
                )
                used = conn.execute(
                    text(
                        "UPDATE invites SET used_by=:u WHERE code=:c AND used_by IS NULL"
                    ),
                    {"u": user["id"], "c": code},
                ).rowcount
                if used == 0:
                    raise HTTPException(409, "Invite was just used")
                outcome = "ok"
                payload = (user, inv["event_id"], inv["role"])
    if outcome == "rate_limited":
        raise HTTPException(
            429,
            f"Too many failed attempts — try again in {auth.RATE_LIMIT_WINDOW_MIN} minutes",
        )
    if outcome == "bad_credentials":
        raise HTTPException(401, _BAD_CREDENTIALS)
    user, event_id, role = payload
    response = JSONResponse(
        {
            "ok": True,
            "user": {
                "id": user["id"],
                "name": user["name"],
                "email": user["email"],
                "is_owner": user["is_owner"],
            },
            "event_id": event_id,
            "role": role,
        }
    )
    auth.set_session_cookie(response, request, user["id"])
    return response


# ------------------------------------------------------ members & invites ----


def _require_event(conn, event_id: int) -> None:
    if not conn.execute(
        text("SELECT 1 FROM events WHERE id=:i"), {"i": event_id}
    ).fetchone():
        raise HTTPException(404, "Event not found")


def _invite_out(inv) -> InviteOut:
    return InviteOut(
        code=inv["code"],
        event_id=inv["event_id"],
        role=inv["role"],
        expires_at=inv["expires_at"],
        used_by=inv["used_by"],
    )


@app.get("/api/admin/events/{event_id}/invites", response_model=list[InviteOut])
def admin_list_invites(event_id: int, request: Request):
    user = auth.require_user(request)
    with get_conn() as conn:
        _require_event(conn, event_id)
        auth.require_action(conn, user, "invite", event_id)
        rows = conn.execute(
            text("SELECT * FROM invites WHERE event_id=:e ORDER BY expires_at DESC"),
            {"e": event_id},
        ).mappings().all()
        return [_invite_out(r) for r in rows]


@app.post("/api/admin/events/{event_id}/invites", response_model=InviteOut)
def admin_create_invite(event_id: int, body: InviteCreateIn, request: Request):
    user = auth.require_user(request)
    role = body.role.strip()
    if role not in _INVITE_ROLES:
        raise HTTPException(400, f"role must be one of {sorted(_INVITE_ROLES)}")
    with get_conn() as conn:
        _require_event(conn, event_id)
        auth.require_action(conn, user, "invite", event_id)
        if role != "gate_staff" and not user["is_owner"]:
            # A co-host may only invite gate staff.
            raise HTTPException(403, "Only the owner can invite co-hosts")
        code = auth.generate_invite_code()
        expires = auth.invite_expiry_iso()
        conn.execute(
            text(
                """INSERT INTO invites (code, event_id, role, created_by, expires_at)
                   VALUES (:c, :e, :r, :b, :x)"""
            ),
            {"c": code, "e": event_id, "r": role, "b": user["id"], "x": expires},
        )
        return InviteOut(
            code=code, event_id=event_id, role=role, expires_at=expires, used_by=None
        )


@app.get("/api/admin/events/{event_id}/members", response_model=list[EventMemberOut])
def admin_list_members(event_id: int, request: Request):
    user = auth.require_user(request)
    with get_conn() as conn:
        _require_event(conn, event_id)
        auth.require_action(conn, user, "manage_members", event_id)
        rows = conn.execute(
            text(
                """SELECT u.id, u.name, u.email, m.role
                   FROM event_members m JOIN users u ON u.id = m.user_id
                   WHERE m.event_id = :e ORDER BY u.name"""
            ),
            {"e": event_id},
        ).mappings().all()
        return [
            EventMemberOut(
                user_id=r["id"], name=r["name"], email=r["email"], role=r["role"]
            )
            for r in rows
        ]


@app.post("/api/admin/events/{event_id}/members", response_model=EventMemberOut)
def admin_add_member(event_id: int, body: MemberAddIn, request: Request):
    user = auth.require_user(request)
    role = body.role.strip()
    if role not in _INVITE_ROLES:
        raise HTTPException(400, f"role must be one of {sorted(_INVITE_ROLES)}")
    with get_conn() as conn:
        _require_event(conn, event_id)
        auth.require_action(conn, user, "manage_members", event_id)
        target = auth.user_by_email(conn, body.email)
        if not target:
            raise HTTPException(404, "No user with that email — send an invite instead")
        existing = conn.execute(
            text(
                "SELECT 1 FROM event_members WHERE event_id=:e AND user_id=:u"
            ),
            {"e": event_id, "u": target["id"]},
        ).fetchone()
        if existing:
            raise HTTPException(409, "Already a member of this event")
        conn.execute(
            text(
                "INSERT INTO event_members (event_id, user_id, role) VALUES (:e, :u, :r)"
            ),
            {"e": event_id, "u": target["id"], "r": role},
        )
        return EventMemberOut(
            user_id=target["id"],
            name=target["name"],
            email=target["email"],
            role=role,
        )


@app.patch("/api/admin/members/{event_id}/{user_id}", response_model=EventMemberOut)
def admin_change_member_role(
    event_id: int, user_id: int, body: MemberRoleIn, request: Request
):
    user = auth.require_user(request)
    role = body.role.strip()
    if role not in _INVITE_ROLES:
        raise HTTPException(400, f"role must be one of {sorted(_INVITE_ROLES)}")
    with get_conn() as conn:
        _require_event(conn, event_id)
        auth.require_action(conn, user, "manage_members", event_id)
        member = conn.execute(
            text(
                """SELECT u.id, u.name, u.email, m.role
                   FROM event_members m JOIN users u ON u.id = m.user_id
                   WHERE m.event_id = :e AND m.user_id = :u"""
            ),
            {"e": event_id, "u": user_id},
        ).mappings().fetchone()
        if not member:
            raise HTTPException(404, "Not a member of this event")
        conn.execute(
            text(
                "UPDATE event_members SET role=:r WHERE event_id=:e AND user_id=:u"
            ),
            {"r": role, "e": event_id, "u": user_id},
        )
        return EventMemberOut(
            user_id=member["id"],
            name=member["name"],
            email=member["email"],
            role=role,
        )


@app.delete("/api/admin/members/{event_id}/{user_id}")
def admin_remove_member(event_id: int, user_id: int, request: Request):
    user = auth.require_user(request)
    with get_conn() as conn:
        _require_event(conn, event_id)
        auth.require_action(conn, user, "manage_members", event_id)
        cur = conn.execute(
            text(
                "DELETE FROM event_members WHERE event_id=:e AND user_id=:u"
            ),
            {"e": event_id, "u": user_id},
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "Not a member of this event")
        return {"ok": True, "removed": user_id}


@app.delete("/api/admin/events/{event_id}")
def admin_delete_event(event_id: int, request: Request):
    """Owner-only. Refuses while guest/registration data still points at the
    event (wipe first) so rows never dangle without an authorization scope."""
    user = auth.require_user(request)
    with get_conn() as conn:
        _require_event(conn, event_id)
        auth.require_action(conn, user, "delete_event", event_id)
        guests = conn.execute(
            text("SELECT COUNT(*) FROM guests WHERE event_id=:e"), {"e": event_id}
        ).scalar() or 0
        regs = conn.execute(
            text("SELECT COUNT(*) FROM registrations WHERE event_id=:e"),
            {"e": event_id},
        ).scalar() or 0
        if guests or regs:
            raise HTTPException(
                409,
                f"Event still has {guests} guests / {regs} registrations — "
                "wipe the data before deleting the event",
            )
        conn.execute(text("DELETE FROM ticket_types WHERE event_id=:e"), {"e": event_id})
        conn.execute(text("DELETE FROM event_members WHERE event_id=:e"), {"e": event_id})
        conn.execute(text("DELETE FROM invites WHERE event_id=:e"), {"e": event_id})
        conn.execute(text("DELETE FROM events WHERE id=:i"), {"i": event_id})
        return {"ok": True, "deleted": event_id}
