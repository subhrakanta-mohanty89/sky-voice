"""SQLAlchemy 2.0 engine, ORM model + schema bootstrap.

Why SQLAlchemy:
    The original prototype used raw psycopg cursors, which is fine for one
    table but doesn't scale to the calls/recordings/teams models we'll add
    later. Moving to SQLAlchemy now keeps the API surface identical (still
    just import ``user_store``) while giving us migrations, query builder,
    transactions, and a clean place to hang relationships.

Design:
    * Single global ``engine`` bound to ``DATABASE_URL`` (psycopg3 driver).
    * Session factory returns short-lived sessions used inside ``with``
      blocks — no Flask request scope coupling, so the same code is
      usable from tests and background tasks.
    * Idempotent :func:`init_schema` runs ``Base.metadata.create_all``.
    * Everything is optional — if ``DATABASE_URL`` is empty the helpers
      return ``None`` and :class:`app.models.UserStore` falls back to its
      in-memory implementation.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Iterator, Optional

from sqlalchemy import Boolean, Engine, Float, Integer, String, Text, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from config import settings

logger = logging.getLogger(__name__)

# The bootstrap tenant. Every legacy (pre-multi-tenancy) user/SIM is migrated
# onto it, and it inherits its Twilio/Sara configuration from the process-wide
# ``.env`` so an existing single-tenant deployment keeps working untouched.
DEFAULT_TENANT_ID = "default"

_engine: Optional[Engine] = None
_SessionLocal: Optional[sessionmaker[Session]] = None
_engine_lock = threading.Lock()


# --------------------------------------------------------------------------- #
#  ORM base + models
# --------------------------------------------------------------------------- #


class Base(DeclarativeBase):
    """Declarative base for every ORM model in the project."""


class UserORM(Base):
    """Persisted shape of the application user.

    Mirrors :class:`app.models.User` so the route layer keeps working
    with the existing dataclass. ``email_lower`` is the unique key for
    case-insensitive lookups; the original casing is preserved in
    ``email`` for display.
    """

    __tablename__ = "users"

    id:               Mapped[str]             = mapped_column(String, primary_key=True)
    # Owning tenant (workspace). NULL only for rows created before
    # multi-tenancy shipped; those are migrated onto the default tenant.
    tenant_id:        Mapped[Optional[str]]   = mapped_column(String, nullable=True, index=True)
    email:            Mapped[str]             = mapped_column(String, nullable=False)
    email_lower:      Mapped[str]             = mapped_column(String, nullable=False, unique=True, index=True)
    password_hash:    Mapped[str]             = mapped_column(String, nullable=False)
    full_name:        Mapped[str]             = mapped_column(String, nullable=False)
    role:             Mapped[str]             = mapped_column(String, nullable=False, default="member", index=True)
    status:           Mapped[str]             = mapped_column(String, nullable=False, default="active", index=True)
    phone:            Mapped[Optional[str]]   = mapped_column(String, nullable=True)
    organization:     Mapped[Optional[str]]   = mapped_column(String, nullable=True)
    avatar_initials:  Mapped[Optional[str]]   = mapped_column(String, nullable=True)
    invited_by:       Mapped[Optional[str]]   = mapped_column(String, nullable=True)
    created_at:       Mapped[float]           = mapped_column(Float, nullable=False)
    updated_at:       Mapped[float]           = mapped_column(Float, nullable=False)
    last_seen_at:     Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Email verification + OTP
    email_verified:   Mapped[bool]            = mapped_column(Boolean, nullable=False, default=False)
    otp_code_hash:    Mapped[Optional[str]]   = mapped_column(String, nullable=True)
    otp_expires_at:   Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    otp_last_sent_at: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    otp_attempts:     Mapped[int]             = mapped_column(Integer, nullable=False, default=0)


class SimORM(Base):
    """Persisted SIM / Twilio phone number the workspace can dial from.

    Each row is one outbound caller ID. ``phone_number`` is E.164 and
    unique. ``source`` is either ``"twilio"`` (auto-imported from the
    Twilio account via the SIMs sync endpoint) or ``"manual"`` (added by
    an admin via the UI). ``twilio_sid`` is the IncomingPhoneNumber SID
    when known, else ``None`` for manual entries.
    """

    __tablename__ = "sims"

    id:           Mapped[str]             = mapped_column(String, primary_key=True)
    # Owning tenant (workspace). NULL rows are migrated onto the default tenant.
    tenant_id:    Mapped[Optional[str]]   = mapped_column(String, nullable=True, index=True)
    phone_number: Mapped[str]             = mapped_column(String, nullable=False, unique=True, index=True)
    label:        Mapped[str]             = mapped_column(String, nullable=False)
    is_default:   Mapped[bool]            = mapped_column(Boolean, nullable=False, default=False, index=True)
    source:       Mapped[str]             = mapped_column(String, nullable=False, default="manual")
    twilio_sid:   Mapped[Optional[str]]   = mapped_column(String, nullable=True)
    created_at:   Mapped[float]           = mapped_column(Float, nullable=False)
    updated_at:   Mapped[float]           = mapped_column(Float, nullable=False)


class TenantORM(Base):
    """A tenant (workspace / organisation).

    Each tenant owns its own users, SIMs, agents, calls and — crucially —
    its own Twilio credentials and Sara (AI receptionist) configuration.
    All credential/config columns are nullable; when blank the application
    falls back to the process-wide ``.env`` defaults, which is what keeps
    the bootstrap "default" tenant working on an existing single-tenant
    deployment.
    """

    __tablename__ = "tenants"

    id:           Mapped[str]             = mapped_column(String, primary_key=True)
    name:         Mapped[str]             = mapped_column(String, nullable=False)
    slug:         Mapped[str]             = mapped_column(String, nullable=False, unique=True, index=True)
    status:       Mapped[str]             = mapped_column(String, nullable=False, default="active", index=True)
    created_at:   Mapped[float]           = mapped_column(Float, nullable=False)
    updated_at:   Mapped[float]           = mapped_column(Float, nullable=False)

    # --- Twilio credentials (per tenant) ---
    twilio_account_sid:     Mapped[Optional[str]] = mapped_column(String, nullable=True)
    twilio_auth_token:      Mapped[Optional[str]] = mapped_column(String, nullable=True)
    twilio_api_key_sid:     Mapped[Optional[str]] = mapped_column(String, nullable=True)
    twilio_api_key_secret:  Mapped[Optional[str]] = mapped_column(String, nullable=True)
    twilio_twiml_app_sid:   Mapped[Optional[str]] = mapped_column(String, nullable=True)
    twilio_caller_id:       Mapped[Optional[str]] = mapped_column(String, nullable=True)
    public_base_url:        Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # --- Sara (AI receptionist) config (per tenant) ---
    sara_company_name:      Mapped[Optional[str]] = mapped_column(String, nullable=True)
    sara_summary_to:        Mapped[Optional[str]] = mapped_column(String, nullable=True)
    sara_knowledge_pdf:     Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Extracted plain text of an uploaded knowledge book. Stored in the DB
    # (not on disk) so it survives Cloud Run's ephemeral, per-instance
    # filesystem and is shared across every container instance.
    sara_knowledge_text:    Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sara_tts_voice:         Mapped[Optional[str]] = mapped_column(String, nullable=True)
    sara_stt_model:         Mapped[Optional[str]] = mapped_column(String, nullable=True)
    sara_deepgram_api_key:  Mapped[Optional[str]] = mapped_column(String, nullable=True)
    sara_ai_api_key:        Mapped[Optional[str]] = mapped_column(String, nullable=True)
    sara_ai_model:          Mapped[Optional[str]] = mapped_column(String, nullable=True)
    sara_ai_base_url:       Mapped[Optional[str]] = mapped_column(String, nullable=True)
    sara_answer_first:      Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    sara_auto_fallback:     Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)


class SipConfigORM(Base):
    """Per-tenant SIP infrastructure settings (one row per tenant).

    The actual SIP resources live in the tenant's Twilio account; this row
    records which ones this workspace uses so the UI/routing layer doesn't
    have to re-discover them on every request.
    """

    __tablename__ = "sip_configs"

    id:                  Mapped[str]           = mapped_column(String, primary_key=True)
    tenant_id:           Mapped[str]           = mapped_column(String, nullable=False, unique=True, index=True)
    domain_sid:          Mapped[Optional[str]] = mapped_column(String, nullable=True)
    domain_name:         Mapped[Optional[str]] = mapped_column(String, nullable=True)
    credential_list_sid: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    ip_acl_sid:          Mapped[Optional[str]] = mapped_column(String, nullable=True)
    voice_url:           Mapped[Optional[str]] = mapped_column(String, nullable=True)
    default_from_number: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at:          Mapped[float]         = mapped_column(Float, nullable=False)
    updated_at:          Mapped[float]         = mapped_column(Float, nullable=False)


class CallORM(Base):
    """Durable per-tenant call history.

    Active calls live in the in-memory :class:`app.models.CallStore` for
    speed; completed calls are written here on hang-up so the call-history
    page survives restarts and stays scoped to the owning tenant.
    """

    __tablename__ = "calls"

    call_sid:          Mapped[str]             = mapped_column(String, primary_key=True)
    tenant_id:         Mapped[str]             = mapped_column(String, nullable=False, index=True)
    direction:         Mapped[str]             = mapped_column(String, nullable=False)
    from_number:       Mapped[str]             = mapped_column(String, nullable=False, default="")
    to_number:         Mapped[str]             = mapped_column(String, nullable=False, default="")
    status:            Mapped[str]             = mapped_column(String, nullable=False, default="completed")
    agent_identity:    Mapped[Optional[str]]   = mapped_column(String, nullable=True)
    parent_call_sid:   Mapped[Optional[str]]   = mapped_column(String, nullable=True)
    service_code:      Mapped[Optional[str]]   = mapped_column(String, nullable=True)
    service_label:     Mapped[Optional[str]]   = mapped_column(String, nullable=True)
    recording_url:     Mapped[Optional[str]]   = mapped_column(String, nullable=True)
    recording_sid:     Mapped[Optional[str]]   = mapped_column(String, nullable=True)
    recording_duration:Mapped[Optional[int]]   = mapped_column(Integer, nullable=True)
    started_at:        Mapped[float]           = mapped_column(Float, nullable=False)
    answered_at:       Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ended_at:          Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    duration_seconds:  Mapped[Optional[int]]   = mapped_column(Integer, nullable=True)
    ended_by:          Mapped[Optional[str]]   = mapped_column(String, nullable=True)


class AgentORM(Base):
    """Durable per-tenant agent roster.

    Volatile state (``current_call_sid``, ``last_seen``) stays in memory;
    only the stable roster fields are persisted so teammates survive a
    restart. ``identity`` is unique *within* a tenant, not globally — the
    surrogate ``id`` (``"<tenant_id>:<identity>"``) is the primary key.
    """

    __tablename__ = "agents"

    id:         Mapped[str]           = mapped_column(String, primary_key=True)
    tenant_id:  Mapped[str]           = mapped_column(String, nullable=False, index=True)
    identity:   Mapped[str]           = mapped_column(String, nullable=False)
    name:       Mapped[str]           = mapped_column(String, nullable=False)
    role:       Mapped[str]           = mapped_column(String, nullable=False, default="agent")
    presence:   Mapped[str]           = mapped_column(String, nullable=False, default="offline")
    email:      Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[float]         = mapped_column(Float, nullable=False)
    updated_at: Mapped[float]         = mapped_column(Float, nullable=False)


# --------------------------------------------------------------------------- #
#  Engine lifecycle
# --------------------------------------------------------------------------- #


def _normalize_url(url: str) -> str:
    """Force the psycopg3 driver dialect (SQLAlchemy still defaults to psycopg2)."""
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    return url


def engine_or_none() -> Optional[Engine]:
    """Return the singleton engine, or ``None`` when DATABASE_URL is unset."""
    global _engine, _SessionLocal
    if not settings.database_url:
        return None
    if _engine is not None:
        return _engine
    with _engine_lock:
        if _engine is not None:
            return _engine
        logger.info("Opening SQLAlchemy engine")
        _engine = create_engine(
            _normalize_url(settings.database_url),
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
            pool_recycle=900,
            future=True,
        )
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


# Backwards-compat alias for any older imports of ``pool_or_none``.
def pool_or_none() -> Optional[Engine]:  # pragma: no cover
    return engine_or_none()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Yield a session, commit on success, rollback on error, always close.

    Usage::

        with session_scope() as s:
            user = s.get(UserORM, "usr_abc")
    """
    engine = engine_or_none()
    if engine is None or _SessionLocal is None:
        raise RuntimeError("No database engine — DATABASE_URL is not set")
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def close_pool() -> None:
    """Dispose of the engine on shutdown (used by tests + graceful restarts)."""
    global _engine, _SessionLocal
    with _engine_lock:
        if _engine is not None:
            _engine.dispose()
            _engine = None
            _SessionLocal = None


