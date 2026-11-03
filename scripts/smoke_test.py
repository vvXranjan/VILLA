"""Functional smoke tests for VILLA check-in app (Phase 2: auth + roles).

Run:  .venv/bin/python -u scripts/smoke_test.py [base_url] [expected_db]

Order (per Phase 2 plan):
  public checks → no-cookie 401 → wrong-password 401 → owner login
  (clears rate-limit counters) → the original 61 checks with the owner
  session → logout / re-login → role matrix (seeded users) → invite flow
  → rate limit LAST + login_attempts cleanup.

Safety guard: refuses to run when DATABASE_URL is set in the environment, or
when a configured URL targets anything other than 127.0.0.1/localhost — this
script must never touch the live Supabase database. Run it only against a
local sandbox server (see CLAUDE_CONTEXT.md).
"""
import csv
import io
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


def _refuse(msg: str) -> None:
    print(f"REFUSING TO RUN: {msg}")
    sys.exit(2)


_db_url = os.environ.get("DATABASE_URL", "").strip()
if _db_url:
    _netloc = re.sub(r"^[a-z0-9+.-]+://", "", _db_url).split("/")[0]
    _host = _netloc.rsplit("@", 1)[-1].split(":")[0]
    if _host not in {"127.0.0.1", "localhost", "::1"}:
        _refuse(f"target host {_host!r} is not 127.0.0.1/localhost")
    _refuse(
        "DATABASE_URL is set in this environment — unset it; smoke_test only "
        "runs against a local sandbox server."
    )

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8001"
EXPECTED_DB = sys.argv[2] if len(sys.argv) > 2 else "sqlite"
TEST_PASSWORD = os.environ.get("VILLA_TEST_PASSWORD", "TestPass123!")
OWNER_EMAIL = os.environ.get("VILLA_OWNER_EMAIL", "owner@sandbox.test")
PASS = FAIL = 0

COOKIES: dict = {}


def _store_cookies(headers) -> None:
    for sc in headers.get_all("Set-Cookie") or []:
        pair = sc.split(";", 1)[0]
        k, _, v = pair.partition("=")
        k, v = k.strip(), v.strip().strip('"')
        if v:
            COOKIES[k] = v
        else:
            COOKIES.pop(k, None)


