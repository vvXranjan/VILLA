"""Auth: PBKDF2 passwords, HMAC-signed session cookie, DB rate limiting, roles.

- Passwords: stdlib hashlib.pbkdf2_hmac, stored as
  `pbkdf2_sha256$<iters>$<salt_hex>$<hash_hex>` — the plaintext is never stored.
- Session: HMAC-SHA256-signed cookie `villa_session` (payload exp + user id),
  secret from env SESSION_SECRET (only auth env var). HttpOnly, SameSite=Lax,
  Path=/. 12h expiry.
- Rate limit: 5 failed logins / 15 min per `ip:<addr>` and `email:<addr>` key
  in the login_attempts table (either key at/over the limit => 429; cleared on
  a successful login).
- Permissions: can(conn, user, action, event_id) — default deny.
  is_owner=true is the global owner (allow-all, no event_members row needed).
  event_members holds only co_host / gate_staff with a real event_id.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException, Request, Response
from sqlalchemy import text

from .database import get_conn, utc_now_str

SESSION_COOKIE = "villa_session"
SESSION_TTL_SECONDS = 12 * 3600  # 12 hours
PBKDF2_ITERATIONS = 600_000

RATE_LIMIT_MAX = 5
RATE_LIMIT_WINDOW_MIN = 15

PUBLIC_EXACT = {"/", "/api/health", "/api/login", "/api/logout", "/api/me"}
PUBLIC_PREFIXES = ("/static/", "/api/invites/")

# ------------------------------------------------------------------ passwords


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


_dummy_hash: Optional[str] = None


def _get_dummy_hash() -> str:
    """PBKDF2 of a random string — verified when the email is unknown so an
    unknown-email login costs the same time as a wrong password (no user
    enumeration by timing)."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password(secrets.token_urlsafe(24))
    return _dummy_hash


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, iters_s, salt_hex, hash_hex = encoded.split("$")
        if algo != "pbkdf2_sha256":
            return False
        iters = int(iters_s)
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), iters
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def burn_password_time(password: str) -> None:
    """Run one PBKDF2 verify against a dummy hash — timing parity for the
    unknown-email path in login/accept (no user enumeration by timing)."""
    verify_password(password, _get_dummy_hash())


def check_password_strength(password: str) -> None:
    if len(password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")


# ------------------------------------------------------------------- sessions


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def session_secret() -> str:
    secret = os.environ.get("SESSION_SECRET", "").strip()
    if not secret:
        # Never fall back to a default — a guessable session secret is worse
        # than no login at all. Surfaced as a clear 503 by the login endpoint.
        raise HTTPException(
            503, "Server auth not configured: SESSION_SECRET env var is missing"
        )
    return secret


def create_session_token(user_id: int) -> str:
    payload = json.dumps(
        {"uid": user_id, "exp": int(time.time()) + SESSION_TTL_SECONDS},
        separators=(",", ":"),
    ).encode("utf-8")
    sig = hmac.new(session_secret().encode("utf-8"), payload, hashlib.sha256).digest()
    return f"{_b64e(payload)}.{_b64e(sig)}"


def parse_session_token(token: str) -> Optional[int]:
    """Return the user id for a valid, unexpired token; None otherwise."""
    try:
        secret = session_secret()
        payload_b64, sig_b64 = token.split(".", 1)
        payload = _b64d(payload_b64)
        sig = _b64d(sig_b64)
        expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            return None
        data = json.loads(payload)
        if int(data.get("exp", 0)) < time.time():
            return None
        uid = int(data["uid"])
        return uid
    except Exception:
        return None


def cookie_secure(request: Request) -> bool:
    """Secure flag: COOKIE_SECURE env override, else the request's scheme.
    Never hard-coded — on http://localhost it must be False or the cookie
    is never sent back."""
    override = os.environ.get("COOKIE_SECURE", "").strip().lower()
    if override in ("1", "true", "yes"):
        return True
    if override in ("0", "false", "no"):
        return False
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    if proto:
        return proto == "https"
    return request.url.scheme == "https"


def set_session_cookie(response: Response, request: Request, user_id: int) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        create_session_token(user_id),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=cookie_secure(request),
        path="/",
    )


