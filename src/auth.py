# -*- coding: utf-8 -*-
"""
Web admin authentication module.

Single toggle (ADMIN_AUTH_ENABLED) + file-based credentials.
First login sets initial password; supports web change-password and CLI reset.
"""

from __future__ import annotations

import base64
import contextvars
import getpass
import hashlib
import hmac
import logging
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

from dotenv import dotenv_values

logger = logging.getLogger(__name__)

COOKIE_NAME = "dsa_session"
PBKDF2_ITERATIONS = 100_000
RATE_LIMIT_WINDOW_SEC = 300
RATE_LIMIT_MAX_FAILURES = 5
SESSION_MAX_AGE_HOURS_DEFAULT = 24
MIN_PASSWORD_LEN = 6
MAX_PASSWORD_LEN = 16
_current_user_ctx: contextvars.ContextVar[dict | None] = contextvars.ContextVar("dsa_current_user", default=None)

# Lazy-loaded state
_auth_enabled: Optional[bool] = None
_session_secret: Optional[bytes] = None
_password_hash_salt: Optional[bytes] = None
_password_hash_stored: Optional[bytes] = None
_rate_limit: dict[str, Tuple[int, float]] = {}
_rate_limit_lock = None


def _get_lock():
    """Lazy init threading lock for rate limit dict."""
    global _rate_limit_lock
    if _rate_limit_lock is None:
        import threading
        _rate_limit_lock = threading.Lock()
    return _rate_limit_lock


def _ensure_env_loaded() -> None:
    """Ensure .env is loaded before reading config."""
    from src.config import setup_env
    setup_env()


def _get_data_dir() -> Path:
    """Return DATA_DIR as parent of DATABASE_PATH."""
    db_path = os.getenv("DATABASE_PATH", "./data/stock_analysis.db")
    return Path(db_path).resolve().parent


def _get_credential_path() -> Path:
    """Path to stored password hash file."""
    return _get_data_dir() / ".admin_password_hash"


def _is_auth_enabled_from_env() -> bool:
    """Read ADMIN_AUTH_ENABLED from .env file."""
    _ensure_env_loaded()
    env_file = os.getenv("ENV_FILE")
    env_path = Path(env_file) if env_file else Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return False
    values = dotenv_values(env_path)
    val = (values.get("ADMIN_AUTH_ENABLED") or "").strip().lower()
    return val in ("true", "1", "yes")


def rotate_session_secret() -> bool:
    """Rotate the session signing secret to invalidate all active sessions."""
    global _session_secret
    data_dir = _get_data_dir()
    secret_path = data_dir / ".session_secret"
    data_dir.mkdir(parents=True, exist_ok=True)
    new_secret = secrets.token_bytes(32)
    try:
        tmp_path = secret_path.with_suffix(".tmp")
        tmp_path.write_bytes(new_secret)
        tmp_path.chmod(0o600)
        tmp_path.replace(secret_path)
        _session_secret = new_secret
        logger.info("Session secret rotated successfully")
        return True
    except OSError as e:
        logger.error("Failed to rotate .session_secret: %s", e)
        return False


def _load_session_secret() -> Optional[bytes]:
    """Load or create session secret."""
    global _session_secret
    if _session_secret is not None:
        return _session_secret

    data_dir = _get_data_dir()
    secret_path = data_dir / ".session_secret"

    try:
        if secret_path.exists():
            _session_secret = secret_path.read_bytes()
            if len(_session_secret) != 32:
                logger.warning("Invalid .session_secret length, regenerating")
                _session_secret = None
                if rotate_session_secret():
                    return _session_secret
                return None
            return _session_secret

        data_dir.mkdir(parents=True, exist_ok=True)
        new_secret = secrets.token_bytes(32)
        try:
            with open(secret_path, "xb") as f:
                f.write(new_secret)
            secret_path.chmod(0o600)
        except FileExistsError:
            _session_secret = secret_path.read_bytes()
        else:
            _session_secret = new_secret
        return _session_secret
    except OSError as e:
        logger.error("Failed to create or read .session_secret: %s", e)
        return None