def req(path, method="GET", data=None, headers=None, raw=False, cookies=True):
    url = BASE + path
    body = data
    hdrs = dict(headers or {})
    if isinstance(data, dict):
        body = json.dumps(data).encode()
        hdrs.setdefault("Content-Type", "application/json")
    if cookies and COOKIES:
        hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in COOKIES.items())
    r = urllib.request.Request(url, data=body, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            payload = resp.read()
            _store_cookies(resp.headers)
            return resp.status, payload if raw else json.loads(payload)
    except urllib.error.HTTPError as e:
        payload = e.read()
        _store_cookies(e.headers)
        try:
            return e.code, json.loads(payload)
        except Exception:
            return e.code, payload


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def login_as(email, password):
    return req(
        "/api/login",
        "POST",
        data={"email": email, "password": password},
        cookies=False,
    )


def multipart(path, fields, files, cookies=True):
    boundary = uuid.uuid4().hex
    buf = io.BytesIO()
    for k, v in fields.items():
        buf.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    for k, (fn, content) in files.items():
        buf.write(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fn}\"\r\n"
            f"Content-Type: text/csv\r\n\r\n".encode()
        )
        buf.write(content if isinstance(content, bytes) else content.encode())
        buf.write(b"\r\n")
    buf.write(f"--{boundary}--\r\n".encode())
    return req(
        path,
        "POST",
        data=buf.getvalue(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        cookies=cookies,
    )


print(f"== VILLA functional tests against {BASE} ==")

# ---------------------------------------------------- Phase 2: public ----
print("\n-- public endpoints (no session) --")
st, body = req("/", raw=True)
check("GET / returns HTML", st == 200 and b"VILLA" in body)
st, body = req("/static/app.js", raw=True)
check("GET /static/app.js", st == 200 and b"function" in body)
st, body = req("/static/style.css", raw=True)
check("GET /static/style.css", st == 200 and b"{" in body)
st, h = req("/api/health")
check("GET /api/health", st == 200 and h.get("ok") is True and h.get("db") == EXPECTED_DB, h)
st, me = req("/api/me")
check("GET /api/me public → user null", st == 200 and me.get("user") is None, me)
st, inv = req("/api/invites/not-a-real-code")
check(
    "GET /api/invites unknown → valid false",
    st == 200 and inv.get("valid") is False and inv.get("reason") == "unknown",
    inv,
)

# ------------------------------------------------- Phase 2: default-deny ----
print("\n-- no session → 401 --")
st, b = req("/api/stats", cookies=False)
check("stats no cookie 401", st == 401 and b.get("detail") == "Not authenticated", b)
st, b = req("/api/export.csv", cookies=False)
check("export no cookie 401", st == 401, b)
st, b = req("/api/guests", cookies=False)
check("guests no cookie 401", st == 401, b)
st, b = req("/api/admin/events", cookies=False)
check("admin events no cookie 401", st == 401, b)
st, b = req("/api/guests/1/checkin", "POST", cookies=False)
check("checkin no cookie 401", st == 401, b)

print("\n-- wrong credentials → 401 (same body) --")
st, b1 = req(
    "/api/login",
    "POST",
    data={"email": OWNER_EMAIL, "password": "definitely-wrong-pw"},
    cookies=False,
)
st2, b2 = req(
    "/api/login",
    "POST",
    data={"email": "ghost-user-does-not-exist@sandbox.test", "password": "definitely-wrong-pw"},
    cookies=False,
)
check(
    "wrong password 401 == unknown email 401 (no oracle)",
    st == 401 and st2 == 401 and b1 == b2 and b1.get("detail") == "Invalid email or password",
    (b1, b2),
)

# ------------------------------------------------- Phase 2: owner login ----
print("\n-- owner login (clears rate-limit counters) --")
st, b = login_as(OWNER_EMAIL, TEST_PASSWORD)
check("owner login 200", st == 200 and b.get("ok") is True and b.get("user", {}).get("is_owner") is True, b)
check("session cookie set", "villa_session" in COOKIES)
st, me = req("/api/me")
check(
    "/api/me shows owner",
    st == 200 and me.get("user") and me["user"]["email"] == OWNER_EMAIL and me["user"]["is_owner"] is True,
    me,
)
check(
    "/api/me memberships is a list",
    st == 200 and isinstance(me.get("user", {}).get("memberships"), list),
    me,
)
check("me never returns password_hash", "password_hash" not in json.dumps(me), me)

# ================================================ original 61 checks ========
# (unchanged Phase 0/1 body — now runs with the owner session cookie)

# 1-3: static + health already checked above; stats baseline next
# 4: stats baseline
st, s = req("/api/stats")
check("stats baseline", st == 200 and s["registrations"] == 19 and s["total"] == 22
      and s["paid"] == 20 and s["unpaid"] == 2 and s["expected_capacity"] == 35
      and s["incomplete_groups"] == 7, s)

# 5: guest list
st, guests = req("/api/guests")
check("guests list (no q)", st == 200 and len(guests) == 22)

# 6: searches
st, g1 = req("/api/guests?q=" + urllib.parse.quote(guests[0]["name"].lower()))
check("guest search case-insensitive", st == 200 and any(g["id"] == guests[0]["id"] for g in g1), g1[:2])
st, g2 = req(f"/api/guests?q=REG-018")
check("guest search by reg_id", st == 200 and len(g2) == 1 and g2[0]["reg_id"] == "REG-018", g2)

# 7: registrations grouped
st, regs = req("/api/registrations")
r18 = next((r for r in regs if r["reg_id"] == "REG-018"), None)
check("registrations grouped", st == 200 and len(regs) == 19)
check("REG-018 incomplete trio 1/3", r18 and r18["named_count"] == 1 and r18["expected_capacity"] == 3
      and r18["needs_review"] is True, r18)

# 8: registration search
st, rs = req("/api/registrations?q=REG-019")
check("registration search", st == 200 and len(rs) == 1 and rs[0]["reg_id"] == "REG-019", rs)

# 9: 404s
st, _ = req("/api/guests/999999")
check("GET guest 404", st == 404)
st, _ = req("/api/qr/notarealtoken")
check("QR invalid token 404", st == 404)

# 10: unpaid blocked
unpaid = next(g for g in guests if g["payment_status"] == "UNPAID")
st, res = req(f"/api/guests/{unpaid['id']}/checkin", "POST")
check("check-in UNPAID blocked", st == 200 and res["ok"] is False and "PAYMENT NOT VERIFIED" in res["message"], res)

# 11-14: paid check-in flow
paid = next(g for g in guests if g["payment_status"] == "PAID" and not g["checked_in_at"])
st, res = req(f"/api/guests/{paid['id']}/checkin", "POST")
check("check-in PAID ok", st == 200 and res["ok"] is True and res["message"].startswith("CHECKED IN — welcome"), res)
ts = res["guest"]["checked_in_at"]
check("checked_in_at timestamp format", bool(ts) and len(ts) == 19 and ts[4] == "-", ts)
st, res2 = req(f"/api/guests/{paid['id']}/checkin", "POST")
check("double check-in blocked", res2["ok"] is False and "ALREADY CHECKED IN" in res2["message"]
      and ts in res2["message"], res2)
st, s = req("/api/stats")
check("stats after check-in", s["checked_in"] == 1 and s["paid_remaining"] == 19, s)
st, arr = req("/api/list/arrived")
st, pend = req("/api/list/pending")
check("arrived=1 pending=21", len(arr) == 1 and len(pend) == 21, (len(arr), len(pend)))

# 15: undo
st, res = req(f"/api/guests/{paid['id']}/undo", "POST")
check("undo ok", st == 200 and res["ok"] is True and res["guest"]["checked_in_at"] is None, res)
st, s = req("/api/stats")
check("stats after undo", s["checked_in"] == 0 and s["paid_remaining"] == 20, s)

# 16: manual review list
st, mr = req("/api/list/manual_review")
check("manual_review 7 groups", st == 200 and len(mr) == 7
      and any(r["reg_id"] == "REG-018" and "INCOMPLETE GROUP DETAILS" in r["review_reason"] for r in mr),
      [r["reg_id"] for r in mr])

# 17: export CSV
st, csv_text = req("/api/export.csv", raw=True)
rows = list(csv.reader(io.StringIO(csv_text.decode())))
check("export header 10 cols", rows[0] == ["reg_id", "name", "entry_pass", "phone", "email",
      "payment_status", "check_in_status", "checked_in_at", "added_by", "incomplete_group"], rows[0])
check("export 22 guest rows", len(rows) == 23)
check("export has no aadhaar", "aadhaar" not in csv_text.decode().lower())

# 18: QR lookup — valid token read from source SQLite (tokens migrated as-is)
import sqlite3 as _sq
_src = _sq.connect(f"file:{Path(__file__).resolve().parent.parent / 'data' / 'party.db'}?mode=ro", uri=True)
_tok = _src.execute("SELECT qr_token FROM guests ORDER BY id LIMIT 1").fetchone()[0]
_src.close()
st, qg = req(f"/api/qr/{_tok}")
check("QR valid token lookup", st == 200 and bool(qg.get("name")), qg)

# 19-22: add_companion on REG-018 (UNPAID trio, 1/3)
st, res = req("/api/admin/add_companion?" + urllib.parse.urlencode(
    {"reg_id": "REG-018", "name": "Test Companion One, Test Companion Two"}), "POST")
check("add_companion REG-018 +2", st == 200 and res.get("ok") is True
      and res.get("added") == ["Test Companion One", "Test Companion Two"]
      and res["registration"]["named_count"] == 3
      and res["registration"]["needs_review"] is False, res)
st, res = req("/api/admin/add_companion?" + urllib.parse.urlencode({"reg_id": "REG-018", "name": "Fourth Person"}), "POST")
check("add_companion capacity full 400", st == 400 and "capacity full" in res.get("detail", ""), res)
st, res = req("/api/admin/add_companion?" + urllib.parse.urlencode({"reg_id": "REG-018", "name": "Test Companion One"}), "POST")
check("add_companion duplicate 400", st == 400 and "already present" in res.get("detail", ""), res)
st, res = req("/api/admin/add_companion?" + urllib.parse.urlencode({"reg_id": "REG-999", "name": "X"}), "POST")
check("add_companion unknown reg 404", st == 404, res)

# companions inherited UNPAID → still blocked at gate
st, g18 = req("/api/guests?q=" + urllib.parse.quote("Test Companion One"))
comp = g18[0]
check("companion added_by=admin, UNPAID", comp["added_by"] == "admin" and comp["payment_status"] == "UNPAID", comp)
st, res = req(f"/api/guests/{comp['id']}/checkin", "POST")
check("companion UNPAID check-in blocked", res["ok"] is False and "PAYMENT NOT VERIFIED" in res["message"], res)

# 23-24: CSV import with Aadhaar column
test_csv = (
    "Timestamp,Entry Pass,Name of the guests,Whatsapp number,Email id,Paid or not,Aadhaar Number\n"
    "2026-10-01,Solo,Import Test Solo,9876543210,solo@test.com,paid,1234 5678 9012\n"
    "2026-10-01,Trio,\"Import A, Import B\",9811111111,trio@test.com,not paid,9999 8888 7777\n"
)
st, prev = multipart("/api/import/preview", {}, {"file": ("test.csv", test_csv)})
check("import preview ok", st == 200 and prev["registration_rows"] == 2 and prev["individual_guests"] == 3
      and prev["solo"] == 1 and prev["trio"] == 1
      and prev["aadhaar_columns_ignored"] == ["Aadhaar Number"], prev)
st, conf = req(f"/api/import/confirm?token={prev['token']}", "POST")
check("import confirm ok", st == 200 and conf["ok"] is True and conf["registrations"] == 2
      and conf["guests"] == 3 and conf["backup_file"], conf)
st, prev2 = multipart("/api/import/preview", {}, {"file": ("test.csv", test_csv)})
check("re-import duplicate warning", prev2["duplicate_file_warning"] and "already imported" in prev2["duplicate_file_warning"], prev2)
st, conf2 = req(f"/api/import/confirm?token={prev2['token']}", "POST")
check("re-import updates not duplicates", conf2["guests"] == 0 and conf2["updated"] == 3, conf2)
st, res = req(f"/api/import/confirm?token={prev2['token']}", "POST")
check("token reuse 400", st == 400, res)
st, s = req("/api/stats")
# 22 base + 2 admin companions + 3 imported = 27 guests; test CSV reg_ids
# REG-001/REG-002 collide with existing rows (positional IDs) → upsert, regs stay 19
check("stats after test import", s["total"] == 27 and s["registrations"] == 19
      and s["paid"] == 21 and s["unpaid"] == 6, s)

# 25: cleanup check-ins + verify reset_all
st, res = req(f"/api/guests/{paid['id']}/checkin", "POST")
st, res = req("/api/admin/reset_all", "POST")
check("reset_all", st == 200 and res["reset"] >= 1, res)
st, s = req("/api/stats")
check("stats clean", s["checked_in"] == 0, s)

# ---- Phase 1: events model ----
st, evs = req("/api/admin/events")
check("admin list events", st == 200 and isinstance(evs, list) and len(evs) >= 1, evs)
villa = next((e for e in evs if e["slug"] == "villa-diwali-bling"), None)
check(
    "VILLA event seeded as draft",
    villa is not None and villa["status"] == "draft"
    and villa["title"] == "VILLA — Desi Diwali Bling",
    villa,
)
check("VILLA event has no ticket types yet", villa is not None and villa["ticket_types"] == [], villa)
villa_id = villa["id"] if villa else -1

st, g = req("/api/guests?q=REG-018")
check(
    "guests back-filled with event_id",
    st == 200 and len(g) >= 1 and all(x["event_id"] == villa_id for x in g),
    g,
)
st, rs = req("/api/registrations?q=REG-018")
check("registrations back-filled with event_id", st == 200 and len(rs) == 1 and rs[0]["event_id"] == villa_id, rs)
st, ig = req("/api/guests?q=" + urllib.parse.quote("Import Test Solo"))
check("imported guest linked to VILLA event", st == 200 and len(ig) == 1 and ig[0]["event_id"] == villa_id, ig)
st, cg = req("/api/guests?q=" + urllib.parse.quote("Test Companion One"))
check("companion inherits registration event_id", st == 200 and len(cg) == 1 and cg[0]["event_id"] == villa_id, cg)

new_ev_body = {
    "title": "Test Diwali Night",
    "venue": "Community Hall",
    "city": "Pune",
    "starts_at": "2026-12-04 19:00",
    "ends_at": "2026-12-04 23:30",
}
st, ev = req("/api/admin/events", "POST", data=new_ev_body)
check(
    "create event (draft, auto slug)",
    st == 200 and ev["status"] == "draft" and ev["slug"] == "test-diwali-night" and ev["id"] > 0,
    ev,
)

st, ev2 = req("/api/admin/events", "POST", data=new_ev_body)
check("duplicate slug rejected", st == 409, ev2)

st, ev3 = req("/api/admin/events", "POST", data={"title": "  "})
check("empty title rejected", st == 400, ev3)

st, ev4 = req(f"/api/admin/events/{ev['id']}", "PATCH", data={"venue": "Rooftop", "description": "evening party"})
check(
    "edit event",
    st == 200 and ev4["venue"] == "Rooftop" and ev4["description"] == "evening party"
    and ev4["title"] == "Test Diwali Night",
    ev4,
)

st, ev5 = req(f"/api/admin/events/{ev['id']}/publish", "POST")
check("publish event", st == 200 and ev5["status"] == "published", ev5)
st, ev6 = req(f"/api/admin/events/{ev['id']}/close", "POST")
check("close event", st == 200 and ev6["status"] == "closed", ev6)

st, err = req("/api/admin/events", "POST", data={"title": "Bad Status", "status": "archived"})
check("invalid status rejected", st == 400, err)

st, err = req("/api/admin/events/999999/publish", "POST")
check("publish missing event 404", st == 404, err)

st, tt = req(
    f"/api/admin/events/{ev['id']}/ticket-types",
    "POST",
    data={"name": "Couple", "price_inr": 999, "group_size": 2, "capacity": 40},
)
check(
    "create ticket type",
    st == 200 and any(
        t["name"] == "Couple" and t["price_inr"] == 999 and t["capacity"] == 40
        and t["sold"] == 0 and t["sale_open"] is True
        for t in tt["ticket_types"]
    ),
    tt,
)

st, tt_dup = req(
    f"/api/admin/events/{ev['id']}/ticket-types", "POST", data={"name": "Couple", "price_inr": 100}
)
check("duplicate ticket type rejected", st == 409, tt_dup)

tt_id = next(t["id"] for t in tt["ticket_types"] if t["name"] == "Couple")
st, tt2 = req(
    f"/api/admin/ticket-types/{tt_id}", "PATCH", data={"price_inr": 1499, "sale_open": False}
)
check(
    "edit ticket type",
    st == 200 and any(
        t["id"] == tt_id and t["price_inr"] == 1499 and t["sale_open"] is False
        for t in tt2["ticket_types"]
    ),
    tt2,
)

st, err = req(
    f"/api/admin/events/{ev['id']}/ticket-types", "POST", data={"name": "VIP", "price_inr": -5}
)
check("negative price rejected", st == 400, err)

st, err = req("/api/admin/ticket-types/999999", "PATCH", data={"price_inr": 100})
check("edit missing ticket type 404", st == 404, err)

st, s = req("/api/stats")
check(
    "stats unchanged by event work",
    s["registrations"] == 19 and s["total"] == 27 and s["checked_in"] == 0,
    s,
)

# ============================================ Phase 2: logout / re-login ====
print("\n-- logout / re-login --")
st, b = req("/api/logout", "POST")
check("logout 200", st == 200 and b.get("ok") is True, b)
check("session cookie cleared", "villa_session" not in COOKIES, COOKIES)
st, b = req("/api/stats")
check("stats after logout 401", st == 401, b)
st, b = login_as(OWNER_EMAIL, TEST_PASSWORD)
check("re-login 200", st == 200 and b.get("ok") is True, b)
st, s = req("/api/stats")
check("stats after re-login 200", st == 200, s)

# ================================================ Phase 2: role matrix ======
print("\n-- role matrix (seeded users; see scripts/seed_test_users.py) --")

# gate_staff
st, b = login_as("gate@sandbox.test", TEST_PASSWORD)
check("gate login", st == 200, b)
st, s = req("/api/stats")
check("gate stats 200", st == 200, s)
st, gl = req("/api/guests?q=REG-001")
check("gate search 200", st == 200, gl)
st, arr = req("/api/list/arrived")
check("gate arrived list 200", st == 200, arr)
st, res = req(f"/api/guests/{unpaid['id']}/checkin", "POST")
check("gate checkin permission OK (business rule blocks unpaid)", st == 200 and res["ok"] is False, res)
st, paid_g = req(f"/api/guests/{paid['id']}/checkin", "POST")
check("gate can check in a paid guest", st == 200 and paid_g["ok"] is True, paid_g)
st, res = req(f"/api/guests/{paid['id']}/undo", "POST")
check("gate can undo", st == 200 and res["ok"] is True, res)
st, b = req("/api/export.csv", raw=True)
check("gate export 403", st == 403, b)
st, b = req("/api/admin/reset_all", "POST")
check("gate reset_all 403", st == 403, b)
st, b = req("/api/admin/wipe_guests", "POST")
check("gate wipe 403", st == 403, b)
st, b = req("/api/admin/events", "POST", data={"title": "Gate Hack"})
check("gate create event 403", st == 403, b)
st, b = multipart("/api/import/preview", {}, {"file": ("g.csv", "Timestamp,Name\n2026-10-01,X\n")})
check("gate import preview 403", st == 403, b)
st, b = req("/api/admin/add_companion?" + urllib.parse.urlencode({"reg_id": "REG-018", "name": "Gate Person"}), "POST")
check("gate add_companion 403", st == 403, b)
st, b = req(f"/api/admin/events/{villa_id}/invites", "POST", data={"role": "gate_staff"})
check("gate create invite 403", st == 403, b)
st, b = req(f"/api/admin/events/{villa_id}/members")
check("gate list members 403", st == 403, b)
st, b = req(f"/api/admin/events/{villa_id}", "PATCH", data={"venue": "Hacked"})
check("gate edit event 403", st == 403, b)

# co_host
st, b = login_as("cohost@sandbox.test", TEST_PASSWORD)
check("cohost login", st == 200, b)
st, s = req("/api/stats")
check("cohost stats 200", st == 200, s)
st, b = req("/api/export.csv", raw=True)
check("cohost export 200", st == 200, b)
st, prev = multipart("/api/import/preview", {}, {"file": ("c.csv", "Timestamp,Name\n2026-10-01,Y\n")})
check("cohost import preview 200", st == 200 and prev.get("registration_rows") == 1, prev)
st, b = req(f"/api/admin/events/{villa_id}/invites", "POST", data={"role": "gate_staff"})
check("cohost invite gate_staff 200", st == 200 and b.get("code"), b)
st, b = req(f"/api/admin/events/{villa_id}/invites", "POST", data={"role": "co_host"})
check("cohost invite co_host 403 (owner only)", st == 403, b)
st, b = req(f"/api/admin/events/{villa_id}", "PATCH", data={"description": "cohost edit ok"})
check("cohost edit event 200", st == 200 and b.get("description") == "cohost edit ok", b)
st, b = req("/api/admin/events", "POST", data={"title": "Cohost Hack"})
check("cohost create event 403", st == 403, b)
st, b = req("/api/admin/wipe_guests", "POST")
check("cohost wipe 403", st == 403, b)
st, b = req(f"/api/admin/events/{villa_id}/ticket-types", "POST", data={"name": "Hacker", "price_inr": 1})
check("cohost ticket type 403 (owner only)", st == 403, b)
st, b = req(f"/api/admin/events/{villa_id}/members")
check("cohost list members 403 (owner only)", st == 403, b)
st, b = req(f"/api/admin/events/{villa_id}", "DELETE")
check("cohost delete event 403", st == 403, b)

# no membership at all
st, b = login_as("nobody@sandbox.test", TEST_PASSWORD)
check("non-member login", st == 200, b)
st, b = req("/api/stats")
check("non-member stats 403", st == 403, b)
st, b = req("/api/guests")
check("non-member guests 403", st == 403, b)
st, b = req("/api/export.csv", raw=True)
check("non-member export 403", st == 403, b)
st, evs = req("/api/admin/events")
check("non-member events list empty", st == 200 and evs == [], evs)

# owner-only extras
st, b = login_as(OWNER_EMAIL, TEST_PASSWORD)
check("owner re-login for owner-only checks", st == 200, b)
st, mem = req(f"/api/admin/events/{villa_id}/members")
check(
    "owner list members 200",
    st == 200 and any(m["email"] == "cohost@sandbox.test" and m["role"] == "co_host" for m in mem)
    and any(m["email"] == "gate@sandbox.test" and m["role"] == "gate_staff" for m in mem),
    mem,
)
st, tmp = req("/api/admin/events", "POST", data={"title": "Temp Delete Event"})
check("owner creates temp event", st == 200 and tmp.get("id"), tmp)
st, b = req(f"/api/admin/events/{tmp['id']}", "DELETE")
check("owner deletes empty event 200", st == 200 and b.get("ok") is True, b)
st, b = req(f"/api/admin/events/{villa_id}", "DELETE")
check("delete event with guests 409", st == 409, b)

# ================================================ Phase 2: invite flow ======
print("\n-- invite flow --")
st, inv = req(f"/api/admin/events/{villa_id}/invites", "POST", data={"role": "gate_staff"})
check("owner creates invite", st == 200 and inv.get("code") and inv.get("role") == "gate_staff", inv)
code = inv.get("code", "")
st, stt = req(f"/api/invites/{code}", cookies=False)
check("invite status public valid", st == 200 and stt.get("valid") is True and stt.get("event_title"), stt)

invitee_email = f"invitee-{uuid.uuid4().hex[:8]}@sandbox.test"
st, acc = req(
    f"/api/invites/{code}/accept",
    "POST",
    data={"email": invitee_email, "password": TEST_PASSWORD, "name": "Invitee One"},
    cookies=False,
)
check(
    "accept invite (signup + join + cookie)",
    st == 200 and acc.get("ok") is True and acc.get("role") == "gate_staff" and "villa_session" in COOKIES,
    acc,
)
st, s = req("/api/stats")
check("invitee (gate) stats 200", st == 200, s)
st, b = req("/api/export.csv", raw=True)
check("invitee (gate) export 403", st == 403, b)

st, reuse = req(
    f"/api/invites/{code}/accept",
    "POST",
    data={"email": f"second-{uuid.uuid4().hex[:8]}@sandbox.test", "password": TEST_PASSWORD, "name": "Second"},
    cookies=False,
)
check("invite reuse 400", st == 400, reuse)

st, exp = req("/api/invites/expired-test-invite", cookies=False)
check(
    "expired invite status",
    st == 200 and exp.get("valid") is False and exp.get("reason") == "expired",
    exp,
)
st, expa = req(
    "/api/invites/expired-test-invite/accept",
    "POST",
    data={"email": f"expired-{uuid.uuid4().hex[:8]}@sandbox.test", "password": TEST_PASSWORD, "name": "Late"},
    cookies=False,
)
check("expired invite accept 400", st == 400, expa)

# the invitee's cookie is in the jar now — switch back to the owner session
st, b = login_as(OWNER_EMAIL, TEST_PASSWORD)
check("owner session restored after invitee", st == 200, b)
st, ci = req(f"/api/admin/events/{villa_id}/invites", "POST", data={"role": "gate_staff"})
check("owner invites after flow 200", st == 200, ci)
st, mem = req(f"/api/admin/events/{villa_id}/members")
check(
    "invitee appears in member list",
    st == 200 and any(m["email"] == invitee_email and m["role"] == "gate_staff" for m in mem),
    mem,
)

# ============================================ cross-event isolation =========
print("\n-- cross-event isolation (co_host of A vs B) --")
st, evA = req("/api/admin/events", "POST", data={"title": "Cross Event A", "slug": "cross-event-a"})
check("create cross event A", st == 200 and evA.get("id"), evA)
st, evB = req("/api/admin/events", "POST", data={"title": "Cross Event B", "slug": "cross-event-b"})
check("create cross event B", st == 200 and evB.get("id"), evB)
idA, idB = evA["id"], evB["id"]

st, inv = req(f"/api/admin/events/{idA}/invites", "POST", data={"role": "co_host"})
check("owner invites co_host for A", st == 200 and inv.get("code"), inv)
xa_email = f"xa-cohost-{uuid.uuid4().hex[:8]}@sandbox.test"
st, acc = req(
    f"/api/invites/{inv['code']}/accept",
    "POST",
    data={"email": xa_email, "password": TEST_PASSWORD, "name": "XA Cohost"},
    cookies=False,
)
check("accept co_host invite for A (cookie → xa cohost)", st == 200 and acc.get("ok") is True, acc)

# Seed one guest+registration each in A and B (direct SQLite — no API creates
# arbitrary-event rows; sandbox only, guarded to SQLite by the DATABASE_URL
# check at the top of this file).
_db = Path(__file__).resolve().parent.parent / "data" / "party.db"
_con = _sq.connect(_db, timeout=10)
_ts = "2026-11-03 00:00:00"
for _reg, _name, _eid in [
    ("CE-A-001", "CrossEvent A Guest", idA),
    ("CE-B-001", "CrossEvent B Guest", idB),
]:
    _con.execute(
        "INSERT INTO registrations (reg_id, entry_pass, phone, email, payment_status,"
        " needs_review, review_reason, expected_capacity, event_id, created_at)"
        " VALUES (?, 'Solo', '', '', 'PAID', 0, '', 1, ?, ?)"
        " ON CONFLICT(reg_id) DO NOTHING",
        (_reg, _eid, _ts),
    )
    _con.execute(
        "INSERT INTO guests (reg_id, name, phone, email, payment_status, entry_pass,"
        " needs_review, review_reason, added_by, event_id, created_at)"
        " VALUES (?, ?, '', '', 'PAID', 'Solo', 0, '', 'csv', ?, ?)"
        " ON CONFLICT(reg_id, name) DO NOTHING",
        (_reg, _name, _eid, _ts),
    )
_con.commit()
b_guest_id = _con.execute("SELECT id FROM guests WHERE reg_id='CE-B-001'").fetchone()[0]
_con.close()

st, me_xa = req("/api/me")
check(
    "xa cohost /api/me lists A membership",
    st == 200
    and any(m["event_id"] == idA and m["role"] == "co_host" for m in me_xa["user"].get("memberships", [])),
    me_xa,
)
st, gl = req("/api/guests")
_names = [g["name"] for g in gl]
check("A cohost search shows A guest", st == 200 and "CrossEvent A Guest" in _names, _names)
check("A cohost search hides B guest", "CrossEvent B Guest" not in _names, _names)
st, regs = req("/api/registrations")
_regids = [r["reg_id"] for r in regs]
check("A cohost registrations include CE-A", "CE-A-001" in _regids, _regids)
check("A cohost registrations exclude CE-B", "CE-B-001" not in _regids, _regids)
st, s = req("/api/stats")
check("A cohost stats scoped to A only", st == 200 and s["total"] == 1 and s["registrations"] == 1, s)
st, csvb = req("/api/export.csv", raw=True)
_txt = csvb.decode()
check(
    "A cohost export has A not B",
    "CrossEvent A Guest" in _txt and "CrossEvent B Guest" not in _txt,
    _txt[:200],
)
st, pend = req("/api/list/pending")
check(
    "A cohost pending list scoped to A",
    st == 200 and [g["name"] for g in pend] == ["CrossEvent A Guest"],
    pend,
)
st, res = req(f"/api/guests/{b_guest_id}/checkin", "POST")
check("A cohost checkin on B guest 403", st == 403, res)
st, prevA = multipart(f"/api/import/preview?event_id={idA}", {}, {"file": ("xa.csv", "Timestamp,Name\n2026-10-01,CEImportA\n")})
check("A cohost import preview into A 200", st == 200 and prevA.get("registration_rows") == 1, prevA)
st, prevB = multipart(f"/api/import/preview?event_id={idB}", {}, {"file": ("xb.csv", "Timestamp,Name\n2026-10-01,CEImportB\n")})
check("A cohost import preview into B 403", st == 403, prevB)

# ================================================ route sweep ===============
# Every /api route (with path params filled) must return 401 without a cookie,
# unless it is on the public allowlist. Catches any route we forgot to protect.
print("\n-- route sweep: every /api route without a cookie → 401 --")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.main import app  # noqa: E402
from app import auth as _auth  # noqa: E402


def _fill_path(path: str) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: ("1" if "id" in m.group(1) else "abc"), path)


