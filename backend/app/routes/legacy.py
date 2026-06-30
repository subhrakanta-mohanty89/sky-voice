"""Legacy ``/api/*`` compatibility shim.

The existing web frontend (``frontend/src/services/api.ts``) was written
against the previous Plivo-based backend. This blueprint re-exports those
endpoints on top of the new Twilio call layer so the UI keeps working
without an immediate rewrite.

When you migrate the frontend to use the v1 endpoints directly, this
blueprint can be removed.
"""

from __future__ import annotations

import logging

from flask import Blueprint, request

from app.models import Call, agent_store, call_store
from app.routes.calls import forward_to_number, hangup, hold, unhold
from app.services.auth_service import require_auth
from app.services.realtime import broadcast_event
from app.services.sim_service import resolve_caller_id
from app.services.tenant_context import current_tenant_id
from app.services.twilio_service import make_outbound_call
from app.utils import fail, ok, require_twilio_configured

logger = logging.getLogger(__name__)

legacy_bp = Blueprint("legacy", __name__)


@legacy_bp.post("/make-call")
@require_auth
def legacy_make_call():
    err = require_twilio_configured()
    if err:
        return err

    body = request.get_json(silent=True) or {}
    to = (body.get("to") or "").strip()
    from_number_raw = (body.get("from_number") or body.get("fromNumber") or "").strip()
    if not to:
        return fail("missing_to", status=400)

    tid = current_tenant_id()
    agent_identity = (body.get("agent_identity") or "admin").strip()
    if not agent_store.get(agent_identity, tenant_id=tid):
        return fail("agent_not_registered", status=400)

    caller_id = resolve_caller_id(from_number_raw, tenant_id=tid)
    if not caller_id:
        return fail(
            "no_caller_id_configured",
            status=400,
            hint="Add at least one SIM via the SIM Cards page before placing a call.",
        )

    try:
        call_sid = make_outbound_call(
            to=to, agent_identity=agent_identity, from_number=caller_id, tenant_id=tid,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Legacy make-call failed: %s", exc)
        return fail("outbound_failed", status=502, detail=str(exc))

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

    return ok({
        "call_uuid": call_sid,
        "to": to,
        "from": caller_id,
        "websocket_url": "",  # SDK handles audio directly; see /api/v1/token
        "message": "Call initiated",
    })


@legacy_bp.get("/active-calls")
@require_auth
def legacy_active_calls():
    calls = call_store.list_active(tenant_id=current_tenant_id())
    return ok({"active_calls": [c.to_dict() for c in calls]})


@legacy_bp.get("/call-history")
@require_auth
def legacy_history():
    from app.routes.calls import list_call_history
    return list_call_history()


@legacy_bp.post("/answer-call/<call_sid>")
@legacy_bp.post("/answer-inbound/<call_sid>")
@require_auth
def legacy_answer(call_sid: str):
    """
    Legacy "answer" endpoint. With the Twilio Voice SDK, the agent's
    softphone answers the call directly in the browser/app — there is no
    separate REST step. We just acknowledge and broadcast.
    """
    call = call_store.get(call_sid)
    if not call or call.tenant_id != current_tenant_id():
        return fail("call_not_found", status=404)
    return ok({
        "call_uuid": call_sid,
        "from": call.from_number,
        "type": call.direction,
        "websocket_url": "",
        "message": "Use the Voice SDK on the client to answer the call.",
    })


@legacy_bp.post("/end-call/<call_sid>")
@require_auth
def legacy_end(call_sid: str):
    return hangup(call_sid)


@legacy_bp.post("/hold-call/<call_sid>")
@require_auth
def legacy_hold(call_sid: str):
    return hold(call_sid)


@legacy_bp.post("/unhold-call/<call_sid>")
@require_auth
def legacy_unhold(call_sid: str):
    return unhold(call_sid)


@legacy_bp.post("/forward-call/<call_sid>")
@require_auth
def legacy_forward(call_sid: str):
    return forward_to_number(call_sid)


@legacy_bp.post("/send-message/<call_sid>")
@require_auth
def legacy_send_message(call_sid: str):
    """The transcript/translation feature isn't part of v1.

    This stub keeps the frontend happy — it returns the user's text
    unchanged. Wire up Twilio Media Streams + Deepgram if/when needed.
    """
    body = request.get_json(silent=True) or {}
    text = body.get("text", "")
    return ok({"call_uuid": call_sid, "original": text, "translated": text})
