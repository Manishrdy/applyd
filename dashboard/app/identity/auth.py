from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import string
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError, VerificationError
from fastapi import HTTPException, Request, status

from app.config import settings
from app.database import get_db


logger = logging.getLogger("identity_service.auth")


VALID_ROLES: frozenset[str] = frozenset({"user", "admin"})


# ---- Time helpers ----------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---- Password hashing ------------------------------------------------------
#
# We use Argon2id (current OWASP recommendation). Inputs are HMAC'd with a
# server-side pepper before hashing, so even a full DB dump is useless to an
# attacker who never had filesystem access. Hash format on disk is the
# standard PHC string Argon2 produces, e.g.
#   $argon2id$v=19$m=65536,t=3,p=4$<salt>$<hash>
# This is self-describing — rehash detection just asks the library whether
# the stored parameters still match the configured ones.


_ph = PasswordHasher(
    time_cost=settings.argon2_time_cost,
    memory_cost=settings.argon2_memory_cost_kib,
    parallelism=settings.argon2_parallelism,
    hash_len=settings.argon2_hash_len,
    salt_len=settings.argon2_salt_len,
)


def _pepper_bytes() -> bytes:
    return settings.password_pepper.encode("utf-8")


def _peppered(password: str) -> bytes:
    """HMAC the raw password with the server pepper before passing to Argon2.

    Using HMAC (rather than concatenation) bounds the input length and avoids
    any pathological password that could weaken the hash function.
    """
    return hmac.new(_pepper_bytes(), password.encode("utf-8"), hashlib.sha256).digest()


def hash_password(password: str) -> str:
    return _ph.hash(_peppered(password))


def verify_password(password: str, password_hash: str) -> bool:
    try:
        _ph.verify(password_hash, _peppered(password))
        return True
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    except Exception:
        # Anything unexpected (malformed hash, IO) → fail closed.
        return False


def password_needs_rehash(password_hash: str) -> bool:
    """True when the stored hash uses outdated parameters or a legacy scheme."""
    if not password_hash:
        return False
    # Anything that isn't an Argon2 PHC string is legacy and must be rotated.
    if not password_hash.startswith("$argon2"):
        return True
    try:
        return _ph.check_needs_rehash(password_hash)
    except Exception:
        return True


# Dummy hash used to equalise timing when the email is unknown. Computed
# once at module load so the cost happens up-front, not on every signin.
_DUMMY_VERIFY_HASH: str = _ph.hash(_peppered(secrets.token_urlsafe(32)))


# ---- Session token storage -------------------------------------------------
#
# We store sha256(token) in auth_sessions.token, never the raw token. The raw
# token only ever exists in transit and in the user's cookie jar. A DB leak
# therefore cannot be replayed as authenticated traffic.


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---- Users -----------------------------------------------------------------


def create_user(name: str, email: str, password: str, role: str = "user") -> int:
    if role not in VALID_ROLES:
        raise ValueError(f"invalid role: {role!r}")
    normalized = email.strip().lower()
    logger.debug("create_user: inserting email=%s role=%s", normalized, role)
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO users (name, email, password_hash, role) VALUES (?, ?, ?, ?)",
            (name.strip(), normalized, hash_password(password), role),
        )
        user_id = int(cur.lastrowid)
    logger.info("create_user: created user_id=%s email=%s role=%s", user_id, normalized, role)
    return user_id


def get_user_role(user_id: int) -> str | None:
    with get_db() as conn:
        row = conn.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
    return row["role"] if row else None


def require_admin(user_id: int | None) -> None:
    """Raise HTTPException(403) unless the user has the admin role.

    Designed as a building block for future admin-only routes — call it after
    you've validated the session and resolved a user_id.
    """
    if user_id is None:
        raise HTTPException(status_code=401, detail="authentication required")
    role = get_user_role(user_id)
    if role != "admin":
        logger.warning("require_admin: denied user_id=%s role=%s", user_id, role)
        raise HTTPException(status_code=403, detail="admin required")


def validate_password_strength(password: str) -> str | None:
    if len(password) < 10:
        return "at least 10 characters"
    if not any(c.islower() for c in password):
        return "a lowercase letter"
    if not any(c.isupper() for c in password):
        return "an uppercase letter"
    if not any(c.isdigit() for c in password):
        return "a number"
    if not any(c in string.punctuation for c in password):
        return "a symbol"
    return None


