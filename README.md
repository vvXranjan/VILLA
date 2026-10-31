# VILLA — Diwali Party Gate Check-in

Lightweight mobile-first gate check-in for the Desi Diwali Bling party. FastAPI + SQLite, no cloud, no auth, all data local.

## Features
- Google Sheets CSV import with **preview → confirm** two-step flow
- **Group registrations**: one CSV row (Solo / Couple / Duo / Trio) → individual guest records sharing one registration ID, each with its own check-in status
- Aadhaar columns always ignored — never stored, displayed, logged, or exported
- Exact payment mapping from `paid or not`: paid → PAID, not paid → UNPAID, blank/unknown → UNDER REVIEW
- Only **PAID** guests can check in; UNPAID and UNDER REVIEW are blocked
- Duplicate check-in prevention (race-safe, survives server restart)
- Manual-review flags when Entry Pass count ≠ names supplied (companions are never invented)
- Grouped search showing all members with individual CHECK IN buttons
- Arrived / Pending / Review lists, admin undo, admin reset-all
- Attendance CSV export, transactional imports, automatic DB backups
- Optional QR scanning via phone camera (each guest gets an opaque random token)

## Start

```bash
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

- Laptop: http://localhost:8000
- Phone (same Wi-Fi): http://<laptop-LAN-IP>:8000 (e.g. http://172.20.10.13:8000)

## Import guest list

1. Open the Google Form responses Sheet.
2. File → Download → Comma Separated Values (.csv).
3. On the app open the **Import** tab → choose the file → **1️⃣ Preview import**.
4. Review counts: registrations, individual guests, pass-type breakdown, paid/unpaid/review, manual-review cases.
5. Optionally tick "Replace entire existing guest list" (a backup is saved to `data/backups/` automatically).
6. **2️⃣ Confirm import** — the import is transactional: on any error the database is left unchanged.
7. Re-importing the same file updates rows and **preserves existing check-ins**; a duplicate-file warning is shown in preview.

## Caretaker instructions

1. **Gate** tab — stats bar at top shows guests / paid / checked-in / paid remaining.
2. Type guest name, WhatsApp number, or reg ID — group results appear instantly.
3. Each registration card shows pass type, payment badge, and every member with their own CHECK IN button.
4. Green CHECK IN per person — entering one group member never checks in the others.
5. Red UNPAID badge = do not allow entry. Amber ⚠ = registration needs manual review (fewer names than pass).
6. Already-checked-in members show ✔ with timestamp; duplicates are blocked.
7. Optional: **📷 Scan QR** resolves the scanner to that guest's full registration card. Manual search always works as fallback.

## Network & permissions

- Phone and laptop must be on the same Wi-Fi.
- macOS may prompt to allow incoming connections for Python — click **Allow**.
- Camera requires HTTPS or localhost in most browsers; over plain HTTP on a phone it may be blocked — manual search covers everything.
- No accounts, no API keys, no internet needed after setup.

## Data & privacy

- All data in local SQLite `data/party.db` (git-ignored), backups in `data/backups/` (git-ignored).
- Aadhaar columns are filtered at parse time; values are never read into records or returned by any API.
- QR tokens are random opaque hex — no personal data encoded.
- Export attendance from the Import tab (or `/api/export.csv`).
- Resets only via explicit admin buttons; destructive import requires an explicit checkbox + confirmation.

## Project layout

```
app/
  main.py         FastAPI endpoints (stats, search, check-in, import preview/confirm, export)
  database.py     SQLite schema + group-schema migration + backup dir
  csv_import.py   Google Forms parser (group split, payment mapping, Aadhaar filter)
  models.py       Pydantic schemas
  templates/index.html   mobile-first UI
  static/         style.css, app.js
```

## Test report (2026-10-31, real sheet import)

Actual counts: **19 registrations, 22 individual guests, 20 PAID, 2 UNPAID, 0 under review, 7 manual-review cases** (REG-002/003/005/006/010/018 Trio with 1 name, REG-019 Duo with 1 name).

| # | Test | Result |
|---|------|--------|
| 1 | Preview before import (counts + review cases) | ✅ |
| 2 | Transactional import; failed attempt rolled back cleanly first | ✅ (schema migration fixed, re-import OK) |
| 3 | Automatic DB backup before wipe | ✅ data/backups/ |
| 4 | Solo check-in | ✅ Ayushi Thomas |
| 5 | Couple member 1 in, member 2 stays pending | ✅ Anamika in / Anshul pending |
| 6 | Trio member individually in, other two pending | ✅ Jash in / Daksh, Rahul pending |
| 7 | Unpaid guest rejection | ✅ akshat, tanisha blocked |
| 8 | Duplicate check-in prevention | ✅ |
| 9 | Search by WhatsApp number | ✅ |
| 10 | Grouped search shows per-member statuses | ✅ |
| 11 | Persistence across server restart | ✅ |
| 12 | Attendance CSV export | ✅ |
| 13 | 8 concurrent check-ins, same guest → exactly 1 success | ✅ |
| 14 | QR valid token / invalid token (404) | ✅ |
| 15 | No Aadhaar field in DB, API, or export | ✅ |
| 16 | Manual review list shows all 7 flagged registrations | ✅ |

Test check-ins were reset afterwards — gate starts clean (22 guests, 0 checked in).

## Privacy
Guest information, Aadhaar numbers and payment records must never be committed.
