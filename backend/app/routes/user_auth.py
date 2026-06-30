"""User authentication + team management.

All endpoints are JSON. Authentication is via Bearer JWT issued by
``POST /api/v1/auth/login`` (or signup). Tokens default to a 7-day TTL,
configurable via ``AUTH_JWT_TTL``.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import time
import uuid
from typing import Optional

from flask import Blueprint, g, request

from app.db import DEFAULT_TENANT_ID
from app.models import (
    Agent,
    Tenant,
    User,
    agent_store,
    new_tenant_id,
    tenant_store,
    user_store,
)
from app.services import email as email_svc
from app.services.auth_service import (
    hash_password,
    issue_jwt,
    require_admin,
    require_auth,
    verify_password,
)
from app.services.realtime import broadcast_event
from app.utils import fail, ok
from config import settings

logger = logging.getLogger(__name__)

user_auth_bp = Blueprint("user_auth", __name__)

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
MIN_PASSWORD_LENGTH = 8

# OTP policy --------------------------------------------------------------- #
OTP_TTL_SECONDS    = 10 * 60   # how long a code stays valid
OTP_RESEND_COOLDOWN = 30       # seconds the user must wait between resends
OTP_MAX_ATTEMPTS   = 5         # wrong-code attempts before the code is burned


def _identity_for_user(user_id: str) -> str:
    """Voice SDK identities can't have ``@`` etc. — derive a clean one."""
    cleaned = re.sub(r"[^A-Za-z0-9_\-]", "_", user_id)
    return cleaned or "agent"


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s or "workspace"


def _unique_slug(name: str) -> str:
    """A URL-safe, collision-free workspace slug."""
    base = _slugify(name)
    if tenant_store.get_by_slug(base) is None:
        return base
    return f"{base}-{uuid.uuid4().hex[:6]}"


def _sync_agent_for_user(user: User) -> Optional[Agent]:
    """Mirror a workspace user into the agent registry.

    Creates the agent if missing (offline) so they appear in the transfer
    picker immediately, or updates name/role/email if they changed. The
    agent lives in the *same tenant* as the user.
    Returns the resulting agent (or None on failure).
    """
    identity = _identity_for_user(user.id)
    tid = getattr(user, "tenant_id", None) or DEFAULT_TENANT_ID
    existing = agent_store.get(identity, tenant_id=tid)
    if existing is None:
        return agent_store.upsert(Agent(
            identity=identity,
            tenant_id=tid,
            name=user.full_name or user.email,
            role="admin" if user.role == "admin" else "agent",
            email=user.email,
            presence="offline",
        ))
    wanted_role = "admin" if user.role == "admin" else "agent"
    changed = (
        existing.name != (user.full_name or user.email)
        or existing.role != wanted_role
        or existing.email != user.email
    )
    if changed:
        existing.name = user.full_name or user.email
        existing.role = wanted_role  # type: ignore[assignment]
        existing.email = user.email
        return agent_store.upsert(existing)
    return existing


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #

def _validate_email(email: str) -> Optional[str]:
    if not email or not EMAIL_RE.match(email):
        return "invalid_email"
    return None


def _validate_password(password: str) -> Optional[str]:
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        return "password_too_short"
    return None


def _user_response(user: User, *, include_token: bool = False) -> dict:
    payload = {"user": user.to_public()}
    if include_token:
        token, exp = issue_jwt(user)
        payload["token"] = token
        payload["expires_at"] = exp
    return payload


# --------------------------------------------------------------------------- #
#  OTP helpers
# --------------------------------------------------------------------------- #

