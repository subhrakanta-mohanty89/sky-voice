"""Cross-cutting helpers (HTTP responses + Twilio webhook signature check)."""

from __future__ import annotations

import logging
from functools import wraps
from typing import Any, Dict, Optional, Tuple

from flask import Response, jsonify, request
from twilio.request_validator import RequestValidator

from config import settings

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
#  JSON response helpers
# --------------------------------------------------------------------------- #

def ok(data: Optional[Dict[str, Any]] = None, **extra) -> Tuple[Response, int]:
    payload: Dict[str, Any] = {"success": True}
    if data:
        payload.update(data)
    if extra:
        payload.update(extra)
    return jsonify(payload), 200


def fail(error: str, *, status: int = 400, **extra) -> Tuple[Response, int]:
    payload: Dict[str, Any] = {"success": False, "error": error}
    if extra:
        payload.update(extra)
    return jsonify(payload), status


def require_twilio_configured(tenant_id: Optional[str] = None) -> Optional[Tuple[Response, int]]:
    """Returns a 503 tuple if Twilio creds are missing for the tenant, else None.

    Resolves the tenant's effective credentials (tenant columns, falling back
    to ``.env``) so a workspace with its own Twilio account passes even when
    the platform ``.env`` is empty, and vice-versa.
    """
    try:
        from app.services.tenant_context import (
            current_tenant_id,
            get_tenant,
            twilio_creds,
        )
        tid = tenant_id or current_tenant_id()
        configured = twilio_creds(get_tenant(tid)).is_configured()
    except Exception:  # noqa: BLE001 — fall back to the global check
        configured = settings.is_twilio_configured()
    if not configured:
        return fail(
            "twilio_not_configured",
            status=503,
            hint="Add your Twilio credentials in workspace settings or backend/.env.",
        )
    return None


# --------------------------------------------------------------------------- #
#  Twilio webhook signature validator
# --------------------------------------------------------------------------- #

_validators: Dict[str, RequestValidator] = {}


def _validator_for_token(token: Optional[str]) -> Optional[RequestValidator]:
    if not token:
        return None
    validator = _validators.get(token)
    if validator is None:
        validator = RequestValidator(token)
        _validators[token] = validator
    return validator


def _resolve_auth_token() -> Optional[str]:
    """Best-effort auth token of the tenant this webhook belongs to.

    The deployment URL is shared across tenants, but each workspace may sign
    with its own Twilio account. We resolve the tenant from the inbound
    ``CallSid`` (already tracked) or the dialed ``To`` number, then use that
    tenant's effective ``auth_token`` (falling back to the platform ``.env``).
    """
    try:
        from app.db import DEFAULT_TENANT_ID
        from app.services.tenant_context import (
            get_tenant,
            resolve_tenant_id_for_call,
            resolve_tenant_id_for_number,
            twilio_creds,
        )

        tid = DEFAULT_TENANT_ID
        call_sid = request.form.get("CallSid", "")
        if call_sid:
            tid = resolve_tenant_id_for_call(call_sid)
        if tid == DEFAULT_TENANT_ID:
            to = request.form.get("To", "")
            if to:
                tid = resolve_tenant_id_for_number(to)
        creds = twilio_creds(get_tenant(tid))
        if creds.auth_token:
            return creds.auth_token
    except Exception:  # noqa: BLE001 — fall back to the platform token
        pass
    return settings.twilio_auth_token


def verify_twilio_signature(view):
    """Decorator: rejects requests whose ``X-Twilio-Signature`` is invalid."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        if not settings.validate_twilio_signature:
            return view(*args, **kwargs)

        validator = _validator_for_token(_resolve_auth_token())
        if validator is None:
            logger.warning(
                "Skipping Twilio signature validation — TWILIO_AUTH_TOKEN not set."
            )
            return view(*args, **kwargs)

        signature = request.headers.get("X-Twilio-Signature", "")
        # When Twilio POSTs the form fields are the params; the URL must match
        # exactly what Twilio used (i.e. PUBLIC_BASE_URL + path + querystring).
        url = settings.webhook_url(request.path)
        if request.query_string:
            url = f"{url}?{request.query_string.decode()}"
        params = request.form.to_dict()
        if not validator.validate(url, params, signature):
            logger.warning(
                "Rejected Twilio webhook with bad signature. url=%s sig=%s",
                url, signature[:12] + "…",
            )
            return ("Invalid signature", 403)
        return view(*args, **kwargs)

    return wrapper