def clear_session_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        SESSION_COOKIE,
        httponly=True,
        samesite="lax",
        secure=cookie_secure(request),
        path="/",
    )


# ------------------------------------------------------------ request helpers


def is_public(path: str) -> bool:
    return path in PUBLIC_EXACT or path.startswith(PUBLIC_PREFIXES)


def get_user(conn, user_id: int) -> Optional[dict]:
    row = conn.execute(
        text("SELECT id, name, email, is_owner, created_at FROM users WHERE id=:i"),
        {"i": user_id},
    ).mappings().fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "email": row["email"],
        "is_owner": bool(row["is_owner"]),
    }


def user_by_email(conn, email: str) -> Optional[dict]:
    row = conn.execute(
        text("SELECT id, name, email, password_hash, is_owner FROM users WHERE email=:e"),
        {"e": email.strip().lower()},
    ).mappings().fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "email": row["email"],
        "password_hash": row["password_hash"],
        "is_owner": bool(row["is_owner"]),
    }


def load_user_from_request(request: Request) -> Optional[dict]:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    uid = parse_session_token(token)
    if uid is None:
        return None
    try:
        with get_conn() as conn:
            return get_user(conn, uid)
    except Exception:
        return None


def require_user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, "Not authenticated")
    return user


def create_user(conn, name: str, email: str, password: str, is_owner: bool = False) -> int:
    # is_owner is INTEGER on both backends (SQLite has no native bool; PG rejects
    # Python bools for an integer column). Bind 1/0 so both engines accept it.
    return conn.execute(
        text(
            """INSERT INTO users (name, email, password_hash, is_owner, created_at)
               VALUES (:name, :email, :hash, :owner, :created)
               RETURNING id"""
        ),
        {
            "name": name.strip(),
            "email": email.strip().lower(),
            "hash": hash_password(password),
            "owner": 1 if is_owner else 0,
            "created": utc_now_str(),
        },
    ).scalar()


# --------------------------------------------------------------- rate limit


def _parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def _rate_keys(ip: str, email: str) -> list[str]:
    # Cap the email to the column/identity length so a giant string can't
    # produce an unbounded key (or a key that never matches on retry).
    email = email.strip().lower()[:254]
    return [f"ip:{ip}", f"email:{email}"]


def _window_expired(window_start: str) -> bool:
    try:
        started = _parse_ts(window_start)
    except Exception:
        return True
    age = datetime.now(timezone.utc) - started
    return age >= timedelta(minutes=RATE_LIMIT_WINDOW_MIN)


def check_rate_limit(conn, ip: str, email: str) -> None:
    """Raise 429 when either the ip: or email: key is at/over the limit."""
    for key in _rate_keys(ip, email):
        row = conn.execute(
            text("SELECT fail_count, window_start FROM login_attempts WHERE key=:k"),
            {"k": key},
        ).mappings().fetchone()
        if not row:
            continue
        if row["fail_count"] >= RATE_LIMIT_MAX and not _window_expired(row["window_start"]):
            raise HTTPException(
                429,
                f"Too many failed attempts — try again in {RATE_LIMIT_WINDOW_MIN} minutes",
            )


def record_failure(conn, ip: str, email: str) -> None:
    now = utc_now_str()
    for key in _rate_keys(ip, email):
        row = conn.execute(
            text("SELECT fail_count, window_start FROM login_attempts WHERE key=:k"),
            {"k": key},
        ).mappings().fetchone()
        if not row or _window_expired(row["window_start"]):
            conn.execute(
                text(
                    """INSERT INTO login_attempts (key, fail_count, window_start, last_attempt)
                       VALUES (:k, 1, :now, :now)
                       ON CONFLICT(key) DO UPDATE SET
                         fail_count=1, window_start=:now, last_attempt=:now"""
                ),
                {"k": key, "now": now},
            )
        else:
            conn.execute(
                text(
                    """UPDATE login_attempts
                       SET fail_count=fail_count+1, last_attempt=:now WHERE key=:k"""
                ),
                {"k": key, "now": now},
            )


