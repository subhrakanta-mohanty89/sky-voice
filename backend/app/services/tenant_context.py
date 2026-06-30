"""Per-tenant context resolution + effective configuration.

This is the single source of truth for two questions:

  1. *Which tenant is this request / call for?*
     - REST requests   → the logged-in user's tenant (``g.current_tenant_id``,
       set by the auth decorators).
     - Twilio webhooks → resolved from the dialed number (``To`` → SIM →
       tenant) or from the active call (``CallSid`` → call → tenant).

  2. *What are the effective Twilio / Sara settings for that tenant?*
     A tenant column when it's set, otherwise the process-wide ``.env``
     default. That fallback is what keeps the bootstrap ``default`` tenant
     (and any partially-configured tenant) working unchanged.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import List, Optional

from app.db import DEFAULT_TENANT_ID
from app.models import Tenant, call_store, sim_store, tenant_store, user_store
from config import settings

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
#  Tenant lookup / resolution
# --------------------------------------------------------------------------- #

def seed_default_tenant() -> None:
    """Ensure the bootstrap ``default`` tenant row exists.

    Its credential/config columns are left blank on purpose so the default
    tenant always inherits the *live* ``.env`` values rather than a snapshot.
    """
    try:
        if tenant_store.get(DEFAULT_TENANT_ID) is None:
            tenant_store.create(Tenant(
                id=DEFAULT_TENANT_ID, name="Default Workspace", slug="default",
            ))
    except Exception:  # noqa: BLE001
        logger.exception("seed_default_tenant failed (non-fatal)")


def get_tenant(tenant_id: Optional[str]) -> Tenant:
    """Load a tenant, synthesising an all-blank object when missing so the
    caller always gets something usable (which then falls back to env)."""
    tid = tenant_id or DEFAULT_TENANT_ID
    t = tenant_store.get(tid)
    if t is not None:
        return t
    return Tenant(id=tid, name="Default Workspace", slug=tid)


def current_tenant_id() -> str:
    """Tenant id for the active request, falling back to the default tenant.

    Safe to call outside a request context (e.g. background threads) — it
    simply returns the default tenant id then.
    """
    try:
        from flask import g, has_request_context
        if has_request_context():
            tid = getattr(g, "current_tenant_id", None)
            if tid:
                return tid
    except Exception:  # noqa: BLE001
        pass
    return DEFAULT_TENANT_ID


def current_tenant() -> Tenant:
    return get_tenant(current_tenant_id())


def resolve_tenant_id_for_number(to_number: str) -> str:
    """Map a dialed Twilio number (a webhook ``To``) to its owning tenant."""
    if to_number:
        sim = sim_store.get_by_number(to_number)
        if sim is not None:
            return sim.tenant_id
    return DEFAULT_TENANT_ID


def resolve_tenant_id_for_call(call_sid: str) -> str:
    """Map an active CallSid to its owning tenant via the call store."""
    if call_sid:
        call = call_store.get(call_sid)
        if call is not None:
            return call.tenant_id
    return DEFAULT_TENANT_ID


def admin_emails(tenant_id: Optional[str] = None) -> List[str]:
    """Every admin user's email for ``tenant_id`` (summary recipients from DB)."""
    tid = tenant_id or current_tenant_id()
    try:
        users = user_store.list_all(tenant_id=tid)
    except Exception:  # noqa: BLE001
        logger.exception("admin_emails lookup failed for tenant %s", tid)
        return []
    return [u.email for u in users if u.role == "admin" and u.email]


def summary_recipients(tenant_id: Optional[str] = None, extra: str = "") -> List[str]:
    """Who should receive Sara's call summary for a tenant.

    Always includes every company admin's email (pulled live from the DB),
    plus any extra comma/semicolon-separated addresses configured on the
    tenant. De-duplicated case-insensitively, original casing preserved.
    """
    tid = tenant_id or current_tenant_id()
    seen: dict[str, str] = {}
    for addr in admin_emails(tid):
        seen.setdefault(addr.lower(), addr)
    for addr in re.split(r"[,;]", extra or ""):
        a = addr.strip()
        if a:
            seen.setdefault(a.lower(), a)
    return list(seen.values())


# --------------------------------------------------------------------------- #
#  Effective configuration (tenant value, else env fallback)
# --------------------------------------------------------------------------- #

def _first(*vals):
    for v in vals:
        if v not in (None, ""):
            return v
    return None


@dataclass(frozen=True)
class TwilioCreds:
    account_sid: str
    auth_token: str
    api_key_sid: str
    api_key_secret: str
    twiml_app_sid: str
    caller_id: str
    public_base_url: str

    def is_configured(self) -> bool:
        return bool(
            self.account_sid and self.auth_token and self.api_key_sid
            and self.api_key_secret and self.twiml_app_sid
        )

    def webhook_url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.public_base_url}{path}"


def twilio_creds(tenant: Optional[Tenant] = None) -> TwilioCreds:
    """Effective Twilio credentials for ``tenant`` (or the current request)."""
    t = tenant or current_tenant()
    return TwilioCreds(
        account_sid=_first(t.twilio_account_sid, settings.twilio_account_sid) or "",
        auth_token=_first(t.twilio_auth_token, settings.twilio_auth_token) or "",
        api_key_sid=_first(t.twilio_api_key_sid, settings.twilio_api_key_sid) or "",
        api_key_secret=_first(t.twilio_api_key_secret, settings.twilio_api_key_secret) or "",
        twiml_app_sid=_first(t.twilio_twiml_app_sid, settings.twilio_twiml_app_sid) or "",
        caller_id=_first(t.twilio_caller_id, settings.twilio_caller_id) or "",
        public_base_url=(_first(t.public_base_url, settings.public_base_url) or "").rstrip("/"),
    )


@dataclass(frozen=True)
class SaraConfig:
    company_name: str
    summary_to: str
    knowledge_pdf: str
    knowledge_text: str
    tts_voice: str
    stt_model: str
    deepgram_api_key: str
    ai_api_key: str
    ai_model: str
    ai_base_url: str
    answer_first: bool
    auto_fallback: bool

    def is_configured(self) -> bool:
        """Sara needs at minimum a Deepgram key; the LLM is optional."""
        return bool(self.deepgram_api_key)


def sara_config(tenant: Optional[Tenant] = None) -> SaraConfig:
    """Effective Sara (AI receptionist) config for ``tenant``."""
    t = tenant or current_tenant()
    return SaraConfig(
        company_name=_first(t.sara_company_name, settings.ivr_company_name) or "",
        summary_to=_first(t.sara_summary_to, settings.sara_summary_to) or "",
        knowledge_pdf=_first(t.sara_knowledge_pdf, settings.sara_knowledge_pdf) or "",
        knowledge_text=(t.sara_knowledge_text or ""),
        tts_voice=_first(t.sara_tts_voice, settings.tts_voice) or "",
        stt_model=_first(t.sara_stt_model, settings.stt_model) or "",
        deepgram_api_key=_first(t.sara_deepgram_api_key, settings.deepgram_api_key) or "",
        ai_api_key=_first(t.sara_ai_api_key, settings.ai_api_key) or "",
        ai_model=_first(t.sara_ai_model, settings.ai_model) or "",
        ai_base_url=_first(t.sara_ai_base_url, settings.ai_base_url) or "",
        answer_first=(
            t.sara_answer_first if t.sara_answer_first is not None
            else settings.sara_answer_first
        ),
        auto_fallback=(
            t.sara_auto_fallback if t.sara_auto_fallback is not None
            else settings.sara_auto_fallback
        ),
    )