swept = 0
for _route in app.routes:
    _path = getattr(_route, "path", "")
    if not _path.startswith("/api/") or _auth.is_public(_path):
        continue
    _methods = {
        m for m in (getattr(_route, "methods", None) or [])
        if m in ("GET", "POST", "PATCH", "DELETE")
    }
    if not _methods:
        continue
    _concrete = _fill_path(_path)
    for _method in _methods:
        _st, _body = req(_concrete, _method, cookies=False)
        swept += 1
        check(f"sweep {_method} {_concrete} → 401", _st == 401, f"{_st} {_body}")
check("route sweep covered 30+ method calls", swept >= 30, swept)

# ================================================ Phase 2: rate limit =======
# MUST be last: it intentionally locks an ip:/email: key.
print("\n-- rate limit (LAST) --")
st, b = login_as(OWNER_EMAIL, TEST_PASSWORD)
check("pre-rate owner login (clears owner email key)", st == 200, b)

# Use a dedicated forwarded IP so the ip: bucket starts clean — earlier failed
# logins (wrong-credentials section) already put failures on ip:127.0.0.1, and
# clear_failures now clears only the email: key by design.
rl_email = f"ratelimit-{uuid.uuid4().hex[:8]}@sandbox.test"
rl_ip = f"192.0.2.{uuid.uuid4().int % 250 + 1}"
codes = []
for _ in range(5):
    st, b = req(
        "/api/login",
        "POST",
        data={"email": rl_email, "password": "wrong-password-attempt"},
        headers={"X-Forwarded-For": rl_ip},
        cookies=False,
    )
    codes.append(st)
