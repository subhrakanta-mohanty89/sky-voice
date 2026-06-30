"""In-memory data stores.

These are intentionally simple — plain Python dicts/lists guarded by a
threading lock. They expose a small API surface that can be replaced by
SQLAlchemy / Redis later without touching the routes.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Literal, Optional

from .db import DEFAULT_TENANT_ID

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
#  Call model
# --------------------------------------------------------------------------- #

CallStatus = Literal[
    "queued",
    "initiated",
    "ringing",
    "in-progress",
    "completed",
    "busy",
    "no-answer",
    "failed",
    "canceled",
]
CallDirection = Literal["inbound", "outbound", "internal"]


@dataclass
class Call:
    """Single call (inbound or outbound) tracked by the backend."""

    call_sid: str
    direction: CallDirection
    from_number: str
    to_number: str
    status: CallStatus = "queued"
    # Owning tenant (workspace) this call belongs to.
    tenant_id: str = DEFAULT_TENANT_ID

    # Identity of the agent currently bridged in (Twilio Voice SDK identity).
    agent_identity: Optional[str] = None

    parent_call_sid: Optional[str] = None  # set on dialed child legs
    is_on_hold: bool = False
    hold_started_at: Optional[float] = None

    recording_url: Optional[str] = None
    recording_sid: Optional[str] = None
    recording_duration: Optional[int] = None

    started_at: float = field(default_factory=time.time)
    answered_at: Optional[float] = None
    ended_at: Optional[float] = None
    duration_seconds: Optional[int] = None
    ended_by: Optional[str] = None

    # IVR selection (set by /twilio/voice/ivr/handle when the caller picks
    # a menu option). Lets the operator UI show what the call is about
    # *before* they pick it up.
    service_code: Optional[str] = None  # e.g. "new-consultation"
    service_label: Optional[str] = None  # e.g. "New Legal Consultation"

    # Frontend-facing convenience aliases
    @property
    def call_uuid(self) -> str:
        return self.call_sid

    def to_dict(self) -> Dict:
        d = asdict(self)
        # Frontend uses "call_uuid", "from", "to", and "type"
        d["call_uuid"] = d["call_sid"]
        d["from"] = d["from_number"]
        d["to"] = d["to_number"]
        d["type"] = d["direction"]
        d["operator_connected"] = bool(self.agent_identity) and self.status == "in-progress"
        d["operator_answered"] = self.answered_at is not None
        d["websocket_url"] = ""  # populated by the route layer
        return d


class CallStore:
    """Thread-safe call repository (active + history)."""

    def __init__(self, history_capacity: int = 500) -> None:
        self._active: Dict[str, Call] = {}
        self._history: List[Call] = []
        self._capacity = history_capacity
        self._lock = threading.RLock()

    # ------------------------- mutators -------------------------- #
    def create(self, call: Call) -> Call:
        with self._lock:
            self._active[call.call_sid] = call
        return call

    def get(self, call_sid: str) -> Optional[Call]:
        with self._lock:
            return self._active.get(call_sid) or next(
                (c for c in self._history if c.call_sid == call_sid), None
            )

    def update(self, call_sid: str, **fields) -> Optional[Call]:
        with self._lock:
            call = self._active.get(call_sid)
            if call is None:
                # Maybe the update arrived after the call ended.
                call = next((c for c in self._history if c.call_sid == call_sid), None)
                if call is None:
                    return None
            for k, v in fields.items():
                if hasattr(call, k):
                    setattr(call, k, v)
            return call

    def active_count(self, tenant_id: Optional[str] = None) -> int:
        """Return the number of currently active (in-progress) calls."""
        with self._lock:
            if tenant_id:
                return sum(1 for c in self._active.values()
                           if c.tenant_id == tenant_id and c.status in ("ringing", "in-progress"))
            return sum(1 for c in self._active.values()
                       if c.status in ("ringing", "in-progress"))

    def end(self, call_sid: str, *, status: CallStatus = "completed",
            ended_by: Optional[str] = None) -> Optional[Call]:
        """Move a call from active → history (and persist it per-tenant)."""
        with self._lock:
            call = self._active.pop(call_sid, None)
            if call is None:
                return None
            call.status = status
            call.ended_at = time.time()
            call.ended_by = ended_by
            if call.answered_at:
                call.duration_seconds = int(call.ended_at - call.answered_at)
            self._history.insert(0, call)
            # Cap history.
            if len(self._history) > self._capacity:
                self._history = self._history[: self._capacity]
        # Best-effort durable write outside the lock so a slow DB never
        # stalls the call-control hot path.
        self._persist(call)
        return call

    @staticmethod
    def _persist(call: "Call") -> None:
        """Write the (usually completed) call into the per-tenant ``calls`` table."""
        from .db import engine_or_none
        if engine_or_none() is None:
            return
        try:
            from .db import CallORM, session_scope
            with session_scope() as s:
                row = s.get(CallORM, call.call_sid)
                if row is None:
                    row = CallORM(call_sid=call.call_sid, tenant_id=call.tenant_id)
                    s.add(row)
                row.tenant_id = call.tenant_id
                row.direction = call.direction
                row.from_number = call.from_number or ""
                row.to_number = call.to_number or ""
                row.status = call.status
                row.agent_identity = call.agent_identity
                row.parent_call_sid = call.parent_call_sid
                row.service_code = call.service_code
                row.service_label = call.service_label
                row.recording_url = call.recording_url
                row.recording_sid = call.recording_sid
                row.recording_duration = call.recording_duration
                row.started_at = call.started_at
                row.answered_at = call.answered_at
                row.ended_at = call.ended_at
                row.duration_seconds = call.duration_seconds
                row.ended_by = call.ended_by
        except Exception:  # noqa: BLE001
            logger.exception("Failed to persist call %s", call.call_sid)

    @staticmethod
    def _orm_to_call(row) -> "Call":  # type: ignore[no-untyped-def]
        return Call(
            call_sid=row.call_sid,
            direction=row.direction,
            from_number=row.from_number,
            to_number=row.to_number,
            status=row.status,
            tenant_id=row.tenant_id,
            agent_identity=row.agent_identity,
            parent_call_sid=row.parent_call_sid,
            service_code=row.service_code,
            service_label=row.service_label,
            recording_url=row.recording_url,
            recording_sid=row.recording_sid,
            recording_duration=row.recording_duration,
            started_at=float(row.started_at),
            answered_at=float(row.answered_at) if row.answered_at is not None else None,
            ended_at=float(row.ended_at) if row.ended_at is not None else None,
            duration_seconds=row.duration_seconds,
            ended_by=row.ended_by,
        )

    # ------------------------- accessors ------------------------- #
    def list_active(self, tenant_id: Optional[str] = None) -> List[Call]:
        with self._lock:
            calls = list(self._active.values())
        if tenant_id is not None:
            calls = [c for c in calls if c.tenant_id == tenant_id]
        return calls

    def list_history(self, limit: int = 50, *, tenant_id: Optional[str] = None) -> List[Call]:
        from .db import engine_or_none
        if engine_or_none() is not None:
            try:
                from sqlalchemy import select
                from .db import CallORM, session_scope
                with session_scope() as s:
                    stmt = select(CallORM).order_by(CallORM.started_at.desc())
                    if tenant_id is not None:
                        stmt = stmt.where(CallORM.tenant_id == tenant_id)
                    rows = s.execute(stmt.limit(limit)).scalars().all()
                    return [self._orm_to_call(r) for r in rows]
            except Exception:  # noqa: BLE001
                logger.exception("Failed to read call history from DB; using memory")
        with self._lock:
            items = list(self._history)
        if tenant_id is not None:
            items = [c for c in items if c.tenant_id == tenant_id]
        return items[:limit]

    def find_by_parent(self, parent_call_sid: str) -> Optional[Call]:
        with self._lock:
            for c in self._active.values():
                if c.parent_call_sid == parent_call_sid:
                    return c
        return None


# --------------------------------------------------------------------------- #
#  Agent model
# --------------------------------------------------------------------------- #

# Manual presence the agent picks (Available / On break / Offline). Distinct
# from the derived ``status`` field below, which also reflects auto-busy.
AgentPresence = Literal["available", "away", "offline"]
# Public-facing status seen by other admins / the call-routing logic. Adds a
# fourth value, "busy", which is auto-set by the call status webhooks when an
# agent's <Client> leg goes ``in-progress`` and cleared when it terminates.
AgentStatus = Literal["available", "busy", "away", "offline"]
AgentRole = Literal["admin", "agent"]


@dataclass
class Agent:
    """Person who can answer/place calls from a softphone (web or mobile)."""

    identity: str  # Twilio Voice SDK identity (unique, no spaces)
    name: str
    role: AgentRole = "agent"
    # Owning tenant (workspace). Identities are unique *within* a tenant.
    tenant_id: str = DEFAULT_TENANT_ID
    # Manual presence — what the human picked from the status menu.
    presence: AgentPresence = "offline"
    # Currently bridged parent CallSid (auto-populated by the leg-status
    # webhook). When non-None, the agent is considered "busy" by routing
    # regardless of presence.
    current_call_sid: Optional[str] = None
    email: Optional[str] = None
    last_seen: float = field(default_factory=time.time)

    @property
    def status(self) -> AgentStatus:
        """Derived status used by the UI and the routing layer.

        offline    presence == 'offline'
        away       presence == 'away'
        busy       presence == 'available' AND on a call
        available  presence == 'available' AND idle
        """
        if self.presence == "offline":
            return "offline"
        if self.presence == "away":
            return "away"
        if self.current_call_sid:
            return "busy"
        return "available"

    @property
    def is_eligible_for_routing(self) -> bool:
        """True when the agent's softphone can be rung for a new inbound call."""
        return self.presence == "available" and not self.current_call_sid

    def to_dict(self) -> Dict:
        return {
            # Frontend's AdminAgent shape uses ``id``; expose ``identity`` too
            # so older callers keep working.
            "id": self.identity,
            "identity": self.identity,
            "name": self.name,
            "role": self.role,
            "status": self.status,           # derived
            "presence": self.presence,       # raw manual choice
            "current_call_sid": self.current_call_sid,
            "current_call_uuid": self.current_call_sid,
            "email": self.email,
            "tenant_id": self.tenant_id,
            "last_seen": self.last_seen,
            "last_seen_at": self.last_seen,
        }


