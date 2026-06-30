"""SIM (outbound caller-ID) routes.

CRUD around :data:`app.models.sim_store` plus a "sync from Twilio"
endpoint that pulls every IncomingPhoneNumber in the workspace's Twilio
account into the local store.

All endpoints require an authenticated user; mutating endpoints require
an admin role (mirrors the agent management policy).
"""

from __future__ import annotations

import logging
import re

from flask import Blueprint, request

from app.models import Sim, new_sim_id, sim_store
from app.services import sim_service
from app.services.auth_service import require_admin, require_auth
from app.services.tenant_context import current_tenant_id
from app.utils import fail, ok

logger = logging.getLogger(__name__)

sims_bp = Blueprint("sims", __name__)

E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")


@sims_bp.get("/sims")
@require_auth
def list_sims():
    """Return every SIM owned by the caller's workspace."""
    items = [s.to_dict() for s in sim_store.list_all(tenant_id=current_tenant_id())]
    return ok({"sims": items})


@sims_bp.post("/sims")
@require_admin
def create_sim():
    """Add a new outbound caller ID.

    Body: ``{"phoneNumber": "+16097398989", "label": "Main Line",
             "isDefault": false}``

    Twilio enforces that every outbound caller ID is either an
    IncomingPhoneNumber the workspace owns OR an entry in
    OutgoingCallerIds. This endpoint detects which case applies:

      * **Owned** number → inserted with ``source="twilio"``, status 200.
      * **Already-verified** external number → inserted with
        ``source="verified"``, status 200.
      * **Unknown** number → starts Twilio's caller-ID verification
        (Twilio voice-calls the number with a 6-digit code) and returns
        ``{verificationRequired: true, validationCode, phoneNumber,
        message}`` so the UI can prompt the user to type the code on
        their phone keypad. The SIM is **not** stored yet — the caller
        must hit ``POST /sims/verify-complete`` after entering the code.
    """
    body = request.get_json(silent=True) or {}
    phone = (body.get("phoneNumber") or body.get("phone_number") or "").strip()
    label = (body.get("label") or "").strip() or phone
    is_default = bool(body.get("isDefault") or body.get("is_default"))

    if not phone:
        return fail("phone_number_required", status=400)
    if not E164_RE.match(phone):
        return fail("phone_number_must_be_e164", status=400)

    # Already in the store? (do not overwrite other tenants)
    if sim_store.get_by_number(phone):
        return fail("phone_number_already_added", status=409)

    classification = sim_service.classify_number(phone)


    if classification == "unknown":
        # Need to verify — call Twilio's validation API and return the
        # code the UI will display while Twilio dials the number.
        try:
            verify = sim_service.start_verification(phone, friendly_name=label)
        except RuntimeError as exc:
            msg = str(exc)
            if msg == "twilio_not_configured":
                return fail("twilio_not_configured", status=400)
            return fail("twilio_verification_failed", message=msg, status=502)
        return ok({
            "verificationRequired": True,
            "validationCode": verify["validation_code"],
            "phoneNumber": verify["phone_number"],
            "label": label,
            "isDefault": is_default,
            "message": (
                f"Twilio is calling {verify['phone_number']} now. "
                f"When prompted, enter this code on your phone keypad: "
                f"{verify['validation_code']}"
            ),
        })

    # Owned or already-verified → insert straight into the store.
    source = "twilio" if classification == "owned" else "verified"
    try:
        sim = sim_store.create(
            Sim(
                id=new_sim_id(),
                tenant_id=current_tenant_id(),
                phone_number=phone,
                label=label,
                is_default=is_default,
                source=source,
            )
        )
    except ValueError as exc:
        if str(exc) == "duplicate_phone_number":
            return fail("phone_number_already_added", status=409)
        raise
    return ok({"sim": sim.to_dict()})


