"""Password hashing + JWT issuance / verification + auth decorators."""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from functools import wraps
from typing import Optional, Tuple

import jwt
from flask import g, request

from app.db import DEFAULT_TENANT_ID
from app.models import User, user_store
from app.utils import fail
from config import settings

logger = logging.getLogger(__name__)

_ALGO = "HS256"
_PBKDF2_ROUNDS = 200_000
_PBKDF2_DIGEST = "sha256"


# --------------------------------------------------------------------------- #
#  Password hashing
# --------------------------------------------------------------------------- #

def hash_password(password: str) -> str:
    """Salted PBKDF2-SHA256 hash, encoded as ``pbkdf2_sha256$rounds$salt$hash``."""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        _PBKDF2_DIGEST, password.encode("utf-8"),
        salt.encode("utf-8"), _PBKDF2_ROUNDS,
    ).hex()
    return f"pbkdf2_{_PBKDF2_DIGEST}${_PBKDF2_ROUNDS}${salt}${digest}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, rounds, salt, expected = encoded.split("$", 3)
    except ValueError:
        return False
    if not scheme.startswith("pbkdf2_"):
        return False
    digest_name = scheme.split("_", 1)[1]
    try:
        rounds_int = int(rounds)
    except ValueError:
        return False
    actual = hashlib.pbkdf2_hmac(
        digest_name, password.encode("utf-8"),
        salt.encode("utf-8"), rounds_int,
    ).hex()
    return hmac.compare_digest(actual, expected)


# --------------------------------------------------------------------------- #
#  JWT
# --------------------------------------------------------------------------- #

def issue_jwt(user: User, *, ttl_seconds: Optional[int] = None) -> Tuple[str, int]:
    """Return (token, exp_epoch)."""
    ttl = ttl_seconds if ttl_seconds is not None else settings.auth_jwt_ttl
    now = int(time.time())
    exp = now + ttl
    payload = {
        "sub": user.id,
        "email": user.email,
        "role": user.role,
        "tenant_id": getattr(user, "tenant_id", None) or DEFAULT_TENANT_ID,
        "iat": now,
        "exp": exp,
    }
    token = jwt.encode(payload, settings.secret_key, algorithm=_ALGO)
    return token, exp


def decode_jwt(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[_ALGO])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


# --------------------------------------------------------------------------- #
#  Decorators
# --------------------------------------------------------------------------- #

def _extract_token() -> Optional[str]:
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    # Allow ?token= for WS upgrades.
    return request.args.get("token") or None


def require_auth(view):
    """Reject the request unless a valid Bearer JWT is present.

    On success, the User object is available as ``g.current_user``.
    """

    @wraps(view)
    def wrapper(*args, **kwargs):
        token = _extract_token()
        if not token:
            return fail("missing_token", status=401)
        payload = decode_jwt(token)
        if not payload:
            return fail("invalid_or_expired_token", status=401)

        user = user_store.get(payload.get("sub", ""))
        if not user:
            return fail("user_not_found", status=401)
        if user.status != "active":
            return fail("user_disabled", status=403)

        # Touch last-seen so admins can see who's online.
        user_store.update(user.id, last_seen_at=time.time())
        g.current_user = user
        g.current_tenant_id = getattr(user, "tenant_id", None) or DEFAULT_TENANT_ID
        return view(*args, **kwargs)

    return wrapper


def require_admin(view):
    """Same as require_auth, but additionally enforces role == 'admin'."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        token = _extract_token()
        if not token:
            return fail("missing_token", status=401)
        payload = decode_jwt(token)
        if not payload:
            return fail("invalid_or_expired_token", status=401)

        user = user_store.get(payload.get("sub", ""))
        if not user:
            return fail("user_not_found", status=401)
        if user.status != "active":
            return fail("user_disabled", status=403)
        if user.role != "admin":
            return fail("admin_only", status=403)

        user_store.update(user.id, last_seen_at=time.time())
        g.current_user = user
        g.current_tenant_id = getattr(user, "tenant_id", None) or DEFAULT_TENANT_ID
        return view(*args, **kwargs)

    return wrapper