class AgentStore:
    """Thread-safe, **tenant-partitioned** agent registry.

    Agents are keyed by ``(tenant_id, identity)`` — an identity (e.g. the
    seeded ``admin``) is only unique *within* a tenant. Stable roster
    fields are persisted to the ``agents`` table when a DB is configured so
    teammates survive a restart; volatile state (``current_call_sid``,
    ``last_seen``) is kept in memory only.
    """

    def __init__(self) -> None:
        # tenant_id -> identity -> Agent
        self._agents: Dict[str, Dict[str, Agent]] = {}
        self._loaded: set[str] = set()
        self._lock = threading.RLock()
        # Seed the bootstrap tenant's default admin in memory only (no DB
        # touch at import time — the schema may not exist yet).
        self._agents[DEFAULT_TENANT_ID] = {
            "admin": Agent(
                identity="admin", name="Support Admin", role="admin",
                tenant_id=DEFAULT_TENANT_ID,
            )
        }

    # ------------------------- persistence ----------------------- #
    @staticmethod
    def _enabled() -> bool:
        from .db import engine_or_none
        return engine_or_none() is not None

    @staticmethod
    def _surrogate(tenant_id: str, identity: str) -> str:
        return f"{tenant_id}:{identity}"

    def _persist(self, agent: Agent) -> None:
        """Write-through the agent's durable roster fields."""
        if not self._enabled():
            return
        try:
            from .db import AgentORM, session_scope
            with session_scope() as s:
                sid = self._surrogate(agent.tenant_id, agent.identity)
                row = s.get(AgentORM, sid)
                now = time.time()
                if row is None:
                    s.add(AgentORM(
                        id=sid, tenant_id=agent.tenant_id, identity=agent.identity,
                        name=agent.name, role=agent.role, presence=agent.presence,
                        email=agent.email, created_at=now, updated_at=now,
                    ))
                else:
                    row.name = agent.name
                    row.role = agent.role
                    row.presence = agent.presence
                    row.email = agent.email
                    row.updated_at = now
        except Exception:  # noqa: BLE001
            logger.exception("Failed to persist agent %s/%s", agent.tenant_id, agent.identity)

    def _delete_persisted(self, tenant_id: str, identity: str) -> None:
        if not self._enabled():
            return
        try:
            from .db import AgentORM, session_scope
            with session_scope() as s:
                row = s.get(AgentORM, self._surrogate(tenant_id, identity))
                if row is not None:
                    s.delete(row)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to delete agent %s/%s", tenant_id, identity)

    def _ensure_loaded(self, tenant_id: str) -> None:
        """Hydrate a tenant's roster from the DB on first access (idempotent)."""
        if tenant_id in self._loaded:
            return
        if not self._enabled():
            self._loaded.add(tenant_id)
            return
        try:
            from sqlalchemy import select
            from .db import AgentORM, session_scope
            with session_scope() as s:
                rows = s.execute(
                    select(AgentORM).where(AgentORM.tenant_id == tenant_id)
                ).scalars().all()
            bucket = self._agents.setdefault(tenant_id, {})
            for r in rows:
                # Never clobber a live in-memory agent (it carries volatile state).
                if r.identity not in bucket:
                    bucket[r.identity] = Agent(
                        identity=r.identity, name=r.name, role=r.role,  # type: ignore[arg-type]
                        tenant_id=tenant_id, presence=r.presence, email=r.email,  # type: ignore[arg-type]
                    )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to load agents for tenant %s", tenant_id)
        finally:
            self._loaded.add(tenant_id)

    def _bucket(self, tenant_id: str) -> Dict[str, Agent]:
        self._ensure_loaded(tenant_id)
        return self._agents.setdefault(tenant_id, {})

    def ensure_seed_admin(self, tenant_id: str) -> None:
        """Guarantee a tenant has its default ``admin`` softphone identity."""
        with self._lock:
            bucket = self._bucket(tenant_id)
            if "admin" not in bucket:
                self.upsert(Agent(
                    identity="admin", name="Support Admin", role="admin",
                    tenant_id=tenant_id,
                ))

    # ------------------------- CRUD ------------------------------ #
    def upsert(self, agent: Agent) -> Agent:
        with self._lock:
            self._bucket(agent.tenant_id)[agent.identity] = agent
        self._persist(agent)
        return agent

    def get(self, identity: str, tenant_id: str = DEFAULT_TENANT_ID) -> Optional[Agent]:
        with self._lock:
            return self._bucket(tenant_id).get(identity)

    def list_all(self, tenant_id: str = DEFAULT_TENANT_ID) -> List[Agent]:
        with self._lock:
            return list(self._bucket(tenant_id).values())

    def list_available(self, tenant_id: str = DEFAULT_TENANT_ID) -> List[Agent]:
        """Agents in ``tenant_id`` currently eligible to receive an inbound call.

        An agent is eligible iff their manual presence is ``available`` AND
        they are not already bridged on a call. Other presences (``away``,
        ``offline``) and busy agents are skipped — the routing layer never
        rings them, which is what makes the call center auto-route to the
        next free teammate.
        """
        with self._lock:
            return [a for a in self._bucket(tenant_id).values() if a.is_eligible_for_routing]

    def list_eligible_for_routing(self, *, sort_by_idle: bool = True,
                                  tenant_id: str = DEFAULT_TENANT_ID) -> List[Agent]:
        """Same as :meth:`list_available` but returns longest-idle first."""
        items = self.list_available(tenant_id)
        if sort_by_idle:
            items.sort(key=lambda a: a.last_seen)
        return items

    def has_any_registered_agent(self, tenant_id: str = DEFAULT_TENANT_ID) -> bool:
        """True when at least one agent in the tenant is online (presence != offline)."""
        with self._lock:
            return any(a.presence != "offline" for a in self._bucket(tenant_id).values())

    def find_by_call(self, call_sid: str, tenant_id: Optional[str] = None) -> Optional[Agent]:
        """Find the agent bridged on ``call_sid``.

        When ``tenant_id`` is known (recommended) we scope to that tenant;
        otherwise we scan every loaded tenant (CallSids are globally unique).
        """
        with self._lock:
            if tenant_id is not None:
                for a in self._bucket(tenant_id).values():
                    if a.current_call_sid == call_sid:
                        return a
                return None
            for bucket in self._agents.values():
                for a in bucket.values():
                    if a.current_call_sid == call_sid:
                        return a
        return None

    def set_presence(self, identity: str, presence: AgentPresence,
                     tenant_id: str = DEFAULT_TENANT_ID) -> Optional[Agent]:
        """Update the agent's manual presence (Available / Break / Offline)."""
        with self._lock:
            a = self._bucket(tenant_id).get(identity)
            if a is None:
                return None
            a.presence = presence
            a.last_seen = time.time()
        self._persist(a)
        return a

    def set_busy(self, identity: str, call_sid: str,
                 tenant_id: str = DEFAULT_TENANT_ID) -> Optional[Agent]:
        """Mark agent as bridged on ``call_sid`` (auto from leg-status webhook)."""
        with self._lock:
            a = self._bucket(tenant_id).get(identity)
            if a is None:
                return None
            a.current_call_sid = call_sid
            a.last_seen = time.time()
            return a

    def set_idle(self, identity: str, *, only_if_call_sid: Optional[str] = None,
                 tenant_id: str = DEFAULT_TENANT_ID) -> Optional[Agent]:
        """Clear the agent's current_call_sid so they're routable again.

        Pass ``only_if_call_sid`` to make the update idempotent when multiple
        leg-status callbacks fire — only clears when the recorded call matches.
        """
        with self._lock:
            a = self._bucket(tenant_id).get(identity)
            if a is None:
                return None
            if only_if_call_sid and a.current_call_sid != only_if_call_sid:
                return a
            a.current_call_sid = None
            a.last_seen = time.time()
            return a

    def set_status(self, identity: str, status: AgentStatus,
                   tenant_id: str = DEFAULT_TENANT_ID) -> Optional[Agent]:
        """Backwards-compatible setter — maps the old 4-value status onto
        the new presence + current_call_sid model.
        """
        if status == "busy":
            # Honoured by the call-status webhook only — manual heartbeat
            # cannot force-busy without a call_sid. Treat as a no-op aside
            # from bumping last_seen so the heartbeat still works.
            with self._lock:
                a = self._bucket(tenant_id).get(identity)
                if a is None:
                    return None
                a.last_seen = time.time()
                return a
        return self.set_presence(identity, status, tenant_id)  # type: ignore[arg-type]

    def remove(self, identity: str, tenant_id: str = DEFAULT_TENANT_ID) -> bool:
        with self._lock:
            removed = self._bucket(tenant_id).pop(identity, None) is not None
        if removed:
            self._delete_persisted(tenant_id, identity)
        return removed


