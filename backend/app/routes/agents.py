"""Agent registry — admin/agent CRUD + status."""

from __future__ import annotations

import re
from typing import Optional

from flask import Blueprint, g, request

from app.models import Agent, agent_store, user_store
from app.services.auth_service import require_admin, require_auth
from app.services.realtime import broadcast_event
from app.services.tenant_context import current_tenant_id
from app.utils import fail, ok

agents_bp = Blueprint("agents", __name__)


def _identity_for_user(user_id: str) -> str:
    """Mirror of routes/auth.py — Voice SDK identities can't have ``@`` etc."""
    cleaned = re.sub(r"[^A-Za-z0-9_\-]", "_", user_id)
    return cleaned or "agent"


def ensure_agents_for_users(tenant_id: str) -> None:
    """Make sure every workspace user in ``tenant_id`` has an Agent record.

    The agent registry (``agent_store``) is populated lazily — typically when
    a user fetches a Voice SDK token for the first time. Until that happens,
    invited teammates don't show up in the admin transfer picker even though
    they exist in ``user_store``.

    This helper bridges the gap by walking the tenant's users and creating an
    ``offline`` agent for any that don't have one yet. Safe to call on every
    snapshot / list request — it's a no-op when everything is already in
    sync.
    """
    try:
        users = user_store.list_all(tenant_id=tenant_id)
    except Exception:  # noqa: BLE001
        return

    for u in users:
        identity = _identity_for_user(u.id)
        existing = agent_store.get(identity, tenant_id=tenant_id)
        if existing is None:
            agent_store.upsert(Agent(
                identity=identity,
                tenant_id=tenant_id,
                name=u.full_name or u.email,
                role="admin" if u.role == "admin" else "agent",
                email=u.email,
                presence="offline",
            ))
        else:
            # Keep name / role / email in sync if the team page changed them.
            wanted_role = "admin" if u.role == "admin" else "agent"
            if (existing.name != (u.full_name or u.email)
                    or existing.role != wanted_role
                    or existing.email != u.email):
                existing.name = u.full_name or u.email
                existing.role = wanted_role  # type: ignore[assignment]
                existing.email = u.email
                agent_store.upsert(existing)


@agents_bp.get("/agents")
@require_auth
def list_agents():
    tid = current_tenant_id()
    ensure_agents_for_users(tid)
    return ok({"agents": [a.to_dict() for a in agent_store.list_all(tenant_id=tid)]})


@agents_bp.post("/agents")
@require_admin
def upsert_agent():
    body = request.get_json(silent=True) or {}
    identity = (body.get("identity") or "").strip()
    if not identity or " " in identity:
        return fail("invalid_identity", status=400)

    raw_status = (body.get("status") or body.get("presence") or "offline").lower()
    # Old API accepted ``busy`` here; map it to ``available`` (busy is now
    # auto-managed by the call-status webhook).
    if raw_status not in {"available", "away", "offline"}:
        raw_status = "offline"

    agent = agent_store.upsert(Agent(
        identity=identity,
        tenant_id=current_tenant_id(),
        name=body.get("name") or identity,
        role=body.get("role") or "agent",
        email=body.get("email"),
        presence=raw_status,  # type: ignore[arg-type]
    ))
    broadcast_event("agent.upserted", agent.to_dict())
    return ok({"agent": agent.to_dict()})


@agents_bp.patch("/agents/<identity>")
@require_auth
def update_agent(identity: str):
    tid = current_tenant_id()
    body = request.get_json(silent=True) or {}
    agent = agent_store.get(identity, tenant_id=tid)
    if not agent:
        return fail("agent_not_found", status=404)

    new_status: Optional[str] = body.get("status") or body.get("presence")
    if new_status:
        if new_status not in {"available", "busy", "away", "offline"}:
            return fail("invalid_status", status=400)
        agent_store.set_status(identity, new_status, tenant_id=tid)  # type: ignore[arg-type]

    if "name" in body:
        agent.name = body["name"]
    if "email" in body:
        agent.email = body["email"]
    if "role" in body:
        agent.role = body["role"]
    agent_store.upsert(agent)

    broadcast_event("agent.updated", agent.to_dict())
    return ok({"agent": agent.to_dict()})


# --------------------------------------------------------------------------- #
#  Self-service presence endpoint — used by the topbar status pill so the
#  agent can flip between Available / On break / Offline without an admin.
# --------------------------------------------------------------------------- #

@agents_bp.post("/agents/me/presence")
@require_auth
def set_my_presence():
    """Body: ``{"presence": "available" | "away" | "offline"}``.

    Returns the updated agent. The matching :class:`Agent` is auto-created
    if the user just signed in but hasn't fetched a Voice SDK token yet.
    """
    body = request.get_json(silent=True) or {}
    presence = (body.get("presence") or body.get("status") or "").lower()
    if presence not in {"available", "away", "offline"}:
        return fail("invalid_presence", status=400,
                    hint="Allowed values: available, away, offline.")

    user = g.current_user
    identity = _identity_for_user(user.id)
    tid = user.tenant_id

    if not agent_store.get(identity, tenant_id=tid):
        agent_store.upsert(Agent(
            identity=identity,
            tenant_id=tid,
            name=user.full_name or user.email,
            role="admin" if user.role == "admin" else "agent",
            email=user.email,
            presence=presence,  # type: ignore[arg-type]
        ))
    else:
        agent_store.set_presence(identity, presence, tenant_id=tid)  # type: ignore[arg-type]

    agent = agent_store.get(identity, tenant_id=tid)
    if agent:
        broadcast_event("agent.updated", agent.to_dict())
    return ok({"agent": agent.to_dict() if agent else None})


@agents_bp.delete("/agents/<identity>")
@require_admin
def delete_agent(identity: str):
    if identity == "admin":
        return fail("cannot_delete_admin", status=400)
    tid = current_tenant_id()
    removed = agent_store.remove(identity, tenant_id=tid)
    if not removed:
        return fail("agent_not_found", status=404)
    broadcast_event("agent.removed", {"identity": identity})
    return ok({"identity": identity})

