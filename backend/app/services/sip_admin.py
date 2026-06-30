"""Twilio SIP REST API wrapper.

Thin facade over the official ``twilio.rest.Client`` for the resources listed
in PART B of ``twilio.txt`` (sections 17-25):

    * SIP Domain                                 (SD…)
    * SIP IpAccessControlList                    (AL…)
    * SIP IpAddress (inside an ACL)              (IP…)
    * SIP CredentialList                         (CL…)
    * SIP Credential (inside a CredentialList)   (CR…)
    * SIP CredentialListMapping (Calls auth)
    * SIP IpAccessControlListMapping (Calls auth)
    * SIP Domain Registration CredentialListMapping
    * Emergency Calling — Address + IncomingPhoneNumber tweaks

Every function returns a JSON-serialisable ``dict`` so the route layer can
hand the result straight back to the frontend.  Errors are normalised to
``SipAdminError`` with the underlying Twilio ``status`` / ``code`` /
``message`` preserved.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from twilio.base.exceptions import TwilioRestException

from .twilio_service import get_client

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
#  Errors
# --------------------------------------------------------------------------- #

class SipAdminError(RuntimeError):
    """Wrapper around ``TwilioRestException`` with HTTP-friendly metadata."""

    def __init__(self, message: str, *, status: int = 502, code: Optional[int] = None,
                 details: Optional[str] = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.details = details


def _wrap(exc: TwilioRestException) -> SipAdminError:
    status = int(getattr(exc, "status", 502) or 502)
    if status in (401, 403, 404, 409, 422):
        http_status = status
    elif 400 <= status < 500:
        http_status = 400
    else:
        http_status = 502
    return SipAdminError(
        str(getattr(exc, "msg", exc)),
        status=http_status,
        code=int(getattr(exc, "code", 0) or 0) or None,
        details=str(exc),
    )


# --------------------------------------------------------------------------- #
#  Pagination helper
# --------------------------------------------------------------------------- #

def _coerce_limit(limit: Optional[int]) -> int:
    """Clamp pagination limit to a safe range."""
    if limit is None:
        return 50
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return 50
    return max(1, min(n, 1000))


# --------------------------------------------------------------------------- #
#  Serialisers
# --------------------------------------------------------------------------- #

def _domain_dict(d: Any) -> Dict[str, Any]:
    return {
        "sid": d.sid,
        "account_sid": d.account_sid,
        "api_version": d.api_version,
        "auth_type": d.auth_type,
        "domain_name": d.domain_name,
        "friendly_name": d.friendly_name,
        "voice_url": d.voice_url,
        "voice_method": d.voice_method,
        "voice_fallback_url": d.voice_fallback_url,
        "voice_fallback_method": d.voice_fallback_method,
        "voice_status_callback_url": d.voice_status_callback_url,
        "voice_status_callback_method": d.voice_status_callback_method,
        "sip_registration": d.sip_registration,
        "emergency_calling_enabled": d.emergency_calling_enabled,
        "secure": d.secure,
        "byoc_trunk_sid": d.byoc_trunk_sid,
        "emergency_caller_sid": d.emergency_caller_sid,
        "subresource_uris": d.subresource_uris,
        "uri": d.uri,
        "date_created": d.date_created.isoformat() if d.date_created else None,
        "date_updated": d.date_updated.isoformat() if d.date_updated else None,
    }


def _acl_dict(a: Any) -> Dict[str, Any]:
    return {
        "sid": a.sid,
        "account_sid": a.account_sid,
        "friendly_name": a.friendly_name,
        "subresource_uris": a.subresource_uris,
        "uri": a.uri,
        "date_created": a.date_created.isoformat() if a.date_created else None,
        "date_updated": a.date_updated.isoformat() if a.date_updated else None,
    }


def _ip_dict(i: Any) -> Dict[str, Any]:
    return {
        "sid": i.sid,
        "account_sid": i.account_sid,
        "ip_access_control_list_sid": i.ip_access_control_list_sid,
        "friendly_name": i.friendly_name,
        "ip_address": i.ip_address,
        "cidr_prefix_length": i.cidr_prefix_length,
        "uri": i.uri,
        "date_created": i.date_created.isoformat() if i.date_created else None,
        "date_updated": i.date_updated.isoformat() if i.date_updated else None,
    }


def _credlist_dict(c: Any) -> Dict[str, Any]:
    return {
        "sid": c.sid,
        "account_sid": c.account_sid,
        "friendly_name": c.friendly_name,
        "subresource_uris": c.subresource_uris,
        "uri": c.uri,
        "date_created": c.date_created.isoformat() if c.date_created else None,
        "date_updated": c.date_updated.isoformat() if c.date_updated else None,
    }


def _cred_dict(c: Any) -> Dict[str, Any]:
    return {
        "sid": c.sid,
        "account_sid": c.account_sid,
        "credential_list_sid": c.credential_list_sid,
        "username": c.username,
        "uri": c.uri,
        "date_created": c.date_created.isoformat() if c.date_created else None,
        "date_updated": c.date_updated.isoformat() if c.date_updated else None,
    }


def _mapping_dict(m: Any) -> Dict[str, Any]:
    return {
        "sid": m.sid,
        "account_sid": m.account_sid,
        "friendly_name": getattr(m, "friendly_name", None),
        "uri": getattr(m, "uri", None),
        "date_created": m.date_created.isoformat() if getattr(m, "date_created", None) else None,
        "date_updated": m.date_updated.isoformat() if getattr(m, "date_updated", None) else None,
    }


def _address_dict(a: Any) -> Dict[str, Any]:
    return {
        "sid": a.sid,
        "account_sid": a.account_sid,
        "customer_name": a.customer_name,
        "friendly_name": a.friendly_name,
        "street": a.street,
        "city": a.city,
        "region": a.region,
        "postal_code": a.postal_code,
        "iso_country": a.iso_country,
        "emergency_enabled": a.emergency_enabled,
        "validated": a.validated,
        "verified": a.verified,
        "uri": a.uri,
        "date_created": a.date_created.isoformat() if a.date_created else None,
        "date_updated": a.date_updated.isoformat() if a.date_updated else None,
    }


def _phone_number_dict(p: Any) -> Dict[str, Any]:
    return {
        "sid": p.sid,
        "account_sid": p.account_sid,
        "phone_number": p.phone_number,
        "friendly_name": p.friendly_name,
        "emergency_status": p.emergency_status,
        "emergency_address_sid": p.emergency_address_sid,
        "emergency_address_status": p.emergency_address_status,
        "uri": p.uri,
    }


# =========================================================================== #
#  SIP DOMAIN
# =========================================================================== #

def list_domains(limit: Optional[int] = 50) -> List[Dict[str, Any]]:
    try:
        return [_domain_dict(d) for d in get_client().sip.domains.list(limit=_coerce_limit(limit))]
    except TwilioRestException as exc:
        raise _wrap(exc) from exc


def get_domain(sid: str) -> Dict[str, Any]:
    try:
        return _domain_dict(get_client().sip.domains(sid).fetch())
    except TwilioRestException as exc:
        raise _wrap(exc) from exc


def create_domain(*,
                  domain_name: str,
                  friendly_name: Optional[str] = None,
                  voice_url: Optional[str] = None,
                  voice_method: Optional[str] = None,
                  voice_fallback_url: Optional[str] = None,
                  voice_fallback_method: Optional[str] = None,
                  voice_status_callback_url: Optional[str] = None,
                  voice_status_callback_method: Optional[str] = None,
                  sip_registration: Optional[bool] = None,
                  emergency_calling_enabled: Optional[bool] = None,
                  secure: Optional[bool] = None,
                  byoc_trunk_sid: Optional[str] = None,
                  emergency_caller_sid: Optional[str] = None) -> Dict[str, Any]:
    """POST /SIP/Domains.json — `domain_name` must end with `.sip.twilio.com`."""
    if not domain_name or not domain_name.endswith(".sip.twilio.com"):
        raise SipAdminError(
            "domain_name must end with '.sip.twilio.com'",
            status=400,
        )
    kwargs: Dict[str, Any] = {"domain_name": domain_name}
    if friendly_name is not None:                kwargs["friendly_name"] = friendly_name
    if voice_url is not None:                    kwargs["voice_url"] = voice_url
    if voice_method is not None:                 kwargs["voice_method"] = voice_method
    if voice_fallback_url is not None:           kwargs["voice_fallback_url"] = voice_fallback_url
    if voice_fallback_method is not None:        kwargs["voice_fallback_method"] = voice_fallback_method
    if voice_status_callback_url is not None:    kwargs["voice_status_callback_url"] = voice_status_callback_url
    if voice_status_callback_method is not None: kwargs["voice_status_callback_method"] = voice_status_callback_method
    if sip_registration is not None:             kwargs["sip_registration"] = sip_registration
    if emergency_calling_enabled is not None:    kwargs["emergency_calling_enabled"] = emergency_calling_enabled
    if secure is not None:                       kwargs["secure"] = secure
    if byoc_trunk_sid:                           kwargs["byoc_trunk_sid"] = byoc_trunk_sid
    if emergency_caller_sid:                     kwargs["emergency_caller_sid"] = emergency_caller_sid
    try:
        return _domain_dict(get_client().sip.domains.create(**kwargs))
    except TwilioRestException as exc:
        raise _wrap(exc) from exc


def update_domain(sid: str, **fields: Any) -> Dict[str, Any]:
    """POST /SIP/Domains/{sid}.json — all fields optional."""
    allowed = {
        "friendly_name", "voice_url", "voice_method",
        "voice_fallback_url", "voice_fallback_method",
        "voice_status_callback_url", "voice_status_callback_method",
        "sip_registration", "emergency_calling_enabled", "secure",
        "domain_name", "byoc_trunk_sid", "emergency_caller_sid",
    }
    kwargs = {k: v for k, v in fields.items() if k in allowed and v is not None}
    try:
        return _domain_dict(get_client().sip.domains(sid).update(**kwargs))
    except TwilioRestException as exc:
        raise _wrap(exc) from exc


def delete_domain(sid: str) -> None:
    try:
        get_client().sip.domains(sid).delete()
    except TwilioRestException as exc:
        raise _wrap(exc) from exc


# =========================================================================== #
#  IpAccessControlList  (ACL) + IpAddress
# =========================================================================== #

def list_acls(limit: Optional[int] = 50) -> List[Dict[str, Any]]:
    try:
        return [_acl_dict(a) for a in get_client().sip.ip_access_control_lists.list(limit=_coerce_limit(limit))]
    except TwilioRestException as exc:
        raise _wrap(exc) from exc


def get_acl(sid: str) -> Dict[str, Any]:
    try:
        return _acl_dict(get_client().sip.ip_access_control_lists(sid).fetch())
    except TwilioRestException as exc:
        raise _wrap(exc) from exc


def create_acl(*, friendly_name: str) -> Dict[str, Any]:
    if not friendly_name.strip():
        raise SipAdminError("friendly_name is required", status=400)
    try:
        return _acl_dict(get_client().sip.ip_access_control_lists.create(friendly_name=friendly_name))
    except TwilioRestException as exc:
        raise _wrap(exc) from exc


def update_acl(sid: str, *, friendly_name: str) -> Dict[str, Any]:
    try:
        return _acl_dict(get_client().sip.ip_access_control_lists(sid).update(friendly_name=friendly_name))
    except TwilioRestException as exc:
        raise _wrap(exc) from exc


def delete_acl(sid: str) -> None:
    try:
        get_client().sip.ip_access_control_lists(sid).delete()
    except TwilioRestException as exc:
        raise _wrap(exc) from exc


def list_ips(acl_sid: str, limit: Optional[int] = 200) -> List[Dict[str, Any]]:
    try:
        return [
            _ip_dict(ip) for ip in
            get_client().sip.ip_access_control_lists(acl_sid)
            .ip_addresses.list(limit=_coerce_limit(limit))
        ]
    except TwilioRestException as exc:
        raise _wrap(exc) from exc


def create_ip(acl_sid: str, *, friendly_name: str, ip_address: str,
              cidr_prefix_length: Optional[int] = None) -> Dict[str, Any]:
    if not friendly_name.strip():
        raise SipAdminError("friendly_name is required", status=400)
    if not ip_address.strip():
        raise SipAdminError("ip_address is required (IPv4 dotted decimal)", status=400)
    kwargs: Dict[str, Any] = {"friendly_name": friendly_name, "ip_address": ip_address}
    if cidr_prefix_length is not None:
        kwargs["cidr_prefix_length"] = cidr_prefix_length
    try:
        return _ip_dict(get_client().sip.ip_access_control_lists(acl_sid).ip_addresses.create(**kwargs))
    except TwilioRestException as exc:
        raise _wrap(exc) from exc


def update_ip(acl_sid: str, ip_sid: str, **fields: Any) -> Dict[str, Any]:
    allowed = {"friendly_name", "ip_address", "cidr_prefix_length"}
    kwargs = {k: v for k, v in fields.items() if k in allowed and v is not None}
    try:
        return _ip_dict(
            get_client().sip.ip_access_control_lists(acl_sid).ip_addresses(ip_sid).update(**kwargs)
        )
    except TwilioRestException as exc:
        raise _wrap(exc) from exc


def delete_ip(acl_sid: str, ip_sid: str) -> None:
    try:
        get_client().sip.ip_access_control_lists(acl_sid).ip_addresses(ip_sid).delete()
    except TwilioRestException as exc:
        raise _wrap(exc) from exc


# =========================================================================== #
#  CredentialList + Credential
# =========================================================================== #

def list_credential_lists(limit: Optional[int] = 50) -> List[Dict[str, Any]]:
    try:
        return [
            _credlist_dict(c)
            for c in get_client().sip.credential_lists.list(limit=_coerce_limit(limit))
        ]
    except TwilioRestException as exc:
        raise _wrap(exc) from exc


def get_credential_list(sid: str) -> Dict[str, Any]:
    try:
        return _credlist_dict(get_client().sip.credential_lists(sid).fetch())
    except TwilioRestException as exc:
        raise _wrap(exc) from exc


def create_credential_list(*, friendly_name: str) -> Dict[str, Any]:
    if not friendly_name.strip():
        raise SipAdminError("friendly_name is required", status=400)
    try:
        return _credlist_dict(get_client().sip.credential_lists.create(friendly_name=friendly_name))
    except TwilioRestException as exc:
        raise _wrap(exc) from exc


def update_credential_list(sid: str, *, friendly_name: str) -> Dict[str, Any]:
    try:
        return _credlist_dict(get_client().sip.credential_lists(sid).update(friendly_name=friendly_name))
    except TwilioRestException as exc:
        raise _wrap(exc) from exc


def delete_credential_list(sid: str) -> None:
    try:
        get_client().sip.credential_lists(sid).delete()
    except TwilioRestException as exc:
        raise _wrap(exc) from exc


def list_credentials(list_sid: str, limit: Optional[int] = 200) -> List[Dict[str, Any]]:
    try:
        return [
            _cred_dict(c)
            for c in get_client().sip.credential_lists(list_sid).credentials.list(limit=_coerce_limit(limit))
        ]
    except TwilioRestException as exc:
        raise _wrap(exc) from exc


def create_credential(list_sid: str, *, username: str, password: str) -> Dict[str, Any]:
    _validate_username(username)
    _validate_password(password)
    try:
        return _cred_dict(
            get_client().sip.credential_lists(list_sid)
            .credentials.create(username=username, password=password)
        )
    except TwilioRestException as exc:
        raise _wrap(exc) from exc


def update_credential(list_sid: str, cred_sid: str, *, password: str) -> Dict[str, Any]:
    """POST .../Credentials/{cred}.json — only password is mutable per docs."""
    _validate_password(password)
    try:
        return _cred_dict(
            get_client().sip.credential_lists(list_sid).credentials(cred_sid).update(password=password)
        )
    except TwilioRestException as exc:
        raise _wrap(exc) from exc


def delete_credential(list_sid: str, cred_sid: str) -> None:
    try:
        get_client().sip.credential_lists(list_sid).credentials(cred_sid).delete()
    except TwilioRestException as exc:
        raise _wrap(exc) from exc


def _validate_username(username: str) -> None:
    if not username or len(username) > 32 or " " in username:
        raise SipAdminError(
            "username must be 1-32 chars and contain no spaces",
            status=400,
        )


def _validate_password(password: str) -> None:
    """Twilio's published rules: ≥12 chars, ≥1 digit, mixed case."""
    if not password or len(password) < 12:
        raise SipAdminError(
            "password must be at least 12 characters long",
            status=400,
        )
    if not any(ch.isdigit() for ch in password):
        raise SipAdminError("password must contain at least one digit", status=400)
    if password.lower() == password or password.upper() == password:
        raise SipAdminError("password must contain mixed-case letters", status=400)


