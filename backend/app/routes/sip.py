"""SIP infrastructure REST API — Domains, ACLs, Credentials, Mappings.

Exposes the Twilio SIP REST API documented in PART B of ``twilio.txt`` as
a clean JSON-over-HTTP surface for the admin frontend. Mounted at
``/api/v1/sip``. Every route requires admin auth and assumes Twilio creds
are configured (returns 503 ``twilio_not_configured`` otherwise).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Tuple

from flask import Blueprint, Response, jsonify, request

from app.services import sip_admin
from app.services.auth_service import require_admin
from app.services.sim_service import resolve_caller_id
from app.services.tenant_context import current_tenant_id
from app.services.twilio_service import make_outbound_sip_call
from app.utils import fail, ok, require_twilio_configured
from config import settings

logger = logging.getLogger(__name__)

sip_bp = Blueprint("sip", __name__)


# --------------------------------------------------------------------------- #
#  helpers
# --------------------------------------------------------------------------- #

def _guard() -> Tuple[Response, int] | None:
    """Common pre-flight: Twilio creds present."""
    return require_twilio_configured()


def _body() -> Dict[str, Any]:
    return request.get_json(silent=True) or {}


def _handle(exc: sip_admin.SipAdminError) -> Tuple[Response, int]:
    """Translate a SipAdminError into a JSON error response."""
    payload: Dict[str, Any] = {
        "success": False,
        "error": "sip_admin_error",
        "message": str(exc),
    }
    if exc.code:
        payload["twilio_code"] = exc.code
    if exc.details and exc.details != str(exc):
        payload["detail"] = exc.details
    return jsonify(payload), exc.status


def _suggested_voice_url() -> str:
    """Default SIP-Domain voice URL pointing at our webhook."""
    return settings.webhook_url("/twilio/voice/sip/incoming")


# =========================================================================== #
#  Discovery / suggested defaults
# =========================================================================== #

@sip_bp.get("/sip/defaults")
@require_admin
def sip_defaults():
    """Helpful tips for the UI: suggested voice URL, caller-id, etc."""
    return ok({
        "suggested_voice_url": _suggested_voice_url() if settings.public_base_url else None,
        "public_base_url": settings.public_base_url or None,
        "caller_id": resolve_caller_id(tenant_id=current_tenant_id()) or None,
        "validation_enabled": settings.validate_twilio_signature,
    })


# =========================================================================== #
#  SIP DOMAINS
# =========================================================================== #

@sip_bp.get("/sip/domains")
@require_admin
def list_sip_domains():
    err = _guard()
    if err:
        return err
    try:
        return ok({"domains": sip_admin.list_domains(limit=request.args.get("limit", 50))})
    except sip_admin.SipAdminError as exc:
        return _handle(exc)


@sip_bp.post("/sip/domains")
@require_admin
def create_sip_domain():
    err = _guard()
    if err:
        return err
    body = _body()
    try:
        domain = sip_admin.create_domain(
            domain_name=str(body.get("domain_name", "")).strip(),
            friendly_name=body.get("friendly_name"),
            voice_url=body.get("voice_url") or _suggested_voice_url(),
            voice_method=body.get("voice_method") or "POST",
            voice_fallback_url=body.get("voice_fallback_url"),
            voice_fallback_method=body.get("voice_fallback_method"),
            voice_status_callback_url=body.get("voice_status_callback_url"),
            voice_status_callback_method=body.get("voice_status_callback_method"),
            sip_registration=body.get("sip_registration"),
            emergency_calling_enabled=body.get("emergency_calling_enabled"),
            secure=body.get("secure"),
            byoc_trunk_sid=body.get("byoc_trunk_sid"),
            emergency_caller_sid=body.get("emergency_caller_sid"),
        )
        return ok({"domain": domain})
    except sip_admin.SipAdminError as exc:
        return _handle(exc)


@sip_bp.get("/sip/domains/<sid>")
@require_admin
def get_sip_domain(sid: str):
    err = _guard()
    if err:
        return err
    try:
        return ok({"domain": sip_admin.get_domain(sid)})
    except sip_admin.SipAdminError as exc:
        return _handle(exc)


@sip_bp.patch("/sip/domains/<sid>")
@require_admin
def update_sip_domain(sid: str):
    err = _guard()
    if err:
        return err
    body = _body()
    try:
        return ok({"domain": sip_admin.update_domain(sid, **body)})
    except sip_admin.SipAdminError as exc:
        return _handle(exc)


@sip_bp.delete("/sip/domains/<sid>")
@require_admin
def delete_sip_domain(sid: str):
    err = _guard()
    if err:
        return err
    try:
        sip_admin.delete_domain(sid)
        return ok({"sid": sid, "deleted": True})
    except sip_admin.SipAdminError as exc:
        return _handle(exc)


@sip_bp.post("/sip/domains/provision")
@require_admin
def provision_sip_domain():
    """One-shot provisioner — create Domain + Credentials + ACL + Mappings."""
    err = _guard()
    if err:
        return err
    body = _body()
    try:
        result = sip_admin.provision_domain(
            domain_name=str(body.get("domain_name", "")).strip(),
            friendly_name=body.get("friendly_name"),
            voice_url=body.get("voice_url") or _suggested_voice_url(),
            allow_registration=bool(body.get("allow_registration", True)),
            secure=bool(body.get("secure", True)),
            credential_list_friendly_name=body.get("credential_list_friendly_name"),
            credentials=body.get("credentials") or None,
            acl_friendly_name=body.get("acl_friendly_name"),
            allowed_ips=body.get("allowed_ips") or None,
        )
        return ok({"provisioned": result})
    except sip_admin.SipAdminError as exc:
        return _handle(exc)


# =========================================================================== #
#  IP ACCESS CONTROL LISTS
# =========================================================================== #

@sip_bp.get("/sip/acls")
@require_admin
def list_acls():
    err = _guard()
    if err:
        return err
    try:
        return ok({"acls": sip_admin.list_acls(limit=request.args.get("limit", 50))})
    except sip_admin.SipAdminError as exc:
        return _handle(exc)


@sip_bp.post("/sip/acls")
@require_admin
def create_acl():
    err = _guard()
    if err:
        return err
    body = _body()
    try:
        return ok({"acl": sip_admin.create_acl(friendly_name=str(body.get("friendly_name", "")).strip())})
    except sip_admin.SipAdminError as exc:
        return _handle(exc)


@sip_bp.get("/sip/acls/<sid>")
@require_admin
def get_acl(sid: str):
    err = _guard()
    if err:
        return err
    try:
        return ok({"acl": sip_admin.get_acl(sid)})
    except sip_admin.SipAdminError as exc:
        return _handle(exc)


@sip_bp.patch("/sip/acls/<sid>")
@require_admin
def update_acl(sid: str):
    err = _guard()
    if err:
        return err
    body = _body()
    try:
        return ok({"acl": sip_admin.update_acl(sid, friendly_name=str(body.get("friendly_name", "")).strip())})
    except sip_admin.SipAdminError as exc:
        return _handle(exc)


@sip_bp.delete("/sip/acls/<sid>")
@require_admin
def delete_acl(sid: str):
    err = _guard()
    if err:
        return err
    try:
        sip_admin.delete_acl(sid)
        return ok({"sid": sid, "deleted": True})
    except sip_admin.SipAdminError as exc:
        return _handle(exc)


# --- IPs inside an ACL --------------------------------------------------- #

@sip_bp.get("/sip/acls/<acl_sid>/ips")
@require_admin
def list_ips(acl_sid: str):
    err = _guard()
    if err:
        return err
    try:
        return ok({"ips": sip_admin.list_ips(acl_sid, limit=request.args.get("limit", 200))})
    except sip_admin.SipAdminError as exc:
        return _handle(exc)


@sip_bp.post("/sip/acls/<acl_sid>/ips")
@require_admin
def create_ip(acl_sid: str):
    err = _guard()
    if err:
        return err
    body = _body()
    try:
        return ok({"ip": sip_admin.create_ip(
            acl_sid,
            friendly_name=str(body.get("friendly_name", "")).strip(),
            ip_address=str(body.get("ip_address", "")).strip(),
            cidr_prefix_length=body.get("cidr_prefix_length"),
        )})
    except sip_admin.SipAdminError as exc:
        return _handle(exc)


@sip_bp.patch("/sip/acls/<acl_sid>/ips/<ip_sid>")
@require_admin
def update_ip(acl_sid: str, ip_sid: str):
    err = _guard()
    if err:
        return err
    try:
        return ok({"ip": sip_admin.update_ip(acl_sid, ip_sid, **_body())})
    except sip_admin.SipAdminError as exc:
        return _handle(exc)


@sip_bp.delete("/sip/acls/<acl_sid>/ips/<ip_sid>")
@require_admin
def delete_ip(acl_sid: str, ip_sid: str):
    err = _guard()
    if err:
        return err
    try:
        sip_admin.delete_ip(acl_sid, ip_sid)
        return ok({"sid": ip_sid, "deleted": True})
    except sip_admin.SipAdminError as exc:
        return _handle(exc)


# =========================================================================== #
#  CREDENTIAL LISTS + CREDENTIALS
# =========================================================================== #

@sip_bp.get("/sip/credential-lists")
@require_admin
def list_credential_lists():
    err = _guard()
    if err:
        return err
    try:
        return ok({"credential_lists": sip_admin.list_credential_lists(
            limit=request.args.get("limit", 50)
        )})
    except sip_admin.SipAdminError as exc:
        return _handle(exc)


@sip_bp.post("/sip/credential-lists")
@require_admin
def create_credential_list():
    err = _guard()
    if err:
        return err
    body = _body()
    try:
        return ok({"credential_list": sip_admin.create_credential_list(
            friendly_name=str(body.get("friendly_name", "")).strip(),
        )})
    except sip_admin.SipAdminError as exc:
        return _handle(exc)


@sip_bp.get("/sip/credential-lists/<sid>")
@require_admin
def get_credential_list(sid: str):
    err = _guard()
    if err:
        return err
    try:
        return ok({"credential_list": sip_admin.get_credential_list(sid)})
    except sip_admin.SipAdminError as exc:
        return _handle(exc)


@sip_bp.patch("/sip/credential-lists/<sid>")
@require_admin
def update_credential_list(sid: str):
    err = _guard()
    if err:
        return err
    body = _body()
    try:
        return ok({"credential_list": sip_admin.update_credential_list(
            sid, friendly_name=str(body.get("friendly_name", "")).strip(),
        )})
    except sip_admin.SipAdminError as exc:
        return _handle(exc)


@sip_bp.delete("/sip/credential-lists/<sid>")
@require_admin
def delete_credential_list(sid: str):
    err = _guard()
    if err:
        return err
    try:
        sip_admin.delete_credential_list(sid)
        return ok({"sid": sid, "deleted": True})
    except sip_admin.SipAdminError as exc:
        return _handle(exc)


# --- Credentials inside a list ----------------------------------------- #

@sip_bp.get("/sip/credential-lists/<list_sid>/credentials")
@require_admin
def list_credentials(list_sid: str):
    err = _guard()
    if err:
        return err
    try:
        return ok({"credentials": sip_admin.list_credentials(list_sid,
                                                             limit=request.args.get("limit", 200))})
    except sip_admin.SipAdminError as exc:
        return _handle(exc)


@sip_bp.post("/sip/credential-lists/<list_sid>/credentials")
@require_admin
def create_credential(list_sid: str):
    err = _guard()
    if err:
        return err
    body = _body()
    try:
        return ok({"credential": sip_admin.create_credential(
            list_sid,
            username=str(body.get("username", "")).strip(),
            password=str(body.get("password", "")),
        )})
    except sip_admin.SipAdminError as exc:
        return _handle(exc)


@sip_bp.patch("/sip/credential-lists/<list_sid>/credentials/<cred_sid>")
@require_admin
def update_credential(list_sid: str, cred_sid: str):
    err = _guard()
    if err:
        return err
    body = _body()
    try:
        return ok({"credential": sip_admin.update_credential(
            list_sid, cred_sid,
            password=str(body.get("password", "")),
        )})
    except sip_admin.SipAdminError as exc:
        return _handle(exc)


@sip_bp.delete("/sip/credential-lists/<list_sid>/credentials/<cred_sid>")
@require_admin
def delete_credential(list_sid: str, cred_sid: str):
    err = _guard()
    if err:
        return err
    try:
        sip_admin.delete_credential(list_sid, cred_sid)
        return ok({"sid": cred_sid, "deleted": True})
    except sip_admin.SipAdminError as exc:
        return _handle(exc)


# =========================================================================== #
#  MAPPINGS (per-domain): Calls auth + Registrations auth
# =========================================================================== #

# --- Calls / CredentialListMapping ------------------------------------- #

@sip_bp.get("/sip/domains/<domain_sid>/mappings/calls/credential-lists")
@require_admin
def list_call_cred_mappings(domain_sid: str):
    err = _guard()
    if err:
        return err
    try:
        return ok({"mappings": sip_admin.list_credential_list_mappings(
            domain_sid, limit=request.args.get("limit", 50),
        )})
    except sip_admin.SipAdminError as exc:
        return _handle(exc)


@sip_bp.post("/sip/domains/<domain_sid>/mappings/calls/credential-lists")
@require_admin
def create_call_cred_mapping(domain_sid: str):
    err = _guard()
    if err:
        return err
    body = _body()
    try:
        return ok({"mapping": sip_admin.create_credential_list_mapping(
            domain_sid, credential_list_sid=str(body.get("credential_list_sid", "")).strip(),
        )})
    except sip_admin.SipAdminError as exc:
        return _handle(exc)


@sip_bp.delete("/sip/domains/<domain_sid>/mappings/calls/credential-lists/<mapping_sid>")
@require_admin
def delete_call_cred_mapping(domain_sid: str, mapping_sid: str):
    err = _guard()
    if err:
        return err
    try:
        sip_admin.delete_credential_list_mapping(domain_sid, mapping_sid)
        return ok({"sid": mapping_sid, "deleted": True})
    except sip_admin.SipAdminError as exc:
        return _handle(exc)


# --- Calls / IpAccessControlListMapping -------------------------------- #

@sip_bp.get("/sip/domains/<domain_sid>/mappings/calls/acls")
@require_admin
def list_call_acl_mappings(domain_sid: str):
    err = _guard()
    if err:
        return err
    try:
        return ok({"mappings": sip_admin.list_ip_access_control_list_mappings(
            domain_sid, limit=request.args.get("limit", 50),
        )})
    except sip_admin.SipAdminError as exc:
        return _handle(exc)


@sip_bp.post("/sip/domains/<domain_sid>/mappings/calls/acls")
@require_admin
def create_call_acl_mapping(domain_sid: str):
    err = _guard()
    if err:
        return err
    body = _body()
    try:
        return ok({"mapping": sip_admin.create_ip_access_control_list_mapping(
            domain_sid,
            ip_access_control_list_sid=str(body.get("ip_access_control_list_sid", "")).strip(),
        )})
    except sip_admin.SipAdminError as exc:
        return _handle(exc)


@sip_bp.delete("/sip/domains/<domain_sid>/mappings/calls/acls/<mapping_sid>")
@require_admin
def delete_call_acl_mapping(domain_sid: str, mapping_sid: str):
    err = _guard()
    if err:
        return err
    try:
        sip_admin.delete_ip_access_control_list_mapping(domain_sid, mapping_sid)
        return ok({"sid": mapping_sid, "deleted": True})
    except sip_admin.SipAdminError as exc:
        return _handle(exc)


# --- Registrations / CredentialListMapping ----------------------------- #

@sip_bp.get("/sip/domains/<domain_sid>/mappings/registrations/credential-lists")
@require_admin
def list_register_cred_mappings(domain_sid: str):
    err = _guard()
    if err:
        return err
    try:
        return ok({"mappings": sip_admin.list_registration_credential_list_mappings(
            domain_sid, limit=request.args.get("limit", 50),
        )})
    except sip_admin.SipAdminError as exc:
        return _handle(exc)


@sip_bp.post("/sip/domains/<domain_sid>/mappings/registrations/credential-lists")
@require_admin
def create_register_cred_mapping(domain_sid: str):
    err = _guard()
    if err:
        return err
    body = _body()
    try:
        return ok({"mapping": sip_admin.create_registration_credential_list_mapping(
            domain_sid, credential_list_sid=str(body.get("credential_list_sid", "")).strip(),
        )})
    except sip_admin.SipAdminError as exc:
        return _handle(exc)


@sip_bp.delete("/sip/domains/<domain_sid>/mappings/registrations/credential-lists/<mapping_sid>")
@require_admin
def delete_register_cred_mapping(domain_sid: str, mapping_sid: str):
    err = _guard()
    if err:
        return err
    try:
        sip_admin.delete_registration_credential_list_mapping(domain_sid, mapping_sid)
        return ok({"sid": mapping_sid, "deleted": True})
    except sip_admin.SipAdminError as exc:
        return _handle(exc)


# =========================================================================== #
#  OUTBOUND SIP CALL  (twilio.txt §10)
# =========================================================================== #

@sip_bp.post("/sip/calls")
@require_admin
def make_sip_call_route():
    err = _guard()
    if err:
        return err
    body = _body()
    to_sip_uri = str(body.get("to") or body.get("to_sip_uri") or "").strip()
    if not to_sip_uri:
        return fail("missing_to", status=400)
    try:
        sid = make_outbound_sip_call(
            to_sip_uri=to_sip_uri,
            from_number=body.get("from") or body.get("from_number"),
            voice_url=body.get("voice_url"),
            voice_method=body.get("voice_method", "POST"),
            timeout=body.get("timeout"),
            tenant_id=current_tenant_id(),
        )
        return ok({"call_sid": sid, "to": to_sip_uri})
    except ValueError as exc:
        return fail("invalid_sip_uri", status=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("Outbound SIP call failed")
        return fail("outbound_sip_failed", status=502, detail=str(exc))


# =========================================================================== #
#  EMERGENCY CALLING (twilio.txt §25)
# =========================================================================== #

@sip_bp.get("/sip/addresses")
@require_admin
def list_addresses():
    err = _guard()
    if err:
        return err
    try:
        return ok({"addresses": sip_admin.list_addresses(limit=request.args.get("limit", 50))})
    except sip_admin.SipAdminError as exc:
        return _handle(exc)


@sip_bp.post("/sip/addresses")
@require_admin
def create_address():
    err = _guard()
    if err:
        return err
    body = _body()
    try:
        return ok({"address": sip_admin.create_address(
            customer_name=str(body.get("customer_name", "")).strip(),
            friendly_name=str(body.get("friendly_name", "")).strip(),
            street=str(body.get("street", "")).strip(),
            city=str(body.get("city", "")).strip(),
            region=str(body.get("region", "")).strip(),
            postal_code=str(body.get("postal_code", "")).strip(),
            iso_country=str(body.get("iso_country", "")).strip().upper(),
            emergency_enabled=bool(body.get("emergency_enabled", True)),
        )})
    except sip_admin.SipAdminError as exc:
        return _handle(exc)


@sip_bp.delete("/sip/addresses/<sid>")
@require_admin
def delete_address(sid: str):
    err = _guard()
    if err:
        return err
    try:
        sip_admin.delete_address(sid)
        return ok({"sid": sid, "deleted": True})
    except sip_admin.SipAdminError as exc:
        return _handle(exc)


@sip_bp.get("/sip/phone-numbers")
@require_admin
def list_phone_numbers():
    err = _guard()
    if err:
        return err
    try:
        return ok({"phone_numbers": sip_admin.list_phone_numbers(limit=request.args.get("limit", 100))})
    except sip_admin.SipAdminError as exc:
        return _handle(exc)


@sip_bp.patch("/sip/phone-numbers/<sid>/emergency-address")
@require_admin
def set_phone_emergency_address(sid: str):
    err = _guard()
    if err:
        return err
    body = _body()
    try:
        return ok({"phone_number": sip_admin.assign_emergency_address(
            sid, emergency_address_sid=body.get("emergency_address_sid"),
        )})
    except sip_admin.SipAdminError as exc:
        return _handle(exc)