def clear_failures(conn, email: str) -> None:
    """Clear only the email: key on a successful login. The ip: key is left in
    place so a single IP hammering many emails stays rate-limited."""
    key = f"email:{email.strip().lower()[:254]}"
    conn.execute(text("DELETE FROM login_attempts WHERE key=:k"), {"k": key})


def client_ip(request: Request) -> str:
    """Client IP: first x-forwarded-for entry (Vercel/Cloudflare prepend the
    real client) else the socket peer. No other request header is trusted —
    spoofing X-Forwarded-For only shifts your own key to a fresh bucket."""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        first = fwd.split(",")[0].strip()
        if first:
            return first
    if request.client:
        return request.client.host
    return "unknown"


# -------------------------------------------------------------- permissions

GATE_ACTIONS = {"search", "view", "checkin", "undo"}
CO_HOST_ACTIONS = GATE_ACTIONS | {
    "export",
    "import",
    "add_companion",
    "edit_event",
    "invite",
}
ROLE_ACTIONS = {
    "gate_staff": GATE_ACTIONS,
    "co_host": CO_HOST_ACTIONS,
}
# Owner-only actions (not in any role set): create_event, delete_event,
# manage_tickets, wipe, reset_all, manage_members. is_owner => allow-all.


def member_role(conn, user_id: int, event_id: Optional[int]) -> Optional[str]:
    if event_id is None:
        return None
    row = conn.execute(
        text("SELECT role FROM event_members WHERE event_id=:e AND user_id=:u"),
        {"e": event_id, "u": user_id},
    ).fetchone()
    if not row:
        return None
    return row[0] if not isinstance(row, dict) else row["role"]


def can(conn, user: Optional[dict], action: str, event_id: Optional[int]) -> bool:
    """Default deny. is_owner is allow-all (global owner, no members row)."""
    if not user:
        return False
    if user.get("is_owner"):
        return True
    role = member_role(conn, user["id"], event_id)
    if role is None:
        return False
    return action in ROLE_ACTIONS.get(role, set())


def can_any(conn, user: Optional[dict], action: str) -> bool:
    """True when the user holds `action` on at least one event (for endpoints
    that span all events, e.g. stats/export over the whole guest list)."""
    if not user:
        return False
    if user.get("is_owner"):
        return True
    rows = conn.execute(
        text("SELECT event_id, role FROM event_members WHERE user_id=:u"),
        {"u": user["id"]},
    ).mappings().all()
    return any(action in ROLE_ACTIONS.get(r["role"], set()) for r in rows)


def accessible_event_ids(conn, user: Optional[dict], action: str) -> set[int]:
    """Event ids the user may perform `action` on. Owner -> every event.
    Non-owner -> only the events where their membership role grants `action`.

    This is the filtering counterpart to can_any(): every endpoint that returns
    guest/registration data must narrow its rows to this set (or resolve a
    single event_id and call can()); can_any alone must not gate data reads."""
    if not user:
        return set()
    if user.get("is_owner"):
        return set(conn.execute(text("SELECT id FROM events")).scalars().all())
    rows = conn.execute(
        text("SELECT event_id, role FROM event_members WHERE user_id=:u"),
        {"u": user["id"]},
    ).mappings().all()
    return {r["event_id"] for r in rows if action in ROLE_ACTIONS.get(r["role"], set())}


def require_action(
    conn, user: dict, action: str, event_id: Optional[int] = None
) -> None:
    if not can(conn, user, action, event_id):
        raise HTTPException(403, "Your role does not allow this action")


def require_any_action(conn, user: dict, action: str) -> None:
    if not can_any(conn, user, action):
        raise HTTPException(403, "Your role does not allow this action")


# ------------------------------------------------------------------- invites


def generate_invite_code() -> str:
    return secrets.token_urlsafe(24)


def invite_expiry_iso() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=7)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
