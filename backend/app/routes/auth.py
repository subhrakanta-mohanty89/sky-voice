"""POST /api/v1/token — issue Twilio Voice SDK access tokens.

Web and mobile softphones both fetch a token from this endpoint and pass it
to the Twilio Voice SDK. The token is short-lived (1 h by default).

Requires a valid Bearer JWT (issued by ``/api/v1/auth/login``).
"""

from __future__ import annotations

import logging
import re

from flask import Blueprint, g, request

from app.models import Agent, agent_store
from app.services.auth_service import require_auth
from app.services.twilio_service import issue_access_token
from app.utils import fail, ok, require_twilio_configured

logger = logging.getLogger(__name__)

auth_bp = Blueprint("auth", __name__)

_IDENTITY_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def _identity_for_user(user_id: str) -> str:
    """Voice SDK identities can't have '@' or other chars, so derive a clean one."""
    cleaned = re.sub(r"[^A-Za-z0-9_\-]", "_", user_id)
    return cleaned or "agent"


@auth_bp.post("/token")
@require_auth
def create_token():
    """
    Body: ``{}`` (identity is derived from the logged-in user).
    Returns: ``{ token, identity, expires_in }``.
    """
    err = require_twilio_configured()
    if err:
        return err

    user = g.current_user
    tid = user.tenant_id
    body = request.get_json(silent=True) or {}
    ttl = max(60, min(int(body.get("ttl_seconds") or 3600), 24 * 3600))

    identity = _identity_for_user(user.id)

    # Make sure the agent registry has this identity so inbound routing works.
    # Don't auto-flip an agent who explicitly set ``away`` / ``offline`` back
    # to available — only first-time logins / fresh agents start available.
    if not agent_store.get(identity, tenant_id=tid):
        agent_store.upsert(Agent(
            identity=identity,
            tenant_id=tid,
            name=user.full_name or user.email,
            role="admin" if user.role == "admin" else "agent",
            email=user.email,
            presence="available",
        ))
    else:
        existing = agent_store.get(identity, tenant_id=tid)
        if existing and existing.presence == "offline":
            agent_store.set_presence(identity, "available", tenant_id=tid)
        else:
            # Just bump last_seen so the team page reflects the activity.
            agent_store.set_presence(
                identity,
                existing.presence if existing else "available",
                tenant_id=tid,
            )

    try:
        token = issue_access_token(identity, ttl_seconds=ttl, tenant_id=tid)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to mint access token: %s", exc)
        return fail("token_mint_failed", status=500, detail=str(exc))

    return ok({"token": token, "identity": identity, "expires_in": ttl})
