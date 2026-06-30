"""SIM (outbound caller-ID) helpers.

A thin service layer that sits between the SIM store and the routes:

  * :func:`sync_from_twilio` — pulls the workspace's Twilio numbers
    (``IncomingPhoneNumbers``) into the local SIM store, inserting any
    new ones and refreshing labels for ones already present.
  * :func:`seed_default_sim` — retained as a no-op stub for backward
    compatibility with ``app/__init__.py``. The legacy ``TWILIO_CALLER_ID``
    env var is **no longer** copied into the DB; the SIM store is now the
    single source of truth for outbound caller IDs. Users must add at
    least one SIM via the SIM Cards page (or sync from Twilio) before
    placing a call.
  * :func:`resolve_caller_id` — picks the right number to use for an
    outbound call: explicit ``from_number`` wins, then the default SIM
    in the store, then any SIM in the store (oldest), then ``None``.
    The ``.env`` fallback has been removed — if the store is empty the
    caller surfaces a "no SIM configured" error.
  * :func:`classify_number` — given an E.164 number, asks Twilio whether
    it's already an account-owned IncomingPhoneNumber, an
    already-verified OutgoingCallerId, or unknown.
  * :func:`start_verification` / :func:`check_verification` — kick off
    and check Twilio's "Outgoing Caller ID" verification flow so users
    can add their personal mobile or any external PSTN number as a
    legal ``from_=`` for outbound calls.

The Twilio sync is best-effort: failures are logged and surfaced to the
caller as a ``RuntimeError``; the local store is unchanged.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from app.models import Sim, new_sim_id, sim_store
from app.services import sip_admin
from config import settings

logger = logging.getLogger(__name__)


def seed_default_sim() -> None:
    """No-op. Retained for backward compatibility with ``app/__init__.py``.

    Auto-seeding from ``TWILIO_CALLER_ID`` has been disabled on purpose
    — the SIM store (DB) is the single source of truth for outbound
    caller IDs. Users must explicitly add a SIM via the UI (or run a
    Twilio sync) before placing calls. This avoids the env value
    silently masquerading as a "default SIM" the user never chose.
    """
    if sim_store.count() == 0 and (settings.twilio_caller_id or "").strip():
        logger.info(
            "SIM store is empty and TWILIO_CALLER_ID is set, but auto-seed "
            "is disabled. Add the number via the SIM Cards page or run a "
            "Twilio sync to use it for outbound calls."
        )
    return


def sync_from_twilio(tenant_id: Optional[str] = None) -> Dict[str, int]:
    """Fetch all IncomingPhoneNumbers from the tenant's Twilio account and upsert.

    Returns counts: ``{"added": N, "updated": M, "total": T}``.
    Raises ``RuntimeError`` if Twilio credentials are missing or the API
    call fails.
    """
    from app.services.tenant_context import current_tenant_id
    tid = tenant_id or current_tenant_id()
    if not settings.is_twilio_configured():
        raise RuntimeError("twilio_not_configured")
    try:
        numbers = sip_admin.list_phone_numbers(limit=500)
    except Exception as exc:  # pragma: no cover - network path
        logger.exception("Twilio list_phone_numbers failed")
        raise RuntimeError(f"twilio_api_error: {exc}") from exc

    added = 0
    updated = 0
    has_default = any(s.is_default for s in sim_store.list_all(tenant_id=tid))

    for n in numbers:
        phone = n.get("phone_number")
        if not phone:
            continue
        friendly = n.get("friendly_name") or phone
        sid = n.get("sid")
        existing = sim_store.get_by_number(phone)
        if existing and existing.tenant_id != tid:
            # Number is registered to a different workspace — leave it alone.
            continue
        if existing:
            # Refresh label + sid when they drift in the Twilio console.
            if existing.label != friendly or existing.twilio_sid != sid or existing.source != "twilio":
                sim_store.update(
                    existing.id,
                    label=friendly,
                    twilio_sid=sid,
                    source="twilio",
                )
                updated += 1
            continue
        sim_store.create(
            Sim(
                id=new_sim_id(),
                tenant_id=tid,
                phone_number=phone,
                label=friendly,
                is_default=not has_default,  # first imported number → default
                source="twilio",
                twilio_sid=sid,
            )
        )
        has_default = True
        added += 1

    return {"added": added, "updated": updated, "total": len(numbers)}


def resolve_caller_id(
    from_number: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> Optional[str]:
    """Pick the outbound caller ID for a call.

    Order of preference:
      1. ``from_number`` passed by the caller (UI dropdown choice).
      2. The SIM marked as default for the tenant. ``SimStore.get_default``
         already falls back to the oldest SIM in the tenant if none is
         explicitly marked default, so any SIM the user added in "Add
         SIM" will be used.
      3. ``None`` — caller surfaces a "no SIM configured" error.

    .env / ``TWILIO_CALLER_ID`` is intentionally **not** consulted here.
    The SIM store (DB) is the single source of truth.
    """
    candidate = (from_number or "").strip()
    if candidate:
        return candidate
    from app.services.tenant_context import current_tenant_id
    default = sim_store.get_default(tenant_id=tenant_id or current_tenant_id())
    if default:
        return default.phone_number
    return None


# --------------------------------------------------------------------------- #
#  Twilio "Outgoing Caller ID" verification
# --------------------------------------------------------------------------- #
#
# Twilio enforces that any number used as ``from_=`` on an outbound call
# must be either:
#   1. An IncomingPhoneNumber the workspace owns (purchased in Console), or
#   2. An entry in OutgoingCallerIds (a number verified via a one-time
#      voice-call OTP — Twilio dials the number and reads a 6-digit code
#      that the user must type on the keypad).
#
# Anything else is rejected with error 21219. The helpers below cover the
# verification flow so a user can add their personal mobile / a colleague's
# number / a landline as a SIM in this app and have it actually work.

def is_owned_by_account(phone_number: str) -> bool:
    """Return True when the number is in IncomingPhoneNumbers."""
    if not settings.is_twilio_configured():
        return False
    try:
        for n in sip_admin.list_phone_numbers(limit=500):
            if (n.get("phone_number") or "") == phone_number:
                return True
    except Exception:
        logger.exception("Twilio list_phone_numbers failed (is_owned_by_account)")
    return False


def is_verified_caller_id(phone_number: str) -> bool:
    """Return True when the number is in OutgoingCallerIds (already verified)."""
    if not settings.is_twilio_configured():
        return False
    try:
        ids = sip_admin.get_client().outgoing_caller_ids.list(
            phone_number=phone_number, limit=5,
        )
        return len(ids) > 0
    except Exception:
        logger.exception("Twilio outgoing_caller_ids.list failed")
        return False


def classify_number(phone_number: str) -> str:
    """Tell the caller what kind of Twilio caller-ID this number is.

    Returns ``"owned"`` when it's an IncomingPhoneNumber, ``"verified"``
    when it's already an OutgoingCallerId, or ``"unknown"`` when neither.
    """
    if is_owned_by_account(phone_number):
        return "owned"
    if is_verified_caller_id(phone_number):
        return "verified"
    return "unknown"


def start_verification(phone_number: str, friendly_name: Optional[str] = None) -> Dict:
    """Kick off Twilio's outbound caller-ID verification.

    Twilio will immediately call ``phone_number`` and an IVR will tell the
    user to enter the returned ``validation_code`` on their phone keypad.
    Once they do, the number is added to OutgoingCallerIds (which is what
    Twilio checks before any outbound call goes out).

    Returns ``{phone_number, friendly_name, validation_code}``.
    Raises ``RuntimeError`` on Twilio errors or missing configuration.
    """
    if not settings.is_twilio_configured():
        raise RuntimeError("twilio_not_configured")
    from twilio.base.exceptions import TwilioRestException
    try:
        req = sip_admin.get_client().validation_requests.create(
            phone_number=phone_number,
            friendly_name=(friendly_name or phone_number)[:64],
            call_delay=0,
        )
    except TwilioRestException as exc:
        logger.warning("Twilio validation_requests.create failed: %s", exc)
        raise RuntimeError(f"twilio_error: {exc.msg or exc}") from exc
    return {
        "phone_number": req.phone_number,
        "friendly_name": req.friendly_name,
        "validation_code": req.validation_code,
    }


def list_twilio_numbers() -> List[Dict]:
    """Return the raw Twilio IncomingPhoneNumbers (useful for the
    "pick from Twilio" UI). Does not touch the local store."""
    if not settings.is_twilio_configured():
        raise RuntimeError("twilio_not_configured")
    try:
        return sip_admin.list_phone_numbers(limit=500)
    except Exception as exc:  # pragma: no cover - network path
        logger.exception("Twilio list_phone_numbers failed")
        raise RuntimeError(f"twilio_api_error: {exc}") from exc