def _parse_password_hash(value: str) -> Optional[Tuple[bytes, bytes]]:
    """Parse salt_b64:hash_b64. Returns (salt, hash) or None."""
    if not value or ":" not in value:
        return None
    parts = value.strip().split(":", 1)
    if len(parts) != 2:
        return None
    try:
        salt_b64, hash_b64 = parts[0].strip(), parts[1].strip()
        salt = base64.standard_b64decode(salt_b64)
        stored_hash = base64.standard_b64decode(hash_b64)
        if salt and stored_hash:
            return (salt, stored_hash)
    except (ValueError, TypeError):
        pass
    return None


def _verify_password_hash(submitted: str, salt: bytes, stored_hash: bytes) -> bool:
    """Verify submitted password against stored pbkdf2 hash."""
    computed = hashlib.pbkdf2_hmac(
        "sha256",
        submitted.encode("utf-8"),
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    return hmac.compare_digest(computed, stored_hash)


def _load_credential_from_file() -> bool:
    """Load credential from file into module globals. Returns True if loaded."""
    global _password_hash_salt, _password_hash_stored

    path = _get_credential_path()
    if not path.exists():
        _password_hash_salt = None
        _password_hash_stored = None
        return False

    try:
        raw = path.read_text().strip()
        parsed = _parse_password_hash(raw)
        if parsed is None:
            logger.warning("Invalid .admin_password_hash format, ignoring")
            return False
        _password_hash_salt, _password_hash_stored = parsed
        return True
    except OSError as e:
        logger.error("Failed to read credential file: %s", e)
        return False


def refresh_auth_state() -> None:
    """Reload auth-related state from disk and env."""
    global _auth_enabled, _session_secret
    _auth_enabled = None
    _session_secret = None
    _load_credential_from_file()


def is_auth_enabled() -> bool:
    """Return whether admin authentication is enabled (ADMIN_AUTH_ENABLED=true)."""
    global _auth_enabled
    if _auth_enabled is not None:
        return _auth_enabled
    _auth_enabled = _is_auth_enabled_from_env()
    return _auth_enabled


def has_stored_password() -> bool:
    """Return whether a valid stored password hash exists on disk."""
    return _load_credential_from_file()


def verify_stored_password(password: str) -> bool:
    """Verify password against stored credential even when auth is disabled."""
    if not has_stored_password():
        return False
    return _verify_password_hash(password, _password_hash_salt, _password_hash_stored)


def is_password_set() -> bool:
    """Return whether initial password has been set (credential file exists and valid)."""
    if not is_auth_enabled():
        return False
    return has_stored_password()


def is_password_changeable() -> bool:
    """Return whether password can be changed via web/CLI (always True when auth enabled)."""
    return is_auth_enabled()


def _get_session_secret() -> Optional[bytes]:
    """Return session signing secret."""
    if not is_auth_enabled():
        return None
    return _load_session_secret()


def _validate_password(pwd: str) -> Optional[str]:
    """Return error message if invalid, None if valid."""
    if not pwd or not pwd.strip():
        return "密码不能为空"
    if len(pwd) < MIN_PASSWORD_LEN:
        return f"密码至少 {MIN_PASSWORD_LEN} 位"
    return None



def get_current_user_context() -> Optional[dict]:
    """Return the current request user context if set."""
    return _current_user_ctx.get()


def set_current_user_context(user: Optional[dict]):
    """Set current request user context and return the reset token."""
    return _current_user_ctx.set(user)


def reset_current_user_context(token) -> None:
    """Reset current request user context."""
    _current_user_ctx.reset(token)


def _validate_username(username: str) -> str:
    username = (username or "").strip()
    if not username:
        raise ValueError("用户名不能为空")
    if len(username) > 64:
        raise ValueError("用户名不能超过 64 位")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.")
    if any(ch not in allowed for ch in username):
        raise ValueError("用户名只能包含字母、数字、下划线、中划线和点")
    return username


def _validate_account_password(password: str) -> None:
    err = _validate_password(password)
    if err:
        raise ValueError(err)
    if len(password) > MAX_PASSWORD_LEN:
        raise ValueError(f"密码不能超过 {MAX_PASSWORD_LEN} 位")
    if password.isdigit():
        raise ValueError("密码不能为纯数字")


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(32)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt=salt, iterations=PBKDF2_ITERATIONS)
    return f"{base64.standard_b64encode(salt).decode('ascii')}:{base64.standard_b64encode(derived).decode('ascii')}"