# --------------------------------------------------------------------------- #
#  Inbound queue (call-center pipeline)
# --------------------------------------------------------------------------- #

@dataclass
class QueuedCall:
    """A customer call waiting for an agent to free up."""

    call_sid: str
    from_number: str
    to_number: str
    enqueued_at: float = field(default_factory=time.time)
    # Owning tenant (workspace) this queued call belongs to.
    tenant_id: str = DEFAULT_TENANT_ID
    # IVR selection — propagated from the parent Call so the operator UI can
    # see what the queued caller asked for.
    service_code: Optional[str] = None
    service_label: Optional[str] = None

    @property
    def wait_seconds(self) -> int:
        return max(0, int(time.time() - self.enqueued_at))

    def to_dict(self) -> Dict:
        return {
            "call_sid": self.call_sid,
            "call_uuid": self.call_sid,
            "from": self.from_number,
            "from_number": self.from_number,
            "to": self.to_number,
            "to_number": self.to_number,
            "enqueued_at": self.enqueued_at,
            "wait_seconds": self.wait_seconds,
            "service_code": self.service_code,
            "service_label": self.service_label,
            "tenant_id": self.tenant_id,
        }


class CallQueue:
    """Thread-safe FIFO queue of customer calls waiting for an agent.

    The queue holds bare metadata; the full :class:`Call` lives in
    ``call_store``. A queued call's TwiML keeps polling
    ``/twilio/voice/queue/wait/<call_sid>`` so it gets dispatched the
    moment an agent frees up. We also actively redirect from the
    leg-status callback so the latency is sub-second on agent free-up.
    """

    def __init__(self) -> None:
        # tenant_id -> FIFO list of queued calls
        self._items: Dict[str, List[QueuedCall]] = {}
        self._lock = threading.RLock()

    def _bucket(self, tenant_id: str) -> List[QueuedCall]:
        return self._items.setdefault(tenant_id, [])

    def enqueue(self, call: QueuedCall) -> int:
        """Add a call to the back of its tenant's queue. Returns 1-based position."""
        with self._lock:
            bucket = self._bucket(call.tenant_id)
            # Idempotent — if the same call is re-enqueued (e.g. the
            # /queue/wait poll re-runs while no agent is free), keep the
            # original ``enqueued_at`` to preserve fair ordering.
            for existing in bucket:
                if existing.call_sid == call.call_sid:
                    return bucket.index(existing) + 1
            bucket.append(call)
            return len(bucket)

    def remove(self, call_sid: str, tenant_id: Optional[str] = None) -> Optional[QueuedCall]:
        with self._lock:
            buckets = [self._bucket(tenant_id)] if tenant_id is not None else list(self._items.values())
            for bucket in buckets:
                for i, c in enumerate(bucket):
                    if c.call_sid == call_sid:
                        return bucket.pop(i)
        return None

    def pop_next(self, tenant_id: str = DEFAULT_TENANT_ID) -> Optional[QueuedCall]:
        with self._lock:
            bucket = self._bucket(tenant_id)
            if not bucket:
                return None
            return bucket.pop(0)

    def peek(self, call_sid: str, tenant_id: Optional[str] = None) -> Optional[QueuedCall]:
        with self._lock:
            buckets = [self._bucket(tenant_id)] if tenant_id is not None else list(self._items.values())
            for bucket in buckets:
                hit = next((c for c in bucket if c.call_sid == call_sid), None)
                if hit is not None:
                    return hit
        return None

    def position(self, call_sid: str, tenant_id: Optional[str] = None) -> int:
        """1-based position within the tenant's queue; 0 if not in queue."""
        with self._lock:
            buckets = [self._bucket(tenant_id)] if tenant_id is not None else list(self._items.values())
            for bucket in buckets:
                for i, c in enumerate(bucket):
                    if c.call_sid == call_sid:
                        return i + 1
        return 0

    def list_calls(self, tenant_id: Optional[str] = None) -> List[QueuedCall]:
        with self._lock:
            if tenant_id is not None:
                return list(self._bucket(tenant_id))
            out: List[QueuedCall] = []
            for bucket in self._items.values():
                out.extend(bucket)
            return out

    def depth(self, tenant_id: str = DEFAULT_TENANT_ID) -> int:
        with self._lock:
            return len(self._bucket(tenant_id))

    def __len__(self) -> int:
        with self._lock:
            return sum(len(b) for b in self._items.values())


