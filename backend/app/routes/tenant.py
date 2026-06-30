"""Per-tenant workspace settings (admin only).

Lets a workspace admin manage their *own* Twilio credentials and Sara
(AI receptionist) configuration from the UI instead of editing ``.env``:

  * Twilio: account SID, auth token, API key SID/secret, TwiML app SID,
    caller ID, public base URL.
  * Sara: company name, summary recipients, voice, answer-first /
    auto-fallback toggles, and an uploadable knowledge book (PDF).

Design notes
------------
* **Secrets are never echoed back.** ``GET`` reports them as booleans
  (``authTokenSet`` etc.) and ``PUT`` only overwrites a secret when a
  non-empty replacement is supplied (send JSON ``null`` to clear it and
  fall back to the platform ``.env`` value).
* **Platform credentials stay in ``.env``.** The OpenAI/Deepgram keys are
  deliberately *not* exposed here — every tenant shares the platform's
  STT/LLM infrastructure; only the per-customer Twilio account and Sara
  persona are configurable.
* **The knowledge book is stored as extracted text in the DB**, not on
  disk, so it survives Cloud Run's ephemeral per-instance filesystem.
"""

from __future__ import annotations

import io
import logging

from flask import Blueprint, g, request

from app.models import tenant_store
from app.services.auth_service import require_admin
from app.services.tenant_context import admin_emails, get_tenant
from app.utils import fail, ok

logger = logging.getLogger(__name__)

tenant_bp = Blueprint("tenant", __name__)

# 8 MB is plenty for a text-heavy knowledge book and keeps a single
# request from ballooning the process's memory.
MAX_KNOWLEDGE_BYTES = 8 * 1024 * 1024

# Plain (non-secret) string columns the UI may set. Empty string clears the
# override (reverting to the process-wide ``.env`` default); JSON ``null`` is
# treated the same.
_STRING_FIELDS = {
    "name":              "name",
    "twilioAccountSid":  "twilio_account_sid",
    "twilioApiKeySid":   "twilio_api_key_sid",
    "twilioTwimlAppSid": "twilio_twiml_app_sid",
    "twilioCallerId":    "twilio_caller_id",
    "publicBaseUrl":     "public_base_url",
    "saraCompanyName":   "sara_company_name",
    "saraSummaryTo":     "sara_summary_to",
    "saraTtsVoice":      "sara_tts_voice",
    "saraSttModel":      "sara_stt_model",
}

# Secret columns: only overwritten when a non-empty string is supplied.
# Send JSON ``null`` to explicitly clear (fall back to ``.env``).
_SECRET_FIELDS = {
    "twilioAuthToken":     "twilio_auth_token",
    "twilioApiKeySecret":  "twilio_api_key_secret",
}

# Tri-state booleans: true / false / null (null → inherit the env default).
_BOOL_FIELDS = {
    "saraAnswerFirst":  "sara_answer_first",
    "saraAutoFallback": "sara_auto_fallback",
}


def _tenant_payload() -> dict:
    """The admin-facing settings shape for the caller's tenant."""
    t = get_tenant(g.current_tenant_id)
    data = t.to_public()
    # Every company admin automatically receives Sara's call summaries —
    # surface that list so the UI can show who's notified.
    data["adminEmails"] = admin_emails(g.current_tenant_id)
    return data


@tenant_bp.get("/tenant/settings")
@require_admin
def get_tenant_settings():
    return ok({"tenant": _tenant_payload()})


class _BadField(Exception):
    """A settings field had the wrong type."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def _apply_string_fields(body: dict, updates: dict) -> None:
    for key, column in _STRING_FIELDS.items():
        if key not in body:
            continue
        raw = body[key]
        if raw is None:
            value = None
        elif isinstance(raw, str):
            value = raw.strip() or None
        else:
            raise _BadField(key)
        # The workspace name can't be blanked out.
        if column == "name" and not value:
            continue
        updates[column] = value


def _apply_secret_fields(body: dict, updates: dict) -> None:
    for key, column in _SECRET_FIELDS.items():
        if key not in body:
            continue
        raw = body[key]
        if raw is None:
            updates[column] = None           # explicit clear → fall back to env
        elif isinstance(raw, str):
            trimmed = raw.strip()
            if trimmed:                       # blank = leave unchanged
                updates[column] = trimmed
        else:
            raise _BadField(key)


def _apply_bool_fields(body: dict, updates: dict) -> None:
    for key, column in _BOOL_FIELDS.items():
        if key not in body:
            continue
        raw = body[key]
        if raw is None or isinstance(raw, bool):
            updates[column] = raw
        else:
            raise _BadField(key)


@tenant_bp.put("/tenant/settings")
@require_admin
def update_tenant_settings():
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return fail("invalid_body", status=400)

    updates: dict = {}
    try:
        _apply_string_fields(body, updates)
        _apply_secret_fields(body, updates)
        _apply_bool_fields(body, updates)
    except _BadField as exc:
        return fail("invalid_field", status=400, detail=f"{exc.key} has the wrong type")

    if updates:
        tenant_store.update(g.current_tenant_id, **updates)

    return ok({"tenant": _tenant_payload()})


def _extract_pdf_text(data: bytes) -> str:
    """Best-effort plain-text extraction from a PDF byte string."""
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover - dependency always present in prod
        return ""
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [(p.extract_text() or "") for p in reader.pages]
        return "\n".join(pages).strip()
    except Exception:  # noqa: BLE001
        logger.exception("knowledge-book PDF parse failed")
        return ""


@tenant_bp.post("/tenant/knowledge-book")
@require_admin
def upload_knowledge_book():
    """Upload a PDF knowledge book for the caller's tenant.

    The PDF is parsed to text and stored in the DB; Sara rebuilds her BM25
    index from it automatically on the next off-script question.
    """
    file = request.files.get("file")
    if file is None or not file.filename:
        return fail("no_file", status=400, detail="Attach a PDF as form field 'file'.")

    data = file.read(MAX_KNOWLEDGE_BYTES + 1)
    if len(data) > MAX_KNOWLEDGE_BYTES:
        return fail("file_too_large", status=413,
                    detail="Knowledge book must be 8 MB or smaller.")
    if not data:
        return fail("empty_file", status=400)

    text = _extract_pdf_text(data)
    if not text:
        return fail("unreadable_pdf", status=422,
                    detail="Could not extract any text from that PDF.")

    filename = (file.filename or "knowledge.pdf").strip()[:200]
    tenant_store.update(
        g.current_tenant_id,
        sara_knowledge_pdf=filename,
        sara_knowledge_text=text,
    )
    logger.info(
        "knowledge-book uploaded tenant=%s file=%r chars=%d",
        g.current_tenant_id, filename, len(text),
    )
    return ok({"tenant": _tenant_payload()})


@tenant_bp.delete("/tenant/knowledge-book")
@require_admin
def delete_knowledge_book():
    """Remove the tenant's uploaded knowledge book (revert to the default)."""
    tenant_store.update(
        g.current_tenant_id,
        sara_knowledge_pdf=None,
        sara_knowledge_text=None,
    )
    return ok({"tenant": _tenant_payload()})
