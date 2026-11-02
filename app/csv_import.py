"""Google Forms CSV parsing for group registrations.

Aadhaar columns are dropped immediately and never leave this module's
header-level filtering — values are never read, stored, logged, or returned.
"""
import csv
import html
import io
import re
import secrets
from typing import Optional

AADHAAR_RE = re.compile(r"aadhaar|aadhar|uid\b", re.I)

PASS_EXPECTED = {"solo": 1, "couple": 2, "duo": 2, "trio": 3}


def norm_payment(v: str) -> str:
    """Exact sheet mapping: paid→PAID, not paid→UNPAID, else UNDER REVIEW."""
    v = (v or "").strip().lower()
    if v == "paid":
        return "PAID"
    if v in {"not paid", "notpaid", "unpaid"}:
        return "UNPAID"
    return "UNDER REVIEW"


def _find(headers: list[str], *needles: str) -> Optional[int]:
    for i, h in enumerate(headers):
        hl = h.strip().lower()
        for n in needles:
            if n in hl:
                return i
    return None


def split_names(raw: str) -> list[str]:
    """Split on commas/newlines/semicolons; clean HTML line-break artifacts."""
    s = raw or ""
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)   # <br>, <br/>, <BR>
    s = re.sub(r"</p>\s*<p[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)                    # any other stray tags
    s = html.unescape(s)                             # &amp; → &
    parts = re.split(r"[,\n;]+", s)
    return [p.strip() for p in parts if p.strip()]


def parse_google_csv(text: str) -> dict:
    """Parse the Google Forms export into registration dicts.

    Returns dict with:
      registrations: list of {reg_id, entry_pass, phone, email, payment_status,
                              needs_review, review_reason, names[]}
      blank_rows_skipped: int
      aadhaar_columns_ignored: list[str]
    """
    reader = csv.reader(io.StringIO(text))
    try:
        raw_headers = next(reader)
    except StopIteration:
        raise ValueError("Empty CSV file")

    headers = [h.strip() for h in raw_headers]
    aadhaar_cols = [h for h in headers if AADHAAR_RE.search(h)]
    # Work strictly on safe indices; aadhaar columns are never indexed again.
    safe = [(i, h) for i, h in enumerate(headers) if not AADHAAR_RE.search(h)]
    safe_headers = [h for _, h in safe]

    i_pass = _find(safe_headers, "entry pass")
    i_name = _find(safe_headers, "name")
    i_phone = _find(safe_headers, "whatsapp", "phone", "mobile")
    i_email = _find(safe_headers, "email")
    i_pay = _find(safe_headers, "paid or not", "payment")
    if i_name is None:
        raise ValueError("Could not find a Name column in the CSV")

    registrations = []
    blank = 0
    for row in reader:
        row = row + [""] * (len(headers) - len(row))

        def cell(idx):
            if idx is None:
                return ""
            # map safe-index back to raw index
            raw_i = safe[idx][0]
            return row[raw_i].strip() if raw_i < len(row) else ""

        names = split_names(cell(i_name))
        entry_pass_raw = cell(i_pass)
        pay_raw = cell(i_pay)
        phone_raw = cell(i_phone)
        email = cell(i_email)

        # Completely empty rows (or payment-only strays) are skipped.
        if not names and not entry_pass_raw and not phone_raw and not email:
            blank += 1
            continue

        if not names:
            # Row has some data but no usable name — flag, don't invent names.
            blank += 1
            continue

        entry_pass = entry_pass_raw.strip().capitalize()
        pay = norm_payment(pay_raw)
        phone = re.sub(r"\D", "", phone_raw)[-10:] if phone_raw else ""

        expected = PASS_EXPECTED.get(entry_pass_raw.strip().lower())
        capacity = expected if expected is not None else len(names)
        needs_review = 0
        reason = ""
        if expected is not None and len(names) < expected:
            missing = expected - len(names)
            needs_review = 1
            reason = (
                f"INCOMPLETE GROUP DETAILS — {entry_pass} expects {expected}, "
                f"{len(names)} named ({missing} companion name(s) not supplied)"
            )
        elif expected is not None and len(names) > expected:
            needs_review = 1
            reason = (
                f"INCOMPLETE GROUP DETAILS — {entry_pass} expects {expected}, "
                f"{len(names)} names supplied (verify at gate)"
            )

        reg_num = len(registrations) + 1
        registrations.append(
            {
                "reg_id": f"REG-{reg_num:03d}",
                "entry_pass": entry_pass,
                "phone": phone,
                "email": email,
                "payment_status": pay,
                "needs_review": needs_review,
                "review_reason": reason,
                "expected_capacity": capacity,
                "names": names,
            }
        )

    return {
        "registrations": registrations,
        "blank_rows_skipped": blank,
        "aadhaar_columns_ignored": aadhaar_cols,
    }


def new_qr_token() -> str:
    return secrets.token_hex(16)