def authenticate_user(email: str, password: str) -> int | None:
    """Verify (email, password) in constant time relative to email existence.

    We always run a full Argon2 verify — against the real hash if the email
    exists, against a fixed sentinel hash if it doesn't. This prevents an
    attacker from telling "user not found" from "wrong password" by timing.

    On success, if the stored hash uses outdated parameters or the legacy
    pbkdf2 format, transparently rehash and store the upgrade.
    """
    normalized = email.strip().lower()
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, password_hash FROM users WHERE email = ?",
            (normalized,),
        ).fetchone()

    if row is None:
        # Burn the same CPU we'd burn for a real verify so timing leaks nothing.
        verify_password(password, _DUMMY_VERIFY_HASH)
        return None

    stored_hash: str = row["password_hash"]
    user_id = int(row["id"])

    # Legacy hashes from the pre-Argon2 schema look like "<saltHex>:<hexDigest>".
    # Verify them with the old algorithm, then rehash to Argon2 on success.
    if not stored_hash.startswith("$argon2"):
        if not _verify_legacy_pbkdf2(password, stored_hash):
            return None
        _upgrade_password_hash(user_id, password)
        return user_id

    if not verify_password(password, stored_hash):
        return None

    if password_needs_rehash(stored_hash):
        _upgrade_password_hash(user_id, password)
    return user_id


def _verify_legacy_pbkdf2(password: str, password_hash: str) -> bool:
    """Verify a hash produced by the previous PBKDF2-SHA256 scheme.

    Format: "<saltHex>:<digestHex>". Legacy hashes were NOT peppered, so we
    compute the raw PBKDF2 here — not on the peppered input.
    """
    try:
        salt_hex, expected = password_hash.split(":", 1)
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000).hex()
    return hmac.compare_digest(actual, expected)


def _upgrade_password_hash(user_id: int, plaintext: str) -> None:
    new_hash = hash_password(plaintext)
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (new_hash, user_id),
        )


def get_user_email(user_id: int) -> str | None:
    with get_db() as conn:
        row = conn.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()
    return row["email"] if row else None


# ---- Sessions --------------------------------------------------------------


def create_session(
    user_id: int,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> tuple[str, datetime]:
    """Issue a new session. Returns (raw_token, expires_at).

    The raw token is returned to the caller exactly once so it can be set as
    an HttpOnly cookie. The DB only ever stores sha256(token).
    """
    token = secrets.token_urlsafe(32)
    public_id = secrets.token_urlsafe(16)
    expires_at = _utcnow() + timedelta(days=settings.session_ttl_days)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO auth_sessions (public_id, token, user_id, expires_at, ip_address, user_agent, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                public_id,
                _hash_token(token),
                user_id,
                expires_at.isoformat(),
                ip_address,
                user_agent,
                _utcnow().isoformat(),
            ),
        )
    return token, expires_at


def clear_session(token: str) -> None:
    if not token:
        return
    with get_db() as conn:
        conn.execute("DELETE FROM auth_sessions WHERE token = ?", (_hash_token(token),))


def validate_session(token: str | None) -> int | None:
    """Validate a session in a single DB round-trip.

    Uses UPDATE … RETURNING (SQLite ≥ 3.35) to update last_seen_at and read
    back user_id atomically. Expired rows are deleted in-line.
    """
    if not token:
        return None
    token_hash = _hash_token(token)
    now = _utcnow()
    now_iso = now.isoformat()
    with get_db() as conn:
        row = conn.execute(
            "UPDATE auth_sessions SET last_seen_at = ? "
            "WHERE token = ? AND expires_at > ? "
            "RETURNING user_id",
            (now_iso, token_hash, now_iso),
        ).fetchone()
        if row is not None:
            return int(row["user_id"])
        # No row updated → either missing or expired. Best-effort cleanup.
        conn.execute(
            "DELETE FROM auth_sessions WHERE token = ? AND expires_at <= ?",
            (token_hash, now_iso),
        )
    return None


# ---- Rate-limit policy (runtime-editable, falls back to settings) ---------


@dataclass(frozen=True)
class RateLimitPolicy:
    pair_max: int
    email_max: int
    ip_max: int
    window_seconds: int
    lockout_seconds: int


_POLICY_KEY = "rate_limit"


def _default_policy() -> RateLimitPolicy:
    return RateLimitPolicy(
        pair_max=settings.auth_rate_limit_max_attempts,
        email_max=settings.auth_rate_limit_email_max_attempts,
        ip_max=settings.auth_rate_limit_ip_max_attempts,
        window_seconds=settings.auth_rate_limit_window_seconds,
        lockout_seconds=settings.auth_rate_limit_lockout_seconds,
    )