def create_user(username: str, password: str, role: str = "user"):
    """Create an application user with a hashed password."""
    from src.storage import AppUser, DatabaseManager

    username = _validate_username(username)
    role = "admin" if role == "admin" else "user"
    _validate_account_password(password)
    db = DatabaseManager.get_instance()
    with db.get_session() as session:
        existing = session.query(AppUser).filter(AppUser.username == username).first()
        if existing:
            raise ValueError("用户名已存在")
        user = AppUser(username=username, password_hash=_hash_password(password), role=role, status="active")
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


def get_user_by_username(username: str):
    from src.storage import AppUser, DatabaseManager

    username = (username or "").strip()
    if not username:
        return None
    db = DatabaseManager.get_instance()
    with db.get_session() as session:
        return session.query(AppUser).filter(AppUser.username == username).first()


def get_user_by_id(user_id: int):
    from src.storage import AppUser, DatabaseManager

    db = DatabaseManager.get_instance()
    with db.get_session() as session:
        return session.query(AppUser).filter(AppUser.id == int(user_id)).first()


def verify_user_password(username: str, password: str) -> bool:
    user = get_user_by_username(username)
    if not user or user.status != "active":
        return False
    parsed = _parse_password_hash(user.password_hash)
    if not parsed:
        return False
    return _verify_password_hash(password, parsed[0], parsed[1])


def authenticate_user(username: str, password: str) -> Optional[dict]:
    user = get_user_by_username(username)
    if not user or user.status != "active":
        return None
    parsed = _parse_password_hash(user.password_hash)
    if not parsed or not _verify_password_hash(password, parsed[0], parsed[1]):
        return None
    try:
        from src.storage import DatabaseManager
        db = DatabaseManager.get_instance()
        with db.get_session() as session:
            db_user = session.query(type(user)).filter(type(user).id == user.id).first()
            if db_user:
                db_user.last_login_at = datetime_now()
                session.commit()
    except Exception:
        logger.debug("Failed to update last_login_at", exc_info=True)
    return {"id": user.id, "username": user.username, "role": user.role}


def datetime_now():
    from datetime import datetime
    return datetime.now()


def create_registration_captcha() -> dict:
    """Create a signed arithmetic captcha challenge."""
    secret = _load_session_secret() or secrets.token_bytes(32)
    a = secrets.randbelow(8) + 2
    b = secrets.randbelow(8) + 2
    answer = str(a + b)
    ts = str(int(time.time()))
    nonce = secrets.token_urlsafe(8)
    payload = f"{answer}.{ts}.{nonce}"
    sig = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    token = base64.urlsafe_b64encode(f"{payload}.{sig}".encode("utf-8")).decode("ascii")
    return {"question": f"{a} + {b} = ?", "captchaToken": token, "answer": answer}


def verify_registration_captcha(token: str, answer: str, max_age_seconds: int = 600) -> bool:
    secret = _load_session_secret() or secrets.token_bytes(32)
    try:
        raw = base64.urlsafe_b64decode((token or "").encode("ascii")).decode("utf-8")
        expected_answer, ts_str, nonce, sig = raw.split(".", 3)
        payload = f"{expected_answer}.{ts_str}.{nonce}"
        expected_sig = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return False
        if time.time() - int(ts_str) > max_age_seconds:
            return False
        return str(answer or "").strip() == expected_answer
    except Exception:
        return False