@sims_bp.post("/sims/verify-complete")
@require_admin
def complete_sim_verification():
    """Confirm a verification round-trip.

    Body: ``{"phoneNumber": "+15558675310", "label": "Mobile",
             "isDefault": false}``

    The user has (presumably) typed the validation code into their phone
    keypad. We re-ask Twilio whether the number is now in
    OutgoingCallerIds — if yes, store it; if no, return a hint so the UI
    can ask them to retry.
    """
    body = request.get_json(silent=True) or {}
    phone = (body.get("phoneNumber") or body.get("phone_number") or "").strip()
    label = (body.get("label") or "").strip() or phone
    is_default = bool(body.get("isDefault") or body.get("is_default"))

    if not phone or not E164_RE.match(phone):
        return fail("phone_number_must_be_e164", status=400)

    if sim_store.get_by_number(phone):
        return fail("phone_number_already_added", status=409)

    if not sim_service.is_verified_caller_id(phone):
        return fail(
            "not_yet_verified",
            status=409,
            hint="Twilio hasn't confirmed the verification yet. Make sure you "
                 "entered the code on your phone keypad followed by '#'.",
        )

    try:
        sim = sim_store.create(
            Sim(
                id=new_sim_id(),
                tenant_id=current_tenant_id(),
                phone_number=phone,
                label=label,
                is_default=is_default,
                source="verified",
            )
        )
    except ValueError as exc:
        if str(exc) == "duplicate_phone_number":
            return fail("phone_number_already_added", status=409)
        raise
    return ok({"sim": sim.to_dict()})


@sims_bp.patch("/sims/<sim_id>")
@require_admin
def update_sim(sim_id: str):
    """Update label or default flag for a SIM."""
    body = request.get_json(silent=True) or {}
    fields: dict = {}
    if "label" in body:
        label = (body.get("label") or "").strip()
        if not label:
            return fail("label_cannot_be_empty", status=400)
        fields["label"] = label
    if "isDefault" in body or "is_default" in body:
        fields["is_default"] = bool(body.get("isDefault") or body.get("is_default"))
    if not fields:
        return fail("no_fields_to_update", status=400)

    owned = sim_store.get(sim_id)
    if not owned or owned.tenant_id != current_tenant_id():
        return fail("sim_not_found", status=404)
    sim = sim_store.update(sim_id, **fields)
    if not sim:
        return fail("sim_not_found", status=404)
    return ok({"sim": sim.to_dict()})


@sims_bp.delete("/sims/<sim_id>")
@require_admin
def delete_sim(sim_id: str):
    """Remove a SIM. The default flag (if any) is promoted to the next SIM."""
    owned = sim_store.get(sim_id)
    if not owned or owned.tenant_id != current_tenant_id():
        return fail("sim_not_found", status=404)
    if not sim_store.delete(sim_id):
        return fail("sim_not_found", status=404)
    return ok({"id": sim_id})


@sims_bp.post("/sims/<sim_id>/default")
@require_admin
def set_default_sim(sim_id: str):
    """Mark this SIM as the default outbound caller ID."""
    owned = sim_store.get(sim_id)
    if not owned or owned.tenant_id != current_tenant_id():
        return fail("sim_not_found", status=404)
    sim = sim_store.update(sim_id, is_default=True)
    if not sim:
        return fail("sim_not_found", status=404)
    return ok({"sim": sim.to_dict()})


@sims_bp.post("/sims/sync")
@require_admin
def sync_sims_from_twilio():
    """Pull every IncomingPhoneNumber from the Twilio account into the store."""
    tid = current_tenant_id()
    try:
        result = sim_service.sync_from_twilio(tenant_id=tid)
    except RuntimeError as exc:
        msg = str(exc)
        if msg == "twilio_not_configured":
            return fail("twilio_not_configured", status=400)
        return fail("twilio_sync_failed", message=msg, status=502)
    sims = [s.to_dict() for s in sim_store.list_all(tenant_id=tid)]
    return ok({**result, "sims": sims})


@sims_bp.get("/sims/twilio-numbers")
@require_admin
def list_twilio_numbers():
    """Return the raw Twilio IncomingPhoneNumbers (for the 'add from Twilio' picker)."""
    try:
        numbers = sim_service.list_twilio_numbers()
    except RuntimeError as exc:
        msg = str(exc)
        if msg == "twilio_not_configured":
            return fail("twilio_not_configured", status=400)
        return fail("twilio_api_failed", message=msg, status=502)
    return ok({"numbers": numbers})