# =========================================================================== #
#  Mappings — Calls auth (digest-user / source-IP) + REGISTER auth
# =========================================================================== #

def list_credential_list_mappings(domain_sid: str, limit: Optional[int] = 50) -> List[Dict[str, Any]]:
    """Calls auth: CredentialList ↔ SIP Domain (INVITE digest-auth)."""
    try:
        return [
            _mapping_dict(m)
            for m in get_client().sip.domains(domain_sid)
            .auth.calls.credential_list_mappings.list(limit=_coerce_limit(limit))
        ]
    except TwilioRestException as exc:
        raise _wrap(exc) from exc


def create_credential_list_mapping(domain_sid: str, *, credential_list_sid: str) -> Dict[str, Any]:
    try:
        return _mapping_dict(
            get_client().sip.domains(domain_sid)
            .auth.calls.credential_list_mappings.create(credential_list_sid=credential_list_sid)
        )
    except TwilioRestException as exc:
        raise _wrap(exc) from exc


def delete_credential_list_mapping(domain_sid: str, mapping_sid: str) -> None:
    try:
        get_client().sip.domains(domain_sid).auth.calls.credential_list_mappings(mapping_sid).delete()
    except TwilioRestException as exc:
        raise _wrap(exc) from exc


def list_ip_access_control_list_mappings(domain_sid: str,
                                         limit: Optional[int] = 50) -> List[Dict[str, Any]]:
    """Calls auth: IpAccessControlList ↔ SIP Domain (INVITE source-IP)."""
    try:
        return [
            _mapping_dict(m)
            for m in get_client().sip.domains(domain_sid)
            .auth.calls.ip_access_control_list_mappings.list(limit=_coerce_limit(limit))
        ]
    except TwilioRestException as exc:
        raise _wrap(exc) from exc