def is_registration_enabled() -> bool:
    """Return whether public registration is enabled."""
    value = (os.getenv("DSA_REGISTRATION_ENABLED") or "true").strip().lower()
    return value in {"true", "1", "yes", "on"}


def get_registration_invite_code() -> str:
    """Return configured registration invite code, if any."""
    return (os.getenv("DSA_REGISTRATION_INVITE_CODE") or "").strip()


def is_registration_invite_required() -> bool:
    """Return whether registration requires invite code."""
    return bool(get_registration_invite_code())


def ensure_admin_user(username: Optional[str] = None, password: Optional[str] = None) -> None:
    """Ensure the configured admin user exists and matches the configured password.

    This staging deployment intentionally seeds the primary admin from server-side env.
    If the configured admin already exists, keep it active/admin and rotate its
    password to the configured value so staging can be recovered predictably.
    """
    username = (username or os.getenv("DSA_ADMIN_USERNAME") or "").strip()
    password = (password or os.getenv("DSA_ADMIN_PASSWORD") or "").strip()
    if not username or not password:
        return
    try:
        _validate_account_password(password)
        from src.storage import AppUser, DatabaseManager
        db = DatabaseManager.get_instance()
        with db.get_session() as session:
            user = session.query(AppUser).filter(AppUser.username == username).first()
            if user:
                user.role = "admin"
                user.status = "active"
                user.password_hash = _hash_password(password)
            else:
                session.add(AppUser(username=username, password_hash=_hash_password(password), role="admin", status="active"))
            session.commit()
    except Exception as exc:
        logger.warning("Failed to ensure configured admin user %s: %s", username, exc)


def list_app_users() -> list[dict]:
    """Return users without password hashes for admin management UI."""
    from src.storage import AppUser, DatabaseManager
    db = DatabaseManager.get_instance()
    with db.get_session() as session:
        users = session.query(AppUser).order_by(AppUser.created_at.desc(), AppUser.id.desc()).all()
        return [
            {
                "id": user.id,
                "username": user.username,
                "role": user.role,
                "status": user.status,
                "created_at": user.created_at.isoformat() if user.created_at else None,
                "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
            }
            for user in users
        ]