# --------------------------------------------------------------------------- #
#  Singletons
# --------------------------------------------------------------------------- #

call_store = CallStore()
agent_store = AgentStore()
call_queue = CallQueue()


def new_internal_id(prefix: str = "loc") -> str:
    """Generate a backend-only id used before Twilio assigns a CallSid."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# --------------------------------------------------------------------------- #
#  User model (auth + team)
# --------------------------------------------------------------------------- #

UserRole = Literal["admin", "member"]
UserStatus = Literal["active", "inactive"]


@dataclass
class User:
    """Application user (admin or team member)."""

    id: str
    email: str
    password_hash: str
    full_name: str
    tenant_id: str = DEFAULT_TENANT_ID
    role: UserRole = "member"
    status: UserStatus = "active"
    phone: Optional[str] = None
    organization: Optional[str] = None
    avatar_initials: Optional[str] = None
    invited_by: Optional[str] = None  # user id of inviter
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_seen_at: Optional[float] = None
    # Email verification + OTP state
    email_verified: bool = False
    otp_code_hash: Optional[str] = None
    otp_expires_at: Optional[float] = None
    otp_last_sent_at: Optional[float] = None
    otp_attempts: int = 0

    def to_public(self) -> Dict:
        """Shape used by the frontend (mirrors SkyUser)."""
        return {
            "id": self.id,
            "email": self.email,
            "fullName": self.full_name,
            "phone": self.phone,
            "organization": self.organization,
            "avatarInitials": self.avatar_initials or _derive_initials(self.full_name),
            "role": self.role,
            "status": self.status,
            "tenantId": self.tenant_id,
            "emailVerified": self.email_verified,
            "createdAt": _iso(self.created_at),
            "updatedAt": _iso(self.updated_at),
            "invitedBy": self.invited_by,
        }


class UserStore:
    """Thread-safe user repository.

    Uses SQLAlchemy when ``DATABASE_URL`` is set (so accounts survive
    restarts) and falls back to an in-memory dict for local-only dev.
    The API surface is identical in both modes so the routes don't care.

    Internally each method opens its own short-lived session via
    :func:`app.db.session_scope`, executes the work, and lets the context
    manager commit or rollback. Sessions are never held across request
    boundaries.
    """

    def __init__(self) -> None:
        # In-memory fallback caches — only used when no DB is configured.
        self._by_id: Dict[str, User] = {}
        self._by_email: Dict[str, str] = {}  # email_lower → id
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    #  Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _enabled() -> bool:
        # Imported lazily to avoid a circular import with config at boot.
        from .db import engine_or_none

        return engine_or_none() is not None

    @staticmethod
    def _orm_to_user(row) -> "User":  # type: ignore[no-untyped-def]
        return User(
            id=row.id,
            tenant_id=getattr(row, "tenant_id", None) or DEFAULT_TENANT_ID,
            email=row.email,
            password_hash=row.password_hash,
            full_name=row.full_name,
            role=row.role,
            status=row.status,
            phone=row.phone,
            organization=row.organization,
            avatar_initials=row.avatar_initials,
            invited_by=row.invited_by,
            created_at=float(row.created_at),
            updated_at=float(row.updated_at),
            last_seen_at=float(row.last_seen_at) if row.last_seen_at is not None else None,
            email_verified=bool(getattr(row, "email_verified", False)),
            otp_code_hash=getattr(row, "otp_code_hash", None),
            otp_expires_at=(
                float(row.otp_expires_at) if getattr(row, "otp_expires_at", None) is not None else None
            ),
            otp_last_sent_at=(
                float(row.otp_last_sent_at) if getattr(row, "otp_last_sent_at", None) is not None else None
            ),
            otp_attempts=int(getattr(row, "otp_attempts", 0) or 0),
        )

    # ------------------------------------------------------------------ #
    #  CRUD
    # ------------------------------------------------------------------ #
    def create(self, user: User) -> User:
        if not self._enabled():
            with self._lock:
                if user.email.lower() in self._by_email:
                    raise ValueError("email_already_registered")
                self._by_id[user.id] = user
                self._by_email[user.email.lower()] = user.id
            return user

        from sqlalchemy.exc import IntegrityError
        from .db import UserORM, session_scope

        row = UserORM(
            id=user.id,
            tenant_id=user.tenant_id,
            email=user.email,
            email_lower=user.email.lower(),
            password_hash=user.password_hash,
            full_name=user.full_name,
            role=user.role,
            status=user.status,
            phone=user.phone,
            organization=user.organization,
            avatar_initials=user.avatar_initials,
            invited_by=user.invited_by,
            created_at=user.created_at,
            updated_at=user.updated_at,
            last_seen_at=user.last_seen_at,
            email_verified=user.email_verified,
            otp_code_hash=user.otp_code_hash,
            otp_expires_at=user.otp_expires_at,
            otp_last_sent_at=user.otp_last_sent_at,
            otp_attempts=user.otp_attempts,
        )
        try:
            with session_scope() as s:
                s.add(row)
        except IntegrityError as exc:
            # email_lower has UNIQUE — the only realistic violation.
            raise ValueError("email_already_registered") from exc
        return user

    def get(self, user_id: str) -> Optional[User]:
        if not self._enabled():
            with self._lock:
                return self._by_id.get(user_id)
        from .db import UserORM, session_scope
        with session_scope() as s:
            row = s.get(UserORM, user_id)
            return self._orm_to_user(row) if row else None

    def get_by_email(self, email: str) -> Optional[User]:
        if not self._enabled():
            with self._lock:
                uid = self._by_email.get(email.lower())
                return self._by_id.get(uid) if uid else None
        from sqlalchemy import select
        from .db import UserORM, session_scope
        with session_scope() as s:
            row = s.execute(
                select(UserORM).where(UserORM.email_lower == email.lower())
            ).scalar_one_or_none()
            return self._orm_to_user(row) if row else None

    def list_all(self, tenant_id: Optional[str] = None) -> List[User]:
        if not self._enabled():
            with self._lock:
                items = list(self._by_id.values())
                if tenant_id is not None:
                    items = [u for u in items if u.tenant_id == tenant_id]
                return items
        from sqlalchemy import select
        from .db import UserORM, session_scope
        with session_scope() as s:
            stmt = select(UserORM).order_by(UserORM.created_at.asc())
            if tenant_id is not None:
                stmt = stmt.where(UserORM.tenant_id == tenant_id)
            rows = s.execute(stmt).scalars().all()
            return [self._orm_to_user(r) for r in rows]

    def update(self, user_id: str, **fields) -> Optional[User]:
        if not self._enabled():
            with self._lock:
                user = self._by_id.get(user_id)
                if not user:
                    return None
                old_email = user.email.lower()
                for k, v in fields.items():
                    if hasattr(user, k):
                        setattr(user, k, v)
                user.updated_at = time.time()
                if user.email.lower() != old_email:
                    self._by_email.pop(old_email, None)
                    self._by_email[user.email.lower()] = user.id
                return user

        # Whitelist mutable columns so callers can't poke at id/created_at.
        mutable = {
            "email",
            "password_hash",
            "full_name",
            "role",
            "status",
            "phone",
            "organization",
            "avatar_initials",
            "invited_by",
            "last_seen_at",
            "email_verified",
            "otp_code_hash",
            "otp_expires_at",
            "otp_last_sent_at",
            "otp_attempts",
        }
        from .db import UserORM, session_scope
        with session_scope() as s:
            row = s.get(UserORM, user_id)
            if row is None:
                return None
            for k, v in fields.items():
                if k not in mutable:
                    continue
                setattr(row, k, v)
                if k == "email":
                    row.email_lower = str(v).lower()
            row.updated_at = time.time()
            s.flush()
            return self._orm_to_user(row)

    def delete(self, user_id: str) -> bool:
        if not self._enabled():
            with self._lock:
                user = self._by_id.pop(user_id, None)
                if user:
                    self._by_email.pop(user.email.lower(), None)
                    return True
                return False
        from .db import UserORM, session_scope
        with session_scope() as s:
            row = s.get(UserORM, user_id)
            if row is None:
                return False
            s.delete(row)
            return True

    def count(self, tenant_id: Optional[str] = None) -> int:
        if not self._enabled():
            with self._lock:
                if tenant_id is None:
                    return len(self._by_id)
                return sum(1 for u in self._by_id.values() if u.tenant_id == tenant_id)
        from sqlalchemy import func, select
        from .db import UserORM, session_scope
        with session_scope() as s:
            stmt = select(func.count(UserORM.id))
            if tenant_id is not None:
                stmt = stmt.where(UserORM.tenant_id == tenant_id)
            return int(s.execute(stmt).scalar_one())


user_store = UserStore()


def _derive_initials(name: str) -> str:
    parts = [p for p in (name or "").split() if p]
    if not parts:
        return "U"
    return (parts[0][0] + (parts[-1][0] if len(parts) > 1 else "")).upper()


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
#  Tenant (workspace / organisation) model
# --------------------------------------------------------------------------- #

TenantStatus = Literal["active", "suspended"]


@dataclass
class Tenant:
    """A workspace that owns its own users, SIMs, agents, calls, Twilio
    credentials and Sara (AI receptionist) configuration.

    Every credential/config field is optional; a blank value means "inherit
    the process-wide ``.env`` default", which is what lets the bootstrap
    ``default`` tenant keep an existing single-tenant deployment working.
    """

    id: str
    name: str
    slug: str
    status: TenantStatus = "active"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    # --- Twilio credentials (blank → fall back to .env) ---
    twilio_account_sid: Optional[str] = None
    twilio_auth_token: Optional[str] = None
    twilio_api_key_sid: Optional[str] = None
    twilio_api_key_secret: Optional[str] = None
    twilio_twiml_app_sid: Optional[str] = None
    twilio_caller_id: Optional[str] = None
    public_base_url: Optional[str] = None

    # --- Sara config (blank → fall back to .env) ---
    sara_company_name: Optional[str] = None
    sara_summary_to: Optional[str] = None
    sara_knowledge_pdf: Optional[str] = None
    # Extracted text of an uploaded knowledge book (DB-backed so it survives
    # restarts / works across Cloud Run instances). Blank → use the PDF path.
    sara_knowledge_text: Optional[str] = None
    sara_tts_voice: Optional[str] = None
    sara_stt_model: Optional[str] = None
    sara_deepgram_api_key: Optional[str] = None
    sara_ai_api_key: Optional[str] = None
    sara_ai_model: Optional[str] = None
    sara_ai_base_url: Optional[str] = None
    sara_answer_first: Optional[bool] = None
    sara_auto_fallback: Optional[bool] = None

    def to_public(self) -> Dict:
        """Admin-UI shape. Secrets are reported as booleans, never echoed."""
        return {
            "id": self.id,
            "name": self.name,
            "slug": self.slug,
            "status": self.status,
            "createdAt": _iso(self.created_at),
            "updatedAt": _iso(self.updated_at),
            "twilio": {
                "accountSid": self.twilio_account_sid,
                "apiKeySid": self.twilio_api_key_sid,
                "twimlAppSid": self.twilio_twiml_app_sid,
                "callerId": self.twilio_caller_id,
                "publicBaseUrl": self.public_base_url,
                "authTokenSet": bool(self.twilio_auth_token),
                "apiKeySecretSet": bool(self.twilio_api_key_secret),
            },
            "sara": {
                "companyName": self.sara_company_name,
                "summaryTo": self.sara_summary_to,
                "knowledgePdf": self.sara_knowledge_pdf,
                "knowledgeBookSet": bool(self.sara_knowledge_text),
                "ttsVoice": self.sara_tts_voice,
                "sttModel": self.sara_stt_model,
                "aiModel": self.sara_ai_model,
                "aiBaseUrl": self.sara_ai_base_url,
                "answerFirst": self.sara_answer_first,
                "autoFallback": self.sara_auto_fallback,
                "deepgramKeySet": bool(self.sara_deepgram_api_key),
                "aiKeySet": bool(self.sara_ai_api_key),
            },
        }


class TenantStore:
    """Tenant repository (DB + in-memory fallback), mirroring :class:`UserStore`."""

    # Columns a caller may mutate via :meth:`update`.
    _CONFIG_FIELDS = (
        "name", "slug", "status",
        "twilio_account_sid", "twilio_auth_token", "twilio_api_key_sid",
        "twilio_api_key_secret", "twilio_twiml_app_sid", "twilio_caller_id",
        "public_base_url",
        "sara_company_name", "sara_summary_to", "sara_knowledge_pdf",
        "sara_knowledge_text",
        "sara_tts_voice", "sara_stt_model", "sara_deepgram_api_key",
        "sara_ai_api_key", "sara_ai_model", "sara_ai_base_url",
        "sara_answer_first", "sara_auto_fallback",
    )

    def __init__(self) -> None:
        self._by_id: Dict[str, Tenant] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _enabled() -> bool:
        from .db import engine_or_none
        return engine_or_none() is not None

    @staticmethod
    def _orm_to_tenant(row) -> "Tenant":  # type: ignore[no-untyped-def]
        return Tenant(
            id=row.id, name=row.name, slug=row.slug, status=row.status,
            created_at=float(row.created_at), updated_at=float(row.updated_at),
            twilio_account_sid=row.twilio_account_sid,
            twilio_auth_token=row.twilio_auth_token,
            twilio_api_key_sid=row.twilio_api_key_sid,
            twilio_api_key_secret=row.twilio_api_key_secret,
            twilio_twiml_app_sid=row.twilio_twiml_app_sid,
            twilio_caller_id=row.twilio_caller_id,
            public_base_url=row.public_base_url,
            sara_company_name=row.sara_company_name,
            sara_summary_to=row.sara_summary_to,
            sara_knowledge_pdf=row.sara_knowledge_pdf,
            sara_knowledge_text=row.sara_knowledge_text,
            sara_tts_voice=row.sara_tts_voice,
            sara_stt_model=row.sara_stt_model,
            sara_deepgram_api_key=row.sara_deepgram_api_key,
            sara_ai_api_key=row.sara_ai_api_key,
            sara_ai_model=row.sara_ai_model,
            sara_ai_base_url=row.sara_ai_base_url,
            sara_answer_first=row.sara_answer_first,
            sara_auto_fallback=row.sara_auto_fallback,
        )

    def create(self, tenant: Tenant) -> Tenant:
        if not self._enabled():
            with self._lock:
                self._by_id[tenant.id] = tenant
            return tenant
        from sqlalchemy.exc import IntegrityError
        from .db import TenantORM, session_scope
        row = TenantORM(
            id=tenant.id, name=tenant.name, slug=tenant.slug, status=tenant.status,
            created_at=tenant.created_at, updated_at=tenant.updated_at,
            twilio_account_sid=tenant.twilio_account_sid,
            twilio_auth_token=tenant.twilio_auth_token,
            twilio_api_key_sid=tenant.twilio_api_key_sid,
            twilio_api_key_secret=tenant.twilio_api_key_secret,
            twilio_twiml_app_sid=tenant.twilio_twiml_app_sid,
            twilio_caller_id=tenant.twilio_caller_id,
            public_base_url=tenant.public_base_url,
            sara_company_name=tenant.sara_company_name,
            sara_summary_to=tenant.sara_summary_to,
            sara_knowledge_pdf=tenant.sara_knowledge_pdf,
            sara_knowledge_text=tenant.sara_knowledge_text,
            sara_tts_voice=tenant.sara_tts_voice,
            sara_stt_model=tenant.sara_stt_model,
            sara_deepgram_api_key=tenant.sara_deepgram_api_key,
            sara_ai_api_key=tenant.sara_ai_api_key,
            sara_ai_model=tenant.sara_ai_model,
            sara_ai_base_url=tenant.sara_ai_base_url,
            sara_answer_first=tenant.sara_answer_first,
            sara_auto_fallback=tenant.sara_auto_fallback,
        )
        try:
            with session_scope() as s:
                s.add(row)
        except IntegrityError as exc:
            raise ValueError("tenant_slug_taken") from exc
        return tenant

    def get(self, tenant_id: str) -> Optional[Tenant]:
        if not self._enabled():
            with self._lock:
                return self._by_id.get(tenant_id)
        from .db import TenantORM, session_scope
        with session_scope() as s:
            row = s.get(TenantORM, tenant_id)
            return self._orm_to_tenant(row) if row else None

    def get_by_slug(self, slug: str) -> Optional[Tenant]:
        if not self._enabled():
            with self._lock:
                return next((t for t in self._by_id.values() if t.slug == slug), None)
        from sqlalchemy import select
        from .db import TenantORM, session_scope
        with session_scope() as s:
            row = s.execute(
                select(TenantORM).where(TenantORM.slug == slug)
            ).scalar_one_or_none()
            return self._orm_to_tenant(row) if row else None

    def list_all(self) -> List[Tenant]:
        if not self._enabled():
            with self._lock:
                return list(self._by_id.values())
        from sqlalchemy import select
        from .db import TenantORM, session_scope
        with session_scope() as s:
            rows = s.execute(
                select(TenantORM).order_by(TenantORM.created_at.asc())
            ).scalars().all()
            return [self._orm_to_tenant(r) for r in rows]

    def update(self, tenant_id: str, **fields) -> Optional[Tenant]:
        if not self._enabled():
            with self._lock:
                t = self._by_id.get(tenant_id)
                if not t:
                    return None
                for k, v in fields.items():
                    if k in self._CONFIG_FIELDS:
                        setattr(t, k, v)
                t.updated_at = time.time()
                return t
        from .db import TenantORM, session_scope
        with session_scope() as s:
            row = s.get(TenantORM, tenant_id)
            if row is None:
                return None
            for k, v in fields.items():
                if k in self._CONFIG_FIELDS:
                    setattr(row, k, v)
            row.updated_at = time.time()
            s.flush()
            return self._orm_to_tenant(row)

    def count(self) -> int:
        if not self._enabled():
            with self._lock:
                return len(self._by_id)
        from sqlalchemy import func, select
        from .db import TenantORM, session_scope
        with session_scope() as s:
            return int(s.execute(select(func.count(TenantORM.id))).scalar_one())


tenant_store = TenantStore()


def new_tenant_id() -> str:
    """Generate a unique tenant identifier."""
    return f"tnt_{uuid.uuid4().hex[:14]}"


# --------------------------------------------------------------------------- #
#  SIM / outbound caller-ID model
# --------------------------------------------------------------------------- #

SimSource = Literal["twilio", "manual", "verified"]


@dataclass
class Sim:
    """A Twilio phone number the workspace can place outbound calls from.

    Conceptually a "SIM card" in the UI — but for cloud telephony, it's just
    a verified outbound caller ID (your Twilio number, or another number
    you've already verified in the Twilio console).
    """

    id: str
    phone_number: str           # E.164 — e.g. "+16097398989"
    label: str                  # human-friendly name
    tenant_id: str = DEFAULT_TENANT_ID
    is_default: bool = False
    source: SimSource = "manual"
    twilio_sid: Optional[str] = None  # IncomingPhoneNumber SID when auto-imported
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "phoneNumber": self.phone_number,
            "label": self.label,
            "isDefault": self.is_default,
            "source": self.source,
            "twilioSid": self.twilio_sid,
            "tenantId": self.tenant_id,
            "registeredAt": _iso(self.created_at),
        }


class SimStore:
    """Thread-safe SIM repository (DB + in-memory fallback).

    Mirrors the UserStore pattern: uses SQLAlchemy + ``sims`` table when
    DATABASE_URL is set, otherwise an in-memory dict for local dev.
    """

    def __init__(self) -> None:
        self._by_id: Dict[str, Sim] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    @staticmethod
    def _enabled() -> bool:
        from .db import engine_or_none
        return engine_or_none() is not None

    @staticmethod
    def _orm_to_sim(row) -> "Sim":  # type: ignore[no-untyped-def]
        return Sim(
            id=row.id,
            phone_number=row.phone_number,
            label=row.label,
            tenant_id=getattr(row, "tenant_id", None) or DEFAULT_TENANT_ID,
            is_default=bool(row.is_default),
            source=row.source,  # type: ignore[arg-type]
            twilio_sid=row.twilio_sid,
            created_at=float(row.created_at),
            updated_at=float(row.updated_at),
        )

    # ------------------------------------------------------------------ #
    def list_all(self, tenant_id: Optional[str] = None) -> List[Sim]:
        if not self._enabled():
            with self._lock:
                items = list(self._by_id.values())
        else:
            from sqlalchemy import select
            from .db import SimORM, session_scope
            with session_scope() as s:
                stmt = select(SimORM).order_by(SimORM.created_at.asc())
                if tenant_id is not None:
                    stmt = stmt.where(SimORM.tenant_id == tenant_id)
                rows = s.execute(stmt).scalars().all()
                items = [self._orm_to_sim(r) for r in rows]
        if tenant_id is not None:
            items = [x for x in items if x.tenant_id == tenant_id]
        # Default SIM first, then alphabetical by label.
        items.sort(key=lambda x: (not x.is_default, x.label.lower()))
        return items

    def get(self, sim_id: str) -> Optional[Sim]:
        if not self._enabled():
            with self._lock:
                return self._by_id.get(sim_id)
        from .db import SimORM, session_scope
        with session_scope() as s:
            row = s.get(SimORM, sim_id)
            return self._orm_to_sim(row) if row else None

    def get_by_number(self, phone_number: str) -> Optional[Sim]:
        if not self._enabled():
            with self._lock:
                return next(
                    (s for s in self._by_id.values() if s.phone_number == phone_number),
                    None,
                )
        from sqlalchemy import select
        from .db import SimORM, session_scope
        with session_scope() as s:
            row = s.execute(
                select(SimORM).where(SimORM.phone_number == phone_number)
            ).scalar_one_or_none()
            return self._orm_to_sim(row) if row else None

    def get_default(self, tenant_id: str = DEFAULT_TENANT_ID) -> Optional[Sim]:
        if not self._enabled():
            with self._lock:
                mine = [s for s in self._by_id.values() if s.tenant_id == tenant_id]
                default = next((s for s in mine if s.is_default), None)
                if default:
                    return default
                # No explicit default — fall back to the oldest entry.
                mine.sort(key=lambda s: s.created_at)
                return mine[0] if mine else None
        from sqlalchemy import select
        from .db import SimORM, session_scope
        with session_scope() as s:
            row = s.execute(
                select(SimORM)
                .where(SimORM.tenant_id == tenant_id, SimORM.is_default.is_(True))
                .limit(1)
            ).scalar_one_or_none()
            if row:
                return self._orm_to_sim(row)
            row = s.execute(
                select(SimORM)
                .where(SimORM.tenant_id == tenant_id)
                .order_by(SimORM.created_at.asc())
                .limit(1)
            ).scalar_one_or_none()
            return self._orm_to_sim(row) if row else None

    # ------------------------------------------------------------------ #
    def create(self, sim: Sim) -> Sim:
        """Insert a new SIM. Raises ``ValueError("duplicate_phone_number")``
        if the number is already in the store."""
        if not self._enabled():
            with self._lock:
                if any(s.phone_number == sim.phone_number for s in self._by_id.values()):
                    raise ValueError("duplicate_phone_number")
                mine = [s for s in self._by_id.values() if s.tenant_id == sim.tenant_id]
                if sim.is_default:
                    for s in mine:
                        s.is_default = False
                if not any(s.is_default for s in mine) and not sim.is_default:
                    sim.is_default = True  # first SIM in this tenant → default
                self._by_id[sim.id] = sim
            return sim
        from sqlalchemy.exc import IntegrityError
        from sqlalchemy import update as sa_update
        from .db import SimORM, session_scope
        try:
            with session_scope() as s:
                # Clear other defaults *within this tenant* if marking default.
                if sim.is_default:
                    s.execute(
                        sa_update(SimORM)
                        .where(SimORM.tenant_id == sim.tenant_id)
                        .values(is_default=False)
                    )
                # Auto-default if this is the very first row for the tenant.
                existing_count = (
                    s.query(SimORM).filter(SimORM.tenant_id == sim.tenant_id).count()
                )
                if existing_count == 0:
                    sim.is_default = True
                s.add(SimORM(
                    id=sim.id,
                    tenant_id=sim.tenant_id,
                    phone_number=sim.phone_number,
                    label=sim.label,
                    is_default=sim.is_default,
                    source=sim.source,
                    twilio_sid=sim.twilio_sid,
                    created_at=sim.created_at,
                    updated_at=sim.updated_at,
                ))
        except IntegrityError as exc:
            raise ValueError("duplicate_phone_number") from exc
        return sim

    def update(self, sim_id: str, **fields) -> Optional[Sim]:
        mutable = {"label", "is_default", "source", "twilio_sid", "phone_number"}
        if not self._enabled():
            with self._lock:
                sim = self._by_id.get(sim_id)
                if not sim:
                    return None
                make_default = bool(fields.get("is_default"))
                for k, v in fields.items():
                    if k in mutable:
                        setattr(sim, k, v)
                sim.updated_at = time.time()
                if make_default:
                    for s in self._by_id.values():
                        if s.tenant_id == sim.tenant_id:
                            s.is_default = s.id == sim_id
                return sim
        from sqlalchemy import update as sa_update
        from .db import SimORM, session_scope
        with session_scope() as s:
            row = s.get(SimORM, sim_id)
            if row is None:
                return None
            make_default = bool(fields.get("is_default"))
            if make_default:
                s.execute(
                    sa_update(SimORM)
                    .where(SimORM.tenant_id == row.tenant_id)
                    .values(is_default=False)
                )
                row.is_default = True
            for k, v in fields.items():
                if k not in mutable or k == "is_default":
                    continue
                setattr(row, k, v)
            row.updated_at = time.time()
            s.flush()
            return self._orm_to_sim(row)

    def delete(self, sim_id: str) -> bool:
        if not self._enabled():
            with self._lock:
                sim = self._by_id.pop(sim_id, None)
                if not sim:
                    return False
                # If we just deleted the default, promote the next one in-tenant.
                if sim.is_default:
                    remaining = [s for s in self._by_id.values() if s.tenant_id == sim.tenant_id]
                    if remaining:
                        remaining[0].is_default = True
                return True
        from sqlalchemy import select
        from .db import SimORM, session_scope
        with session_scope() as s:
            row = s.get(SimORM, sim_id)
            if row is None:
                return False
            was_default = bool(row.is_default)
            tid = row.tenant_id
            s.delete(row)
            s.flush()
            if was_default:
                successor = s.execute(
                    select(SimORM)
                    .where(SimORM.tenant_id == tid)
                    .order_by(SimORM.created_at.asc())
                    .limit(1)
                ).scalar_one_or_none()
                if successor is not None:
                    successor.is_default = True
            return True

    def count(self, tenant_id: Optional[str] = None) -> int:
        if not self._enabled():
            with self._lock:
                if tenant_id is None:
                    return len(self._by_id)
                return sum(1 for s in self._by_id.values() if s.tenant_id == tenant_id)
        from sqlalchemy import func, select
        from .db import SimORM, session_scope
        with session_scope() as s:
            stmt = select(func.count(SimORM.id))
            if tenant_id is not None:
                stmt = stmt.where(SimORM.tenant_id == tenant_id)
            return int(s.execute(stmt).scalar_one())


sim_store = SimStore()


def new_sim_id() -> str:
    """Generate a unique SIM identifier."""
    return f"sim_{uuid.uuid4().hex[:14]}"