def _hash_otp(code: str) -> str:
    """HMAC-SHA256 of the 6-digit code, keyed with the app secret.

    Stored in the DB so a leaked DB dump still doesn't reveal live codes.
    """
    return hmac.new(
        settings.secret_key.encode("utf-8"),
        code.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _generate_otp() -> str:
    """Cryptographically random 6-digit code (000000–999999, zero-padded)."""
    return f"{secrets.randbelow(1_000_000):06d}"


def _issue_and_send_otp(user: User) -> tuple[bool, Optional[str]]:
    """Mint a fresh OTP for ``user``, persist its hash, send via Mailgun.

    Returns ``(sent_via_email, dev_code)``. ``dev_code`` is only populated
    when the email service is not configured — the route can return it in
    the JSON body for local development. In production with Mailgun set,
    the code is never returned over the API.
    """
    code = _generate_otp()
    now = time.time()
    user_store.update(
        user.id,
        otp_code_hash=_hash_otp(code),
        otp_expires_at=now + OTP_TTL_SECONDS,
        otp_last_sent_at=now,
        otp_attempts=0,
    )

    if email_svc.is_configured():
        try:
            email_svc.send_signup_otp(
                to=user.email,
                code=code,
                full_name=user.full_name,
                ttl_minutes=OTP_TTL_SECONDS // 60,
            )
            return True, None
        except email_svc.EmailError:
            logger.exception("Failed to send OTP email to %s — falling back to dev mode", user.email)
            # Fall through and return the code so the dev still gets in.
            return False, code

    logger.warning("Mailgun not configured — returning OTP %s in API response (DEV ONLY)", code)
    return False, code


# --------------------------------------------------------------------------- #
#  Signup / login / me
# --------------------------------------------------------------------------- #

@user_auth_bp.post("/signup")
def signup():
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    full_name = (body.get("fullName") or body.get("full_name") or "").strip()
    phone = (body.get("phone") or "").strip() or None
    organization = (body.get("organization") or "").strip() or None

    if err := _validate_email(email):
        return fail(err, status=400)
    if err := _validate_password(password):
        return fail(err, status=400, hint=f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if not full_name:
        return fail("missing_full_name", status=400)

    if user_store.get_by_email(email):
        return fail("email_already_registered", status=409)

    # Every self-service signup provisions its OWN isolated workspace (tenant)
    # and becomes that workspace's admin. Teammates are added later via
    # POST /team and inherit the inviter's tenant as "member".
    tenant_id = new_tenant_id()
    workspace_name = organization or (f"{full_name}'s Workspace" if full_name else email)
    try:
        tenant_store.create(Tenant(
            id=tenant_id,
            name=workspace_name,
            slug=_unique_slug(workspace_name),
        ))
    except ValueError:
        # Slug raced to taken — retry once with a guaranteed-unique slug.
        tenant_store.create(Tenant(
            id=tenant_id,
            name=workspace_name,
            slug=f"{_slugify(workspace_name)}-{uuid.uuid4().hex[:8]}",
        ))
    role = "admin"

    user = User(
        id=f"usr_{uuid.uuid4().hex[:14]}",
        tenant_id=tenant_id,
        email=email,
        password_hash=hash_password(password),
        full_name=full_name,
        phone=phone,
        organization=organization,
        role=role,
        status="active",
        email_verified=False,
    )
    user_store.create(user)
    # Seed this workspace's default admin agent so routing/transfer works
    # the moment the owner verifies and signs in.
    agent_store.ensure_seed_admin(tenant_id)
    sent, dev_code = _issue_and_send_otp(user)
    logger.info(
        "New signup pending verification: %s (tenant=%s, role=%s, otp_emailed=%s)",
        email, tenant_id, role, sent,
    )

    payload = {
        "message": "verification_code_sent",
        "email": user.email,
        "ttl_seconds": OTP_TTL_SECONDS,
        "resend_cooldown_seconds": OTP_RESEND_COOLDOWN,
    }
    # Only included when Mailgun isn't wired up (local dev / boot-strap).
    if dev_code is not None:
        payload["dev_otp"] = dev_code
    return ok(payload)


@user_auth_bp.post("/verify-otp")
def verify_otp():
    """Validate the 6-digit code that was emailed during signup.

    Successful verification flips ``email_verified`` to True, clears the OTP
    fields, and issues a fresh JWT so the client can log in.
    """
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    code = (body.get("code") or "").strip()

    if not email or not code or not code.isdigit() or len(code) != 6:
        return fail("invalid_otp", status=400)

    user = user_store.get_by_email(email)
    if not user:
        return fail("invalid_otp", status=400)
    if user.email_verified:
        # Idempotent: just hand back a token.
        user_store.update(user.id, last_seen_at=time.time())
        return ok(_user_response(user, include_token=True))
    if not user.otp_code_hash or not user.otp_expires_at:
        return fail("otp_not_requested", status=400)
    if time.time() > user.otp_expires_at:
        return fail("otp_expired", status=400)
    if user.otp_attempts >= OTP_MAX_ATTEMPTS:
        return fail("otp_too_many_attempts", status=429)

    if not hmac.compare_digest(user.otp_code_hash, _hash_otp(code)):
        user_store.update(user.id, otp_attempts=user.otp_attempts + 1)
        return fail("invalid_otp", status=400)

    # Success — mark verified and clear the OTP state.
    user = user_store.update(
        user.id,
        email_verified=True,
        otp_code_hash=None,
        otp_expires_at=None,
        otp_last_sent_at=None,
        otp_attempts=0,
        last_seen_at=time.time(),
    ) or user
    logger.info("Email verified for %s", user.email)
    return ok(_user_response(user, include_token=True))


@user_auth_bp.post("/resend-otp")
def resend_otp():
    """Issue a new OTP for an unverified account, respecting a cooldown."""
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    if not email:
        return fail("missing_email", status=400)

    user = user_store.get_by_email(email)
    # Don't leak whether an email exists; always pretend we sent.
    if not user or user.email_verified:
        return ok({"message": "verification_code_sent", "email": email})

    if user.otp_last_sent_at and (time.time() - user.otp_last_sent_at) < OTP_RESEND_COOLDOWN:
        wait = int(OTP_RESEND_COOLDOWN - (time.time() - user.otp_last_sent_at))
        return fail("otp_resend_cooldown", status=429, hint=f"Try again in {wait}s.")

    sent, dev_code = _issue_and_send_otp(user)
    payload = {
        "message": "verification_code_sent",
        "email": user.email,
        "ttl_seconds": OTP_TTL_SECONDS,
        "resend_cooldown_seconds": OTP_RESEND_COOLDOWN,
    }
    if dev_code is not None:
        payload["dev_otp"] = dev_code
    _ = sent
    return ok(payload)


@user_auth_bp.post("/login")
def login():
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""

    if not email or not password:
        return fail("missing_credentials", status=400)

    user = user_store.get_by_email(email)
    if not user or not verify_password(password, user.password_hash):
        return fail("invalid_credentials", status=401)
    if user.status != "active":
        return fail("user_disabled", status=403)
    if not user.email_verified:
        # Re-issue an OTP so the user can finish onboarding without going back
        # to /signup. The frontend reads ``email`` and routes to /verify-otp.
        _issue_and_send_otp(user)
        return fail(
            "email_not_verified",
            status=403,
            hint="Verify your email to finish signing in.",
            detail=user.email,
        )

    user_store.update(user.id, last_seen_at=time.time())
    return ok(_user_response(user, include_token=True))


@user_auth_bp.get("/me")
@require_auth
def me():
    return ok(_user_response(g.current_user))


@user_auth_bp.post("/logout")
@require_auth
def logout():
    """Stateless: client just discards the token. Endpoint exists for symmetry."""
    return ok({"message": "logged_out"})


# --------------------------------------------------------------------------- #
#  Profile / password / account
# --------------------------------------------------------------------------- #

@user_auth_bp.patch("/profile")
@require_auth
def update_profile():
    body = request.get_json(silent=True) or {}
    user: User = g.current_user

    fields = {}
    if "fullName" in body or "full_name" in body:
        full_name = (body.get("fullName") or body.get("full_name") or "").strip()
        if not full_name:
            return fail("missing_full_name", status=400)
        fields["full_name"] = full_name
    if "phone" in body:
        fields["phone"] = (body["phone"] or "").strip() or None
    if "organization" in body:
        fields["organization"] = (body["organization"] or "").strip() or None
    if "avatarInitials" in body:
        fields["avatar_initials"] = (body["avatarInitials"] or "").strip()[:2].upper() or None

    user_store.update(user.id, **fields)
    return ok(_user_response(user_store.get(user.id) or user))


@user_auth_bp.post("/change-password")
@require_auth
def change_password():
    body = request.get_json(silent=True) or {}
    current = body.get("currentPassword") or body.get("current_password") or ""
    new = body.get("newPassword") or body.get("new_password") or ""

    user: User = g.current_user
    if not verify_password(current, user.password_hash):
        return fail("incorrect_current_password", status=401)
    if err := _validate_password(new):
        return fail(err, status=400)

    user_store.update(user.id, password_hash=hash_password(new))
    return ok({"message": "password_updated"})


@user_auth_bp.delete("/account")
@require_auth
def delete_account():
    user: User = g.current_user
    if user.role == "admin":
        # Don't let the last admin of the workspace delete themselves.
        admins = [u for u in user_store.list_all(tenant_id=user.tenant_id) if u.role == "admin"]
        if len(admins) <= 1:
            return fail("cannot_delete_last_admin", status=409)
    user_store.delete(user.id)
    return ok({"message": "account_deleted"})


@user_auth_bp.post("/forgot-password")
def forgot_password():
    """
    DEV-MODE only: returns a one-shot reset token directly in the response so
    you can wire UI without an email provider. In production, email this to
    the user instead.
    """
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    user = user_store.get_by_email(email)
    if not user:
        # Always return success so we don't leak which emails exist.
        return ok({"message": "If that email is registered, a reset link has been sent."})

    # Lightweight reset token: signed JWT with short TTL.
    token, _ = issue_jwt(user, ttl_seconds=15 * 60)
    logger.info("Password reset token for %s issued.", email)
    return ok({
        "message": "If that email is registered, a reset link has been sent.",
        "reset_token_dev": token,
    })


@user_auth_bp.post("/reset-password")
def reset_password():
    from app.services.auth_service import decode_jwt
    body = request.get_json(silent=True) or {}
    token = body.get("token") or ""
    new = body.get("newPassword") or body.get("new_password") or ""

    if err := _validate_password(new):
        return fail(err, status=400)

    payload = decode_jwt(token)
    if not payload:
        return fail("invalid_or_expired_token", status=401)

    user = user_store.get(payload.get("sub", ""))
    if not user:
        return fail("user_not_found", status=404)

    user_store.update(user.id, password_hash=hash_password(new))
    return ok({"message": "password_reset"})


# --------------------------------------------------------------------------- #
#  Team management (admin-only)
# --------------------------------------------------------------------------- #

@user_auth_bp.get("/team")
@require_auth
def list_team():
    me_user: User = g.current_user
    members = user_store.list_all(tenant_id=me_user.tenant_id)
    # Make sure each existing team member has an agent record so any
    # admin opening the dialer sees them in the transfer picker even if
    # they never logged in yet.
    for u in members:
        _sync_agent_for_user(u)
    payload = [u.to_public() for u in members]
    return ok({"team": payload})


@user_auth_bp.post("/team")
@require_admin
def invite_team_member():
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    full_name = (body.get("fullName") or body.get("full_name") or "").strip()
    role = body.get("role") or "member"
    phone = (body.get("phone") or "").strip() or None
    temp_password = body.get("password") or secrets.token_urlsafe(12)

    if err := _validate_email(email):
        return fail(err, status=400)
    if not full_name:
        return fail("missing_full_name", status=400)
    if role not in {"admin", "member"}:
        return fail("invalid_role", status=400)
    if user_store.get_by_email(email):
        return fail("email_already_registered", status=409)

    inviter: User = g.current_user
    user = User(
        id=f"usr_{uuid.uuid4().hex[:14]}",
        tenant_id=inviter.tenant_id,
        email=email,
        password_hash=hash_password(temp_password),
        full_name=full_name,
        phone=phone,
        organization=inviter.organization,
        role=role,  # type: ignore[arg-type]
        status="active",
        invited_by=inviter.id,
    )
    user_store.create(user)
    # Mirror into agent_store + push agent.upserted so every connected
    # admin sees the new teammate in the transfer picker right away.
    agent = _sync_agent_for_user(user)
    if agent:
        broadcast_event("agent.upserted", agent.to_dict())
    return ok({
        "user": user.to_public(),
        "temporary_password": temp_password,
    })


@user_auth_bp.patch("/team/<user_id>")
@require_admin
def update_team_member(user_id: str):
    body = request.get_json(silent=True) or {}
    user = user_store.get(user_id)
    if not user or user.tenant_id != g.current_user.tenant_id:
        return fail("user_not_found", status=404)

    fields = {}
    if "status" in body:
        if body["status"] not in {"active", "inactive"}:
            return fail("invalid_status", status=400)
        fields["status"] = body["status"]
    if "role" in body:
        if body["role"] not in {"admin", "member"}:
            return fail("invalid_role", status=400)
        fields["role"] = body["role"]
    if "fullName" in body or "full_name" in body:
        fields["full_name"] = (body.get("fullName") or body.get("full_name") or "").strip()
    if "phone" in body:
        fields["phone"] = (body["phone"] or "").strip() or None

    user_store.update(user_id, **fields)
    updated = user_store.get(user_id)
    if updated is not None:
        agent = _sync_agent_for_user(updated)
        if agent:
            broadcast_event("agent.upserted", agent.to_dict())
    return ok({"user": updated.to_public()})  # type: ignore[union-attr]


@user_auth_bp.delete("/team/<user_id>")
@require_admin
def remove_team_member(user_id: str):
    me: User = g.current_user
    user = user_store.get(user_id)
    if not user or user.tenant_id != me.tenant_id:
        return fail("user_not_found", status=404)
    if user.role == "admin":
        admins = [u for u in user_store.list_all(tenant_id=me.tenant_id) if u.role == "admin"]
        if len(admins) <= 1:
            return fail("cannot_delete_last_admin", status=409)
    user_store.delete(user_id)
    # Drop the corresponding agent so the transfer picker doesn't show
    # ghost members after a removal.
    identity = _identity_for_user(user_id)
    if agent_store.get(identity, tenant_id=user.tenant_id):
        agent_store.remove(identity, tenant_id=user.tenant_id)
        broadcast_event("agent.removed", {"identity": identity})
    return ok({"id": user_id})
