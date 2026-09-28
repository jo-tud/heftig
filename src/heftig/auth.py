"""Single-user authentication.

- passwords: scrypt (stdlib ``hashlib.scrypt``) with per-user salt
- browser sessions: random token in an HttpOnly/SameSite=Strict cookie, only its SHA-256 is
  stored; each session has its own CSRF token for state-changing requests
- API tokens: separate, random, shown once, stored hashed, revocable
- login rate limit per client address and per username, persisted in the database
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import timedelta

from .db import iso, now_iso, utcnow, write_tx
from .i18n import _

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**15, 8, 1
TOKEN_PREFIX = "hft_"  # noqa: S105 - not a secret, a marker
MIN_PASSWORD_LEN = 10


class AuthError(Exception):
    pass


class RateLimited(AuthError):
    pass


@dataclass
class User:
    id: int
    username: str


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, maxmem=64 * 1024**2
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, dk_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(
            password.encode("utf-8"),
            salt=base64.b64decode(salt_b64),
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=64 * 1024**2,
        )
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except (ValueError, TypeError):
        return False


_DUMMY: list[str] = []


def _dummy_hash() -> str:
    if not _DUMMY:
        _DUMMY.append(hash_password(secrets.token_urlsafe(12)))
    return _DUMMY[0]


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LEN:
        raise AuthError(
            _("The password must be at least %(num)s characters long.", num=MIN_PASSWORD_LEN)
        )


def user_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def create_user(conn: sqlite3.Connection, username: str, password: str) -> User:
    validate_password(password)
    username = username.strip()
    if not username:
        raise AuthError(_("User name missing."))
    with write_tx(conn):
        if user_count(conn) > 0:
            raise AuthError(_("There is already a user (single-user system)."))
        cur = conn.execute(
            "INSERT INTO users(username, password_hash, created_at) VALUES(?,?,?)",
            (username, hash_password(password), now_iso()),
        )
        return User(int(cur.lastrowid), username)  # type: ignore[arg-type]


def set_password(conn: sqlite3.Connection, username: str, password: str) -> None:
    validate_password(password)
    with write_tx(conn):
        cur = conn.execute(
            "UPDATE users SET password_hash=? WHERE username=?", (hash_password(password), username)
        )
        if cur.rowcount == 0:
            raise AuthError(_("Unknown user."))
        # existing sessions end when the password changes
        conn.execute(
            "DELETE FROM sessions WHERE user_id=(SELECT id FROM users WHERE username=?)",
            (username,),
        )


MAX_USERNAME = 200
MAX_PASSWORD = 1024
USER_LIMIT_FACTOR = 3  # per username (all addresses together) a little more generous


def _rate_keys(username: str, client: str) -> list[str]:
    # the typed name is hashed: an unauthenticated caller must not be able to store text
    user = hashlib.sha256(username.strip().lower().encode()).hexdigest()[:32]
    return [f"ip:{client[:64]}", f"user:{user}"]


def _count_attempt(
    conn: sqlite3.Connection, keys: list[str], max_attempts: int, window: int
) -> None:
    """Check the limits and record this attempt in one transaction - before the (slow)
    password check, so parallel requests cannot all pass the check."""
    since = iso(utcnow() - timedelta(seconds=window))
    with write_tx(conn):
        for key in keys:
            limit = max_attempts * (USER_LIMIT_FACTOR if key.startswith("user:") else 1)
            n = conn.execute(
                "SELECT COUNT(*) FROM login_attempts WHERE key=? AND created_at>=?", (key, since)
            ).fetchone()[0]
            if n >= limit:
                raise RateLimited(
                    _("Too many sign-in attempts. Please try again in a few minutes.")
                )
        for key in keys:
            conn.execute(
                "INSERT INTO login_attempts(key, created_at) VALUES(?,?)", (key, now_iso())
            )
        conn.execute(
            "DELETE FROM login_attempts WHERE created_at < ?",
            (iso(utcnow() - timedelta(days=1)),),
        )


def login(
    conn: sqlite3.Connection,
    username: str,
    password: str,
    client: str,
    *,
    max_attempts: int,
    window: int,
    session_hours: int,
) -> tuple[str, str]:
    """Returns (session_token, csrf_token) or raises AuthError/RateLimited."""
    if len(username) > MAX_USERNAME or len(password) > MAX_PASSWORD:
        raise AuthError(_("Wrong user name or password."))
    keys = _rate_keys(username, client)
    _count_attempt(conn, keys, max_attempts, window)
    row = conn.execute(
        "SELECT id, password_hash FROM users WHERE username=?", (username.strip(),)
    ).fetchone()
    # always run one scrypt verification to keep timing similar for unknown users
    ok = verify_password(password, row["password_hash"] if row else _dummy_hash())
    if not row or not ok:
        raise AuthError(_("Wrong user name or password."))
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    with write_tx(conn):
        conn.execute(
            "INSERT INTO sessions(token_hash, user_id, csrf_token, created_at, expires_at) "
            "VALUES(?,?,?,?,?)",
            (
                _sha(token),
                row["id"],
                csrf,
                now_iso(),
                iso(utcnow() + timedelta(hours=session_hours)),
            ),
        )
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now_iso(),))
        for k in keys:
            conn.execute("DELETE FROM login_attempts WHERE key=?", (k,))
    return token, csrf


def session_user(conn: sqlite3.Connection, token: str | None) -> tuple[User, str] | None:
    """(user, csrf_token) for a valid session cookie."""
    if not token:
        return None
    row = conn.execute(
        "SELECT u.id, u.username, s.csrf_token FROM sessions s JOIN users u ON u.id=s.user_id "
        "WHERE s.token_hash=? AND s.expires_at > ?",
        (_sha(token), now_iso()),
    ).fetchone()
    if not row:
        return None
    return User(row["id"], row["username"]), row["csrf_token"]


def logout(conn: sqlite3.Connection, token: str | None) -> None:
    if token:
        with write_tx(conn):
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (_sha(token),))


SCOPES = ("full", "read")  # "read": GET requests only (e.g. the MCP connection to Claude)


def create_api_token(
    conn: sqlite3.Connection, user_id: int, name: str, scope: str = "full"
) -> tuple[int, str]:
    if scope not in SCOPES:
        raise ValueError(scope)
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    with write_tx(conn):
        cur = conn.execute(
            "INSERT INTO api_tokens(user_id, name, token_prefix, token_hash, created_at, scope) "
            "VALUES(?,?,?,?,?,?)",
            (user_id, name.strip()[:100] or "Token", token[:10], _sha(token), now_iso(), scope),
        )
    return int(cur.lastrowid), token  # type: ignore[arg-type]


def token_principal(conn: sqlite3.Connection, token: str | None) -> tuple[User, str] | None:
    """(user, scope) for a valid, not revoked API token."""
    if not token or not token.startswith(TOKEN_PREFIX):
        return None
    row = conn.execute(
        "SELECT t.id AS tid, t.scope, u.id, u.username FROM api_tokens t "
        "JOIN users u ON u.id=t.user_id WHERE t.token_hash=? AND t.revoked_at IS NULL",
        (_sha(token),),
    ).fetchone()
    if not row:
        return None
    with write_tx(conn):
        conn.execute("UPDATE api_tokens SET last_used_at=? WHERE id=?", (now_iso(), row["tid"]))
    return User(row["id"], row["username"]), row["scope"]


def token_user(conn: sqlite3.Connection, token: str | None) -> User | None:
    found = token_principal(conn, token)
    return found[0] if found else None


def list_tokens(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, name, token_prefix, scope, created_at, last_used_at, revoked_at "
        "FROM api_tokens ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def revoke_token(conn: sqlite3.Connection, token_id: int) -> bool:
    with write_tx(conn):
        cur = conn.execute(
            "UPDATE api_tokens SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
            (now_iso(), token_id),
        )
        return cur.rowcount > 0