# --------------------------------------------------------------------------- #
#  Schema bootstrap
# --------------------------------------------------------------------------- #


def init_schema() -> bool:
    """Create every table declared on ``Base.metadata``. Idempotent.

    Returns ``True`` when the DB is wired and the schema is in place,
    ``False`` when DATABASE_URL is empty (caller should fall back to the
    in-memory store).
    """
    engine = engine_or_none()
    if engine is None:
        logger.info("DATABASE_URL not set — using in-memory user store")
        return False
    try:
        Base.metadata.create_all(engine)
        # Idempotent column-level migrations for existing tables. ``create_all``
        # only creates missing tables, not missing columns, so any new field
        # added to UserORM after the table was first deployed needs an
        # ALTER TABLE here.
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT FALSE"
            ))
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS otp_code_hash TEXT"
            ))
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS otp_expires_at DOUBLE PRECISION"
            ))
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS otp_last_sent_at DOUBLE PRECISION"
            ))
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS otp_attempts INTEGER NOT NULL DEFAULT 0"
            ))
            # Grandfather every account that was created before the OTP
            # feature shipped (i.e. accounts with no OTP fields ever set) so
            # they don't get locked out on the next login.
            conn.execute(text(
                "UPDATE users SET email_verified = TRUE "
                "WHERE email_verified = FALSE AND otp_code_hash IS NULL "
                "  AND otp_last_sent_at IS NULL"
            ))
            # --- Multi-tenancy migration -------------------------------- #
            # Add the tenant_id foreign-key columns to the pre-existing
            # users/sims tables (create_all only adds whole tables, never
            # new columns to existing ones).
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS tenant_id TEXT"
            ))
            conn.execute(text(
                "ALTER TABLE sims ADD COLUMN IF NOT EXISTS tenant_id TEXT"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_users_tenant_id ON users (tenant_id)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_sims_tenant_id ON sims (tenant_id)"
            ))
            # Backfill every legacy user/sim onto the default tenant so a
            # pre-multi-tenant deployment keeps working unchanged.
            conn.execute(text(
                "UPDATE users SET tenant_id = :tid WHERE tenant_id IS NULL"
            ), {"tid": DEFAULT_TENANT_ID})
            conn.execute(text(
                "UPDATE sims SET tenant_id = :tid WHERE tenant_id IS NULL"
            ), {"tid": DEFAULT_TENANT_ID})
            # A tenant's uploaded knowledge book is stored as extracted text
            # in the DB. Idempotent in case the tenants table predates it.
            conn.execute(text(
                "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS sara_knowledge_text TEXT"
            ))
        logger.info("SQLAlchemy schema is ready (users + tenants tables present)")
        return True
    except Exception:
        logger.exception("Failed to initialise SQLAlchemy schema")
        raise
