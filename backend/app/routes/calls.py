"""REST API for managing calls.

This is the primary surface used by both the web frontend and any mobile
client. The legacy ``/api/*`` shim (see legacy.py) wraps these for
backwards-compat with the existing frontend ``services/api.ts``.
"""

from __future__ import annotations

import logging
from typing import Optional

from flask import Blueprint, g, request

from app.models import Call, agent_store, call_store
from app.services.auth_service import require_auth
from app.services.realtime import broadcast_event
from app.services.sim_service import resolve_caller_id
from app.services.tenant_context import current_tenant_id
from app.services.twilio_service import (
    get_client,
    fetch_call,
    hangup_call,
    make_outbound_call,
    redirect_call,
    twiml_connect_sara,
    twiml_dial_client,
    twiml_dial_number,
    twiml_play_hold_music,
)
from app.utils import fail, ok, require_twilio_configured

logger = logging.getLogger(__name__)

calls_bp = Blueprint("calls", __name__)


def _owned_call(call_sid: str, *, recover: bool = False) -> Optional[Call]:
    """Fetch a call only if it belongs to the caller's tenant (else None).

    CallSids are globally unique, so the store is keyed globally; this guard
    stops one workspace from reading/controlling another's call by guessing
    a SID (IDOR protection).

    With ``recover=True``, a miss in this process's in-memory store falls back
    to Twilio's REST API (see :func:`_recover_call_from_twilio`). Call-control
    endpoints set this so a live call still works after the store was wiped by
    a deploy/restart or created on a different Cloud Run instance.
    """
    call = call_store.get(call_sid)
    if call is not None:
        return call if call.tenant_id == current_tenant_id() else None
    if recover:
        return _recover_call_from_twilio(call_sid)
    return None