def update_app_user(user_id: int, *, role: Optional[str] = None, status: Optional[str] = None, password: Optional[str] = None) -> dict:
    """Update a user account and return its public representation."""
    from src.storage import AppUser, DatabaseManager
    db = DatabaseManager.get_instance()
    with db.get_session() as session:
        user = session.query(AppUser).filter(AppUser.id == int(user_id)).first()
        if not user:
            raise ValueError("用户不存在")
        if role is not None:
            normalized_role = (role or "").strip().lower()
            if normalized_role not in {"admin", "user"}:
                raise ValueError("角色只能是 admin 或 user")
            user.role = normalized_role
        if status is not None:
            normalized_status = (status or "").strip().lower()
            if normalized_status not in {"active", "disabled"}:
                raise ValueError("状态只能是 active 或 disabled")
            user.status = normalized_status
        if password is not None and str(password).strip():
            _validate_account_password(str(password).strip())
            user.password_hash = _hash_password(str(password).strip())
        session.commit()
        session.refresh(user)
        return {
            "id": user.id,
            "username": user.username,
            "role": user.role,
            "status": user.status,
            "created_at": user.created_at.isoformat() if user.created_at else None,
            "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        }

def set_initial_password(password: str) -> Optional[str]:
    """
    Set initial password (first-time setup). Returns error message or None on success.
    Atomic write with 0o600 permissions.
    """
    err = _validate_password(password)
    if err:
        return err

    data_dir = _get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    cred_path = _get_credential_path()

    salt = secrets.token_bytes(32)
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    salt_b64 = base64.standard_b64encode(salt).decode("ascii")
    hash_b64 = base64.standard_b64encode(derived).decode("ascii")
    content = f"{salt_b64}:{hash_b64}"

    try:
        tmp_path = cred_path.with_suffix(".tmp")
        tmp_path.write_text(content)
        tmp_path.chmod(0o600)
        tmp_path.replace(cred_path)
        _load_credential_from_file()
        return None
    except OSError as e:
        logger.error("Failed to write credential file: %s", e)
        return "密码保存失败"


def verify_password(password: str) -> bool:
    """Verify password against stored credential. Constant-time where applicable."""
    if not is_auth_enabled():
        return True
    return verify_stored_password(password)


def change_password(current: str, new: str) -> Optional[str]:
    """
    Change password. Verifies current, writes new hash. Returns error message or None on success.
    """
    if not is_auth_enabled():
        return "认证功能未启用"
    if not is_password_set():
        return "尚未设置密码"

    if not current or not current.strip():
        return "请输入当前密码"
    if not _verify_password_hash(current, _password_hash_salt, _password_hash_stored):
        return "当前密码错误"

    err = _validate_password(new)
    if err:
        return err

    cred_path = _get_credential_path()
    salt = secrets.token_bytes(32)
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        new.encode("utf-8"),
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    salt_b64 = base64.standard_b64encode(salt).decode("ascii")
    hash_b64 = base64.standard_b64encode(derived).decode("ascii")
    content = f"{salt_b64}:{hash_b64}"

    try:
        tmp_path = cred_path.with_suffix(".tmp")
        tmp_path.write_text(content)
        tmp_path.chmod(0o600)
        tmp_path.replace(cred_path)
        # Reload into memory so subsequent verify_password uses new hash
        _load_credential_from_file()
        return None
    except OSError as e:
        logger.error("Failed to write credential file: %s", e)
        return "密码保存失败"


def create_session(user: Optional[dict] = None) -> str:
    """Create a signed session payload. Format: nonce.ts.user_id.role.signature."""
    secret = _get_session_secret()
    if not secret:
        return ""
    nonce = secrets.token_urlsafe(32)
    ts = str(int(time.time()))
    if not user:
        payload = f"{nonce}.{ts}"
        sig = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{payload}.{sig}"
    user_id = str((user or {}).get("id", "0"))
    role = str((user or {}).get("role", "admin"))
    username = base64.urlsafe_b64encode(str((user or {}).get("username", "admin")).encode("utf-8")).decode("ascii").rstrip("=")
    payload = f"{nonce}.{ts}.{user_id}.{role}.{username}"
    sig = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def get_session_user(value: str) -> Optional[dict]:
    """Verify session cookie and return user context."""
    secret = _get_session_secret()
    if not secret or not value:
        return None
    value = (value or "").strip().strip('"')
    parts = value.split(".")
    if len(parts) == 3:
        # Legacy admin-only cookie format.
        nonce, ts_str, sig = parts[0], parts[1], parts[2]
        payload = f"{nonce}.{ts_str}"
        user_ctx = {"id": None, "username": "admin", "role": "admin"}
    elif len(parts) == 6:
        nonce, ts_str, user_id_str, role, username_b64, sig = parts
        payload = f"{nonce}.{ts_str}.{user_id_str}.{role}.{username_b64}"
        try:
            padded_username = username_b64 + ("=" * (-len(username_b64) % 4))
            username = base64.urlsafe_b64decode(padded_username.encode("ascii")).decode("utf-8")
        except Exception:
            username = ""
        try:
            user_id = int(user_id_str)
        except ValueError:
            user_id = None
        user_ctx = {"id": user_id, "username": username, "role": role}
    else:
        return None
    expected = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        ts = int(ts_str)
    except ValueError:
        return None
    try:
        max_age_hours = int(os.getenv("ADMIN_SESSION_MAX_AGE_HOURS", str(SESSION_MAX_AGE_HOURS_DEFAULT)))
    except ValueError:
        max_age_hours = SESSION_MAX_AGE_HOURS_DEFAULT
    if time.time() - ts > max_age_hours * 3600:
        return None
    return user_ctx


def verify_session(value: str) -> bool:
    """Verify session cookie and check expiry."""
    return get_session_user(value) is not None

def get_client_ip(request) -> str:
    """Get client IP, respecting TRUST_X_FORWARDED_FOR.

    When behind a single trusted reverse proxy, the proxy appends the real
    client IP as the rightmost entry in X-Forwarded-For.  We use [-1] instead
    of [0] so that an attacker cannot spoof an arbitrary leftmost value to
    rotate rate-limit buckets and bypass brute-force protection.
    """
    if os.getenv("TRUST_X_FORWARDED_FOR", "false").lower() == "true":
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[-1].strip()
    if request.client:
        return request.client.host or "127.0.0.1"
    return "127.0.0.1"


def check_rate_limit(ip: str) -> bool:
    """Return True if under limit, False if rate limited."""
    lock = _get_lock()
    now = time.time()
    with lock:
        expired_keys = [k for k, (_, ts) in _rate_limit.items() if now - ts > RATE_LIMIT_WINDOW_SEC]
        for k in expired_keys:
            del _rate_limit[k]
        if ip in _rate_limit:
            count, first_ts = _rate_limit[ip]
            if count >= RATE_LIMIT_MAX_FAILURES:
                return False
        return True


def record_login_failure(ip: str) -> None:
    """Record a failed login attempt for rate limiting."""
    lock = _get_lock()
    now = time.time()
    with lock:
        if ip in _rate_limit:
            count, first_ts = _rate_limit[ip]
            if now - first_ts > RATE_LIMIT_WINDOW_SEC:
                _rate_limit[ip] = (1, now)
            else:
                _rate_limit[ip] = (count + 1, first_ts)
        else:
            _rate_limit[ip] = (1, now)


def clear_rate_limit(ip: str) -> None:
    """Clear rate limit for IP after successful login."""
    lock = _get_lock()
    with lock:
        _rate_limit.pop(ip, None)


def overwrite_password(new_password: str) -> Optional[str]:
    """
    Overwrite stored password without verifying current. For CLI reset only.
    Returns error message or None on success.
    """
    if not is_auth_enabled():
        return "认证功能未启用"
    err = _validate_password(new_password)
    if err:
        return err

    data_dir = _get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    cred_path = _get_credential_path()

    salt = secrets.token_bytes(32)
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        new_password.encode("utf-8"),
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    salt_b64 = base64.standard_b64encode(salt).decode("ascii")
    hash_b64 = base64.standard_b64encode(derived).decode("ascii")
    content = f"{salt_b64}:{hash_b64}"

    try:
        tmp_path = cred_path.with_suffix(".tmp")
        tmp_path.write_text(content)
        tmp_path.chmod(0o600)
        tmp_path.replace(cred_path)
        _load_credential_from_file()
        return None
    except OSError as e:
        logger.error("Failed to write credential file: %s", e)
        return "密码保存失败"


def reset_password_cli() -> int:
    """Interactive CLI to reset password. Returns exit code."""
    _ensure_env_loaded()
    if not _is_auth_enabled_from_env():
        print("Error: Auth is not enabled. Set ADMIN_AUTH_ENABLED=true in .env", file=sys.stderr)
        return 1

    print("Enter new admin password (will not echo):", end=" ")
    pwd = getpass.getpass("")
    err = _validate_password(pwd)
    if err:
        print(f"Error: {err}", file=sys.stderr)
        return 1

    print("Confirm new password:", end=" ")
    pwd2 = getpass.getpass("")
    if pwd != pwd2:
        print("Error: Passwords do not match", file=sys.stderr)
        return 1

    err = overwrite_password(pwd)
    if err:
        print(f"Error: {err}", file=sys.stderr)
        return 1

    print("Password has been reset successfully.")
    return 0


def _main() -> int:
    """CLI entry: reset_password subcommand."""
    if len(sys.argv) > 1 and sys.argv[1] == "reset_password":
        return reset_password_cli()
    print("Usage: python -m src.auth reset_password", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(_main())
