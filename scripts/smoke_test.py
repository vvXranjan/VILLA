"""Functional smoke tests for VILLA check-in app (Postgres or SQLite backend).

Run: .venv/bin/python -u scripts/smoke_test.py [base_url] [expected_db]
"""
import csv
import io
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8001"
EXPECTED_DB = sys.argv[2] if len(sys.argv) > 2 else "postgresql"
PASS = FAIL = 0


def req(path, method="GET", data=None, headers=None, raw=False):
    url = BASE + path
    body = data
    hdrs = headers or {}
    if isinstance(data, dict):
        body = json.dumps(data).encode()
        hdrs.setdefault("Content-Type", "application/json")
    r = urllib.request.Request(url, data=body, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            payload = resp.read()
            return resp.status, payload if raw else json.loads(payload)
    except urllib.error.HTTPError as e:
        payload = e.read()
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


def multipart(path, fields, files):
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
    return req(path, "POST", data=buf.getvalue(), headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})


print(f"== VILLA functional tests against {BASE} ==")

# 1-3: static + health
st, body = req("/", raw=True)
check("GET / returns HTML", st == 200 and b"VILLA" in body)
st, body = req("/static/app.js", raw=True)
check("GET /static/app.js", st == 200 and b"function" in body)
st, body = req("/static/style.css", raw=True)
check("GET /static/style.css", st == 200 and b"{" in body)
st, h = req("/api/health")
check("GET /api/health", st == 200 and h.get("ok") is True and h.get("db") == EXPECTED_DB, h)

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
from pathlib import Path as _P
_src = _sq.connect(f"file:{_P(__file__).resolve().parent.parent / 'data' / 'party.db'}?mode=ro", uri=True)
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

print(f"\n== {PASS} passed, {FAIL} failed ==")
sys.exit(1 if FAIL else 0)