def _recover_call_from_twilio(call_sid: str) -> Optional[Call]:
    """Rebuild a :class:`Call` for a live SID missing from the local store.

    The in-memory ``call_store`` is per-process, so a call created on another
    Cloud Run instance — or before the latest deploy restarted this one — is
    invisible here and control endpoints 404. We fetch it from Twilio using
    the *caller's* tenant credentials (so it can only resolve a call in their
    own account — IDOR-safe), then re-seed the store so subsequent control
    calls and status webhooks stay fast and consistent.
    """
    tenant_id = current_tenant_id()
    try:
        info = fetch_call(call_sid, tenant_id=tenant_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Twilio call recovery failed for %s: %s", call_sid, exc)
        return None
    if info is None:
        return None

    call = Call(
        call_sid=info["call_sid"],
        direction=info["direction"],
        from_number=info["from_number"],
        to_number=info["to_number"],
        status=info["status"],
        tenant_id=tenant_id,
        parent_call_sid=info["parent_call_sid"],
    )
    call_store.create(call)
    logger.info("Recovered call %s from Twilio (not in local store)", call_sid)
    return call


def _customer_leg(call: Call) -> Call:
    """Resolve the customer-facing leg to control for an inbound call.

    The browser's Voice SDK hands us the ``<Client>`` *child* leg SID — the
    leg ringing/bridged to the operator. To hand the caller to Sara we must
    act on the **parent** leg (the still-connected customer), exactly like
    the auto-fallback path (``twiml_connect_sara`` is always emitted on the
    customer's call). When the SID we were given has no parent it already IS
    the customer leg (e.g. the admin UI's active-call SID) — return as-is.
    """
    if not call.parent_call_sid:
        return call
    parent = _owned_call(call.parent_call_sid, recover=True)
    return parent or call


# Twilio REST error codes → human-readable hint. Keep this top-level so
# the route function stays under SonarQube's cognitive-complexity budget.
# Reference: https://www.twilio.com/docs/api/errors
_TWILIO_OUTBOUND_HINTS = {
    13223: "Twilio rejected the destination number as invalid.",
    13224: "Twilio rejected the destination number as invalid or unreachable.",
    21210: "The SIM number {caller_id} is not verified as a caller ID on your Twilio account.",
    21212: "The SIM number {caller_id} is not a valid Twilio number or verified caller ID.",
    21214: "The destination is not a valid phone number.",
    21215: "Your Twilio account is not permitted to dial this country. Enable geo-permissions in the Twilio console.",
    21217: "The number {caller_id} isn't a valid 'From' for your account. Add it as a Twilio number or verify it as a caller ID.",
    21219: "The destination is not verified — trial Twilio accounts can only call verified numbers.",
    21606: "The SIM number {caller_id} cannot be used as a caller ID — buy/port a Twilio number or verify yours.",
    21610: "The destination number has unsubscribed from messages from this Twilio number.",
    21611: "This number has reached the international call limit.",
    21614: "The destination is not a valid mobile number.",
}


def _twilio_outbound_hint(exc: Exception, caller_id: str) -> tuple[str, str | None]:
    """Extract (detail, hint) from a Twilio outbound-call exception."""
    code = getattr(exc, "code", None)
    twilio_msg = getattr(exc, "msg", None) or str(exc)
    template = _TWILIO_OUTBOUND_HINTS.get(code) if code else None
    hint = template.format(caller_id=caller_id) if template else None
    if not hint and "trial" in twilio_msg.lower():
        hint = (
            "Trial Twilio accounts can only dial verified caller IDs. "
            "Verify the destination number in the Twilio console first."
        )
    detail = f"Twilio error {code}: {twilio_msg}" if code else twilio_msg
    return detail, hint


# --------------------------------------------------------------------------- #
#  Reads
# --------------------------------------------------------------------------- #

@calls_bp.get("/calls")
@require_auth
def list_active_calls():
    return ok({"active_calls": [
        c.to_dict() for c in call_store.list_active(tenant_id=current_tenant_id())
    ]})


@calls_bp.get("/calls/history")
@require_auth
def list_call_history():
    limit = max(1, min(int(request.args.get("limit", 50)), 500))
    history = call_store.list_history(limit, tenant_id=current_tenant_id())
    return ok({
        "call_history": [_to_history_entry(c) for c in history],
        "total": len(history),
    })


@calls_bp.get("/calls/<call_sid>")
@require_auth
def get_call(call_sid: str):
    call = _owned_call(call_sid)
    if not call:
        return fail("call_not_found", status=404)
    return ok({"call": call.to_dict()})


# --------------------------------------------------------------------------- #
#  Outbound from admin softphone (server-initiated, client-bridged)
# --------------------------------------------------------------------------- #

@calls_bp.post("/calls")
@require_auth
def create_outbound_call():
    """
    Place an outbound call. Body::

        {
          "to": "+14155550100",          # required, E.164
          "from_number": "+16097398989", # optional — which SIM to dial from
          "agent_identity": "admin"      # optional, defaults to "admin"
        }

    The flow: backend tells Twilio to dial the customer; when the customer
    answers, Twilio bridges the agent's Voice SDK softphone in via <Client>.

    The outbound caller ID is resolved in this order:
      1. ``from_number`` from the request body (UI dropdown choice).
      2. The default SIM in the store (or the oldest SIM if none is
         explicitly marked default).
      3. None — error out with ``no_caller_id_configured``. The legacy
         ``TWILIO_CALLER_ID`` env var is **not** consulted; the SIM
         store is the single source of truth.
    """
    err = require_twilio_configured()
    if err:
        return err

    body = request.get_json(silent=True) or {}
    to = (body.get("to") or "").strip()
    from_number_raw = (body.get("from_number") or body.get("fromNumber") or "").strip()
    agent_identity = (body.get("agent_identity") or "admin").strip()
    tid = current_tenant_id()

    if not to:
        return fail("missing_to", status=400, hint="Provide a destination number in E.164 format.")

    if not agent_store.get(agent_identity, tenant_id=tid):
        return fail("agent_not_registered", status=400,
                    hint=f"POST /api/v1/agents to register '{agent_identity}' first.")

    caller_id = resolve_caller_id(from_number_raw, tenant_id=tid)
    if not caller_id:
        return fail(
            "no_caller_id_configured",
            status=400,
            hint="Add at least one SIM via the SIM Cards page before placing a call.",
        )

    try:
        call_sid = make_outbound_call(
            to=to,
            agent_identity=agent_identity,
            from_number=caller_id,
            tenant_id=tid,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to create outbound call: %s", exc)
        detail, hint = _twilio_outbound_hint(exc, caller_id)
        return fail("outbound_failed", status=502, detail=detail, hint=hint)

    call = call_store.create(Call(
        call_sid=call_sid,
        tenant_id=tid,
        direction="outbound",
        from_number=caller_id,
        to_number=to,
        status="initiated",
        agent_identity=agent_identity,
    ))
    broadcast_event("call.initiated", call.to_dict())

    return ok({"call": call.to_dict()})


def request_caller_id(from_number: str = "", *, tenant_id: Optional[str] = None) -> str:
    """Resolve the outbound caller ID, falling back to the default SIM."""
    return resolve_caller_id(from_number, tenant_id=tenant_id) or ""


# --------------------------------------------------------------------------- #
#  Live-call control
# --------------------------------------------------------------------------- #

@calls_bp.post("/calls/<call_sid>/hangup")
@require_auth
def hangup(call_sid: str):
    err = require_twilio_configured()
    if err:
        return err

    call = _owned_call(call_sid, recover=True)
    if not call:
        return fail("call_not_found", status=404)

    try:
        hangup_call(call_sid, tenant_id=call.tenant_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Twilio hangup failed: %s", exc)
        # Continue — the status webhook will reconcile if it eventually fires.

    ended = call_store.end(
        call_sid,
        status="completed",
        ended_by=(g.current_user.full_name or g.current_user.email or "agent"),
    )
    if ended:
        broadcast_event("call.ended", ended.to_dict())
    return ok({"call_sid": call_sid})


@calls_bp.post("/calls/<call_sid>/forward")
@require_auth
def forward_to_number(call_sid: str):
    """
    Forward a live call to an external PSTN number.
    Body: ``{"to": "+14155550100"}``.
    """
    err = require_twilio_configured()
    if err:
        return err

    body = request.get_json(silent=True) or {}
    to = (body.get("to") or "").strip()
    if not to:
        return fail("missing_to", status=400)

    call = _owned_call(call_sid, recover=True)
    if not call:
        return fail("call_not_found", status=404)

    twiml = twiml_dial_number(to, caller_id=call.from_number or request_caller_id(tenant_id=call.tenant_id))
    try:
        redirect_call(call_sid, twiml, tenant_id=call.tenant_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Twilio redirect failed: %s", exc)
        return fail("forward_failed", status=502, detail=str(exc))

    call_store.update(call_sid, agent_identity=None, status="in-progress")
    broadcast_event("call.forwarded", {"call_sid": call_sid, "to": to})
    return ok({"call_sid": call_sid, "forwarded_to": to})


@calls_bp.post("/calls/<call_sid>/transfer")
@require_auth
def transfer_to_agent(call_sid: str):
    """Transfer a live call to another agent's softphone (warm-less, blind)."""
    err = require_twilio_configured()
    if err:
        return err

    body = request.get_json(silent=True) or {}
    target = (body.get("agent_identity") or "").strip()
    if not target:
        return fail("missing_agent_identity", status=400)
    if not agent_store.get(target, tenant_id=current_tenant_id()):
        return fail("agent_not_registered", status=400)

    call = _owned_call(call_sid, recover=True)
    if not call:
        return fail("call_not_found", status=404)

    twiml = twiml_dial_client(target, caller_id=call.from_number or request_caller_id(tenant_id=call.tenant_id))
    try:
        redirect_call(call_sid, twiml, tenant_id=call.tenant_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Transfer redirect failed: %s", exc)
        return fail("transfer_failed", status=502, detail=str(exc))

    call_store.update(call_sid, agent_identity=target)
    broadcast_event("call.transferred", {"call_sid": call_sid, "to_agent": target})
    return ok({"call_sid": call_sid, "transferred_to": target})


@calls_bp.post("/calls/<call_sid>/transfer-to-sara")
@require_auth
def transfer_to_sara(call_sid: str):
    """Hand a live call off to the Sara AI receptionist.

    The current agent (admin or otherwise) drops out of the bridge; Twilio
    redirects the still-connected caller into a ``<Connect><Stream>`` that
    talks to our Media Streams WS handler.
    """
    from app.services.tenant_context import get_tenant, sara_config

    err = require_twilio_configured()
    if err:
        return err

    call = _owned_call(call_sid, recover=True)
    if not call:
        return fail("call_not_found", status=404)

    if not sara_config(get_tenant(call.tenant_id)).is_configured():
        return fail("sara_not_configured", status=503,
                    detail="DEEPGRAM_API_KEY is not set for this workspace.")

    # Sara must connect to the still-connected CALLER (the parent leg), not
    # the operator's <Client> child leg that the browser handed us — that
    # leg is ringing/bridged to the agent and can't be redirected to the
    # caller's media stream (which is why a transfer of a still-ringing call
    # was failing). This mirrors the auto-fallback path, which always emits
    # the Sara stream on the customer's call.
    customer = _customer_leg(call)
    target_sid = customer.call_sid

    from_agent_label = ""
    user = getattr(g, "current_user", None)
    if user is not None:
        from_agent_label = (
            getattr(user, "full_name", None)
            or getattr(user, "email", None)
            or getattr(user, "identity", "")
            or ""
        )

    twiml = twiml_connect_sara(
        target_sid,
        from_number=customer.from_number or "",
        to_number=customer.to_number or "",
        from_agent=from_agent_label or None,
    )
    try:
        redirect_call(target_sid, twiml, tenant_id=call.tenant_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Transfer-to-Sara redirect failed: %s", exc)
        return fail("transfer_failed", status=502, detail=str(exc))

    call_store.update(target_sid, agent_identity="sara")
    broadcast_event(
        "call.transferred",
        {"call_sid": target_sid, "to_agent": "sara", "sara": True},
    )
    return ok({"call_sid": target_sid, "transferred_to": "sara"})


@calls_bp.post("/calls/<call_sid>/hold")
@require_auth
def hold(call_sid: str):
    err = require_twilio_configured()
    if err:
        return err

    call = _owned_call(call_sid, recover=True)
    if not call:
        return fail("call_not_found", status=404)

    # Find the customer's leg (the child call when admin is bridged in).
    child = call_store.find_by_parent(call_sid)
    target_sid = child.call_sid if child else call_sid

    try:
        redirect_call(target_sid, twiml_play_hold_music(), tenant_id=call.tenant_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Hold failed: %s", exc)
        return fail("hold_failed", status=502, detail=str(exc))

    import time as _t
    call_store.update(call_sid, is_on_hold=True, hold_started_at=_t.time())
    broadcast_event("call.held", {"call_sid": call_sid})
    return ok({"call_sid": call_sid, "is_on_hold": True})


@calls_bp.post("/calls/<call_sid>/unhold")
@require_auth
def unhold(call_sid: str):
    err = require_twilio_configured()
    if err:
        return err

    call = _owned_call(call_sid, recover=True)
    if not call:
        return fail("call_not_found", status=404)
    if not call.agent_identity:
        return fail("no_agent_to_resume", status=409,
                    hint="Cannot unhold a call without a target agent.")

    child = call_store.find_by_parent(call_sid)
    target_sid = child.call_sid if child else call_sid

    twiml = twiml_dial_client(call.agent_identity, caller_id=call.from_number or request_caller_id(tenant_id=call.tenant_id))
    try:
        redirect_call(target_sid, twiml, tenant_id=call.tenant_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unhold failed: %s", exc)
        return fail("unhold_failed", status=502, detail=str(exc))

    call_store.update(call_sid, is_on_hold=False, hold_started_at=None)
    broadcast_event("call.unheld", {"call_sid": call_sid})
    return ok({"call_sid": call_sid, "is_on_hold": False})


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #

def _to_history_entry(call: Call) -> dict:
    """Shape a finished call for the frontend's CallHistoryEntry contract."""
    return {
        "call_uuid": call.call_sid,
        "type": call.direction,
        "direction": call.direction,
        "from": call.from_number,
        "to": call.to_number,
        "status": call.status,
        "timestamp": _epoch_to_iso(call.ended_at or call.started_at),
        "ended_by": call.ended_by or "system",
        "duration_seconds": call.duration_seconds,
        "recording_url": call.recording_url,
    }


def _epoch_to_iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