def create_ip_access_control_list_mapping(domain_sid: str,
                                          *, ip_access_control_list_sid: str) -> Dict[str, Any]:
    try:
        return _mapping_dict(
            get_client().sip.domains(domain_sid)
            .auth.calls.ip_access_control_list_mappings
            .create(ip_access_control_list_sid=ip_access_control_list_sid)
        )
    except TwilioRestException as exc:
        raise _wrap(exc) from exc


def delete_ip_access_control_list_mapping(domain_sid: str, mapping_sid: str) -> None:
    try:
        get_client().sip.domains(domain_sid).auth.calls.ip_access_control_list_mappings(mapping_sid).delete()
    except TwilioRestException as exc:
        raise _wrap(exc) from exc


def list_registration_credential_list_mappings(domain_sid: str,
                                               limit: Optional[int] = 50) -> List[Dict[str, Any]]:
    """REGISTER auth: CredentialList ↔ SIP Domain (digest-auth on REGISTER)."""
    try:
        return [
            _mapping_dict(m)
            for m in get_client().sip.domains(domain_sid)
            .auth.registrations.credential_list_mappings.list(limit=_coerce_limit(limit))
        ]
    except TwilioRestException as exc:
        raise _wrap(exc) from exc


def create_registration_credential_list_mapping(domain_sid: str,
                                                *, credential_list_sid: str) -> Dict[str, Any]:
    try:
        return _mapping_dict(
            get_client().sip.domains(domain_sid)
            .auth.registrations.credential_list_mappings.create(
                credential_list_sid=credential_list_sid,
            )
        )
    except TwilioRestException as exc:
        raise _wrap(exc) from exc