def get_rate_limit_policy() -> RateLimitPolicy:
    """Read the current policy from auth_policy, falling back to settings."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM auth_policy WHERE key = ?",
            (_POLICY_KEY,),
        ).fetchone()
    if row is None or not row["value"]:
        return _default_policy()
    try:
        payload = json.loads(row["value"])
        if not isinstance(payload, dict):
            return _default_policy()
    except (TypeError, ValueError):
        return _default_policy()
    base = _default_policy()
    return RateLimitPolicy(
        pair_max=int(payload.get("pair_max", base.pair_max)),
        email_max=int(payload.get("email_max", base.email_max)),
        ip_max=int(payload.get("ip_max", base.ip_max)),
        window_seconds=int(payload.get("window_seconds", base.window_seconds)),
        lockout_seconds=int(payload.get("lockout_seconds", base.lockout_seconds)),
    )


def set_rate_limit_policy(policy: RateLimitPolicy, *, updated_by: str | None = None) -> None:
    payload = json.dumps(
        {
            "pair_max": policy.pair_max,
            "email_max": policy.email_max,
            "ip_max": policy.ip_max,
            "window_seconds": policy.window_seconds,
            "lockout_seconds": policy.lockout_seconds,
        },
        separators=(",", ":"),
    )
    with get_db() as conn:
        conn.execute(
            "INSERT INTO auth_policy(key, value, updated_at, updated_by) "
            "VALUES(?, ?, datetime('now'), ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "value = excluded.value, updated_at = datetime('now'), updated_by = excluded.updated_by",
            (_POLICY_KEY, payload, updated_by),
        )


# ---- Rate limiting ---------------------------------------------------------


def _rate_limit_bucket_key(ip_address: str, email: str) -> str:
    return f"pair::{ip_address.lower()}::{email.strip().lower()}"


def _rate_limit_email_key(email: str) -> str:
    return f"email::{email.strip().lower()}"


def _rate_limit_ip_key(ip_address: str) -> str:
    return f"ip::{ip_address.lower()}"


def is_signin_rate_limited(ip_address: str, email: str) -> bool:
    now = _utcnow()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT locked_until FROM auth_rate_limits "
            "WHERE bucket_key IN (?, ?, ?) AND locked_until IS NOT NULL",
            (
                _rate_limit_bucket_key(ip_address, email),
                _rate_limit_email_key(email),
                _rate_limit_ip_key(ip_address),
            ),
        ).fetchall()
    for row in rows:
        try:
            locked_until = datetime.fromisoformat(row["locked_until"])
        except Exception:
            continue
        if locked_until.tzinfo is None:
            locked_until = locked_until.replace(tzinfo=timezone.utc)
        if locked_until > now:
            return True
    return False


def _increment_bucket(bucket_key: str, max_attempts: int, *, policy: RateLimitPolicy | None = None) -> None:
    """Increment a single rate-limit bucket and trip the lockout if needed."""
    pol = policy or get_rate_limit_policy()
    now = _utcnow()
    window_start = now - timedelta(seconds=pol.window_seconds)
    with get_db() as conn:
        row = conn.execute(
            "SELECT failed_attempts, window_started_at FROM auth_rate_limits WHERE bucket_key = ?",
            (bucket_key,),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO auth_rate_limits (bucket_key, failed_attempts, window_started_at, locked_until) "
                "VALUES (?, ?, ?, ?)",
                (bucket_key, 1, now.isoformat(), None),
            )
            return
        attempts = int(row["failed_attempts"])
        try:
            started = datetime.fromisoformat(row["window_started_at"])
        except Exception:
            started = now
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        if started < window_start:
            attempts = 1
            started = now
        else:
            attempts += 1
        locked_until: str | None = None
        if attempts >= max_attempts:
            locked_until = (now + timedelta(seconds=pol.lockout_seconds)).isoformat()
        conn.execute(
            "UPDATE auth_rate_limits SET failed_attempts = ?, window_started_at = ?, locked_until = ? "
            "WHERE bucket_key = ?",
            (attempts, started.isoformat(), locked_until, bucket_key),
        )


def record_signin_failure(ip_address: str, email: str) -> None:
    """Increment all three buckets: (IP, email), email-wide, IP-wide.

    Per-(IP, email) trips fast — catches a focused attacker. Per-email
    trips at a higher threshold (catches distributed credential stuffing
    without locking the real owner out from a clean IP for a single
    misclick). Per-IP catches an attacker rotating emails.
    """
    policy = get_rate_limit_policy()
    _increment_bucket(_rate_limit_bucket_key(ip_address, email), policy.pair_max, policy=policy)
    _increment_bucket(_rate_limit_email_key(email), policy.email_max, policy=policy)
    _increment_bucket(_rate_limit_ip_key(ip_address), policy.ip_max, policy=policy)


def clear_signin_failures(ip_address: str, email: str) -> None:
    """Clear ONLY the (IP, email) bucket on a successful signin.

    Email-wide and IP-wide buckets persist — a successful signin from one
    address does not retroactively forgive failures observed elsewhere.
    """
    with get_db() as conn:
        conn.execute(
            "DELETE FROM auth_rate_limits WHERE bucket_key = ?",
            (_rate_limit_bucket_key(ip_address, email),),
        )


def is_signup_rate_limited(ip_address: str) -> bool:
    """Cheap IP-level lockout for the signup endpoint."""
    now = _utcnow()
    with get_db() as conn:
        row = conn.execute(
            "SELECT locked_until FROM auth_rate_limits WHERE bucket_key = ?",
            (f"signup_ip::{ip_address.lower()}",),
        ).fetchone()
    if not row or not row["locked_until"]:
        return False
    try:
        locked_until = datetime.fromisoformat(row["locked_until"])
    except Exception:
        return False
    if locked_until.tzinfo is None:
        locked_until = locked_until.replace(tzinfo=timezone.utc)
    locked = locked_until > now
    if locked:
        logger.debug("is_signup_rate_limited: ip=%s locked_until=%s", ip_address, locked_until.isoformat())
    return locked


def record_signup_attempt(ip_address: str) -> None:
    _increment_bucket(f"signup_ip::{ip_address.lower()}", settings.auth_signup_ip_max_attempts)


# ---- Audit log -------------------------------------------------------------


def log_auth_event(
    *,
    event_type: str,
    success: bool,
    email: str | None = None,
    user_id: int | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    detail: str | None = None,
) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO auth_events (event_type, email, user_id, ip_address, user_agent, success, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event_type, email, user_id, ip_address, user_agent, 1 if success else 0, detail),
        )


def list_active_sessions(user_id: int) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT public_id, created_at, expires_at, ip_address, user_agent, last_seen_at "
            "FROM auth_sessions WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    out: list[dict] = []
    for row in rows:
        d = dict(row)
        pid = d.pop("public_id", None)
        out.append({"session_id": pid, **d})
    return out


def clear_all_sessions(user_id: int, keep_token: str | None = None) -> int:
    """Delete every session for a user, optionally keeping the current one.

    keep_token is the RAW cookie value; we hash it before comparing.
    """
    keep_hash = _hash_token(keep_token) if keep_token else None
    with get_db() as conn:
        if keep_hash:
            cur = conn.execute(
                "DELETE FROM auth_sessions WHERE user_id = ? AND token != ?",
                (user_id, keep_hash),
            )
        else:
            cur = conn.execute(
                "DELETE FROM auth_sessions WHERE user_id = ?",
                (user_id,),
            )
    return int(cur.rowcount or 0)


# ---- Admin queries / mutations --------------------------------------------


def admin_list_sessions(limit: int = 200) -> list[dict]:
    """Every active session across all users, newest-touched first."""
    limit = max(1, min(int(limit), 1000))
    with get_db() as conn:
        rows = conn.execute(
            "SELECT s.public_id, s.user_id, u.email, "
            "s.created_at, s.expires_at, s.last_seen_at, s.ip_address, s.user_agent "
            "FROM auth_sessions s "
            "LEFT JOIN users u ON u.id = s.user_id "
            "WHERE s.expires_at > datetime('now') "
            "ORDER BY COALESCE(s.last_seen_at, s.created_at) DESC "
            "LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def admin_terminate_session(public_id: str) -> bool:
    """Kill a single session by its public id. Returns True if removed."""
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM auth_sessions WHERE public_id = ?",
            (public_id,),
        )
    return int(cur.rowcount or 0) > 0


def admin_list_failed_logins(limit: int = 100) -> list[dict]:
    """Recent failed sign-in attempts. Joins auth_events for richer detail."""
    limit = max(1, min(int(limit), 500))
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, event_type, email, user_id, ip_address, user_agent, detail, created_at "
            "FROM auth_events "
            "WHERE success = 0 "
            "ORDER BY id DESC "
            "LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def admin_clear_failed_logins(
    *, email: str | None = None, ip_address: str | None = None
) -> dict[str, int]:
    """Clear matching rate-limit buckets and failed rows in auth_events.

    Scoped to either email, ip, or — if both None — broad unlock plus all
    failed events. Rate-limit clears are conservative (same as before).
    """
    patterns: list[str] = []
    if email:
        normalized = email.strip().lower()
        patterns.append(f"pair::%::{normalized}")
        patterns.append(f"email::{normalized}")
    if ip_address:
        normalized_ip = ip_address.strip().lower()
        patterns.append(f"pair::{normalized_ip}::%")
        patterns.append(f"ip::{normalized_ip}")
        patterns.append(f"signup_ip::{normalized_ip}")
    norm_email = (email or "").strip().lower() or None
    norm_ip = (ip_address or "").strip().lower() or None
    buckets_cleared = 0
    events_deleted = 0
    with get_db() as conn:
        if not patterns:
            # Caller asked for a broad clear — only clear lockouts, not the
            # full bucket history, so legitimate counters survive.
            cur = conn.execute(
                "UPDATE auth_rate_limits SET locked_until = NULL "
                "WHERE locked_until IS NOT NULL"
            )
            buckets_cleared = int(cur.rowcount or 0)
        else:
            for pat in patterns:
                cur = conn.execute(
                    "DELETE FROM auth_rate_limits WHERE bucket_key LIKE ?",
                    (pat,),
                )
                buckets_cleared += int(cur.rowcount or 0)

        if not norm_email and not norm_ip:
            cur = conn.execute("DELETE FROM auth_events WHERE success = 0")
            events_deleted = int(cur.rowcount or 0)
        elif norm_email and norm_ip:
            cur = conn.execute(
                "DELETE FROM auth_events WHERE success = 0 AND ("
                "(email IS NOT NULL AND lower(trim(email)) = ?) OR "
                "(ip_address IS NOT NULL AND lower(trim(ip_address)) = ?))",
                (norm_email, norm_ip),
            )
            events_deleted = int(cur.rowcount or 0)
        elif norm_email:
            cur = conn.execute(
                "DELETE FROM auth_events WHERE success = 0 AND email IS NOT NULL "
                "AND lower(trim(email)) = ?",
                (norm_email,),
            )
            events_deleted = int(cur.rowcount or 0)
        else:
            cur = conn.execute(
                "DELETE FROM auth_events WHERE success = 0 AND ip_address IS NOT NULL "
                "AND lower(trim(ip_address)) = ?",
                (norm_ip,),
            )
            events_deleted = int(cur.rowcount or 0)

    return {"cleared": buckets_cleared, "events_deleted": events_deleted}


def admin_list_locked_buckets() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT bucket_key, failed_attempts, window_started_at, locked_until "
            "FROM auth_rate_limits "
            "WHERE locked_until IS NOT NULL "
            "ORDER BY locked_until DESC "
            "LIMIT 200"
        ).fetchall()
    return [dict(r) for r in rows]


def admin_unlock_bucket(bucket_key: str) -> bool:
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE auth_rate_limits SET locked_until = NULL, failed_attempts = 0 "
            "WHERE bucket_key = ?",
            (bucket_key,),
        )
    return int(cur.rowcount or 0) > 0


def admin_set_user_role(user_id: int, role: str) -> bool:
    if role not in VALID_ROLES:
        raise ValueError(f"invalid role: {role!r}")
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE users SET role = ? WHERE id = ?",
            (role, user_id),
        )
    return int(cur.rowcount or 0) > 0


def require_user(request: Request) -> int:
    """FastAPI dependency: return authenticated user_id or raise 401.

    `auth_middleware` populates `request.state.user_id` for protected
    routes; this just exposes it cleanly and fails closed if missing.
    """
    user_id = getattr(request.state, "user_id", None)
    if not isinstance(user_id, int):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required")
    return user_id


def admin_list_users(limit: int = 200) -> list[dict]:
    limit = max(1, min(int(limit), 1000))
    with get_db() as conn:
        rows = conn.execute(
            "SELECT u.id, u.email, u.name, u.role, u.created_at, "
            "(SELECT COUNT(*) FROM auth_sessions s WHERE s.user_id = u.id AND s.expires_at > datetime('now')) AS active_sessions, "
            "(SELECT MAX(created_at) FROM auth_events e WHERE e.user_id = u.id AND e.event_type = 'signin' AND e.success = 1) AS last_signin "
            "FROM users u "
            "ORDER BY u.id DESC "
            "LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