check("5 failed logins → 401 each", codes == [401] * 5, codes)
st, b = req(
    "/api/login",
    "POST",
    data={"email": rl_email, "password": "wrong-password-attempt"},
    headers={"X-Forwarded-For": rl_ip},
    cookies=False,
)
check("6th failed login → 429", st == 429 and "Too many failed attempts" in str(b.get("detail", "")), (st, b))

# key isolation: a different forwarded IP + a different email still logs the owner in
st, b = req(
    "/api/login",
    "POST",
    data={"email": OWNER_EMAIL, "password": TEST_PASSWORD},
    headers={"X-Forwarded-For": "198.51.100.7"},
    cookies=False,
)
check("owner unaffected by locked ip/email keys", st == 200, b)

# login_attempts cleanup (local sandbox only — DATABASE_URL guard above
# already ensures we are never pointed at a live database)
if EXPECTED_DB == "sqlite":
    db_file = Path(__file__).resolve().parent.parent / "data" / "party.db"
    if db_file.exists():
        con = _sq.connect(db_file)
        n = con.execute("DELETE FROM login_attempts").rowcount
        con.commit()
        con.close()
        print(f"  cleaned login_attempts ({n} rows)")
else:
    print("  warning: non-sqlite backend — login_attempts left in place")

print(f"\n== {PASS} passed, {FAIL} failed ==")
sys.exit(1 if FAIL else 0)