def delete_registration_credential_list_mapping(domain_sid: str, mapping_sid: str) -> None:
    try:
        get_client().sip.domains(domain_sid).auth.registrations.credential_list_mappings(mapping_sid).delete()
    except TwilioRestException as exc:
        raise _wrap(exc) from exc


# =========================================================================== #
#  Convenience — fully-wired up Domain in one call (recipe from sec. 26)
# =========================================================================== #

def provision_domain(*,
                     domain_name: str,
                     friendly_name: Optional[str],
                     voice_url: str,
                     allow_registration: bool = True,
                     secure: bool = True,
                     credential_list_friendly_name: Optional[str] = None,
                     credentials: Optional[List[Dict[str, str]]] = None,
                     acl_friendly_name: Optional[str] = None,
                     allowed_ips: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """One-shot helper that:

      1. Creates a SIP Domain (with voice_url → your TwiML handler)
      2. Optionally creates a CredentialList + Credentials and maps it to the
         Domain for INVITE auth (and REGISTER auth if ``allow_registration``)
      3. Optionally creates an IpAccessControlList + IpAddresses and maps it
         to the Domain for INVITE source-IP auth

    Returns ``{ "domain": {...}, "credential_list": {...} | None,
                "acl": {...} | None, "mappings": {...} }``.

    All Twilio errors are wrapped in :class:`SipAdminError`. We do NOT roll
    back partial work — the caller can re-run with the same `domain_name`
    and inspect what already exists (Twilio rejects duplicates).
    """
    domain = create_domain(
        domain_name=domain_name,
        friendly_name=friendly_name,
        voice_url=voice_url,
        voice_method="POST",
        sip_registration=allow_registration,
        secure=secure,
    )
    domain_sid = domain["sid"]

    out: Dict[str, Any] = {
        "domain": domain,
        "credential_list": None,
        "acl": None,
        "mappings": {
            "calls_credential_list_mapping": None,
            "calls_ip_acl_mapping": None,
            "registration_credential_list_mapping": None,
        },
    }

    if credentials:
        clist = create_credential_list(
            friendly_name=credential_list_friendly_name or f"{friendly_name or domain_name} users",
        )
        for cred in credentials:
            create_credential(clist["sid"],
                              username=str(cred.get("username", "")).strip(),
                              password=str(cred.get("password", "")))
        out["credential_list"] = clist
        out["mappings"]["calls_credential_list_mapping"] = create_credential_list_mapping(
            domain_sid, credential_list_sid=clist["sid"],
        )
        if allow_registration:
            out["mappings"]["registration_credential_list_mapping"] = (
                create_registration_credential_list_mapping(
                    domain_sid, credential_list_sid=clist["sid"],
                )
            )

    if allowed_ips:
        acl = create_acl(friendly_name=acl_friendly_name or f"{friendly_name or domain_name} edge IPs")
        for entry in allowed_ips:
            create_ip(
                acl["sid"],
                friendly_name=str(entry.get("friendly_name", "")).strip() or entry.get("ip_address", ""),
                ip_address=str(entry.get("ip_address", "")).strip(),
                cidr_prefix_length=entry.get("cidr_prefix_length"),
            )
        out["acl"] = acl
        out["mappings"]["calls_ip_acl_mapping"] = create_ip_access_control_list_mapping(
            domain_sid, ip_access_control_list_sid=acl["sid"],
        )

    return out


# =========================================================================== #
#  Emergency Calling helpers (Address + IncomingPhoneNumber tweaks)
# =========================================================================== #

def list_addresses(limit: Optional[int] = 50) -> List[Dict[str, Any]]:
    try:
        return [_address_dict(a) for a in get_client().addresses.list(limit=_coerce_limit(limit))]
    except TwilioRestException as exc:
        raise _wrap(exc) from exc


def create_address(*,
                   customer_name: str, friendly_name: str,
                   street: str, city: str, region: str, postal_code: str,
                   iso_country: str, emergency_enabled: bool = True) -> Dict[str, Any]:
    try:
        return _address_dict(get_client().addresses.create(
            customer_name=customer_name, friendly_name=friendly_name,
            street=street, city=city, region=region,
            postal_code=postal_code, iso_country=iso_country,
            emergency_enabled=emergency_enabled,
        ))
    except TwilioRestException as exc:
        raise _wrap(exc) from exc


def delete_address(sid: str) -> None:
    try:
        get_client().addresses(sid).delete()
    except TwilioRestException as exc:
        raise _wrap(exc) from exc


def list_phone_numbers(limit: Optional[int] = 100) -> List[Dict[str, Any]]:
    try:
        return [
            _phone_number_dict(p)
            for p in get_client().incoming_phone_numbers.list(limit=_coerce_limit(limit))
        ]
    except TwilioRestException as exc:
        raise _wrap(exc) from exc


def assign_emergency_address(phone_number_sid: str, *, emergency_address_sid: Optional[str]) -> Dict[str, Any]:
    """Pass ``None`` (or empty string) for ``emergency_address_sid`` to unregister."""
    try:
        return _phone_number_dict(
            get_client().incoming_phone_numbers(phone_number_sid)
            .update(emergency_address_sid=emergency_address_sid or "")
        )
    except TwilioRestException as exc:
        raise _wrap(exc) from exc
