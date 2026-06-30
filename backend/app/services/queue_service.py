"""Call-center queue dispatcher.

When every agent is busy, an inbound call is held in :data:`call_queue`
(in-memory FIFO). This module is responsible for popping the head of the
queue and bridging it to a newly-free agent — the function is called from
two places:

1. The queued call's own TwiML poll (``/twilio/voice/queue/wait/<sid>``)
   — happens every ``QUEUE_POLL_SECONDS`` as a safety net.
2. The leg-status webhook, the moment any agent transitions from busy
   → idle. This is the primary, low-latency path: a queued caller is
   typically connected within a second of an agent hanging up.

Both paths are race-safe because :class:`CallQueue.pop_next` is
synchronised, and ``redirect_call`` is idempotent — if Twilio has
already moved on (e.g. the customer hung up), the REST call returns a
404 which we swallow.
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

from app.db import DEFAULT_TENANT_ID
from app.models import Agent, agent_store, call_queue, call_store
from app.services.realtime import broadcast_event
from app.services.twilio_service import (
    redirect_call,
    twiml_dial_agents,
)

logger = logging.getLogger(__name__)

_EVT_QUEUE_UPDATED = "queue.updated"


def _pick_agent(tenant_id: str) -> Optional[Agent]:
    """Return the next agent in ``tenant_id`` to dispatch a queued call to."""
    eligible = agent_store.list_eligible_for_routing(
        sort_by_idle=True, tenant_id=tenant_id,
    )
    return eligible[0] if eligible else None


def try_dispatch_next(tenant_id: str = DEFAULT_TENANT_ID) -> Optional[Tuple[str, str]]:
    """Pop the head of ``tenant_id``'s queue and dial it to a newly-free agent.

    Returns ``(call_sid, agent_identity)`` on success, ``None`` if either
    the queue is empty or no agent is currently free.
    """
    if call_queue.depth(tenant_id) == 0:
        return None

    agent = _pick_agent(tenant_id)
    if agent is None:
        return None

    # Pop AFTER we know an agent is free so a transient "no agent" state
    # doesn't lose the queue entry.
    queued = call_queue.pop_next(tenant_id)
    if queued is None:
        return None

    # Eagerly mark the agent busy so a sibling dispatch (e.g. two leg-status
    # callbacks firing in parallel) can't double-book them. The status
    # webhook will re-confirm the call_sid once Twilio rings the agent.
    agent_store.set_busy(agent.identity, queued.call_sid, tenant_id=tenant_id)

    call = call_store.get(queued.call_sid)
    caller_id = (call.from_number if call else queued.from_number) or ""

    twiml = twiml_dial_agents([agent.identity], caller_id=caller_id)

    try:
        redirect_call(queued.call_sid, twiml, tenant_id=tenant_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Queue dispatch failed for call=%s agent=%s: %s — re-enqueuing.",
            queued.call_sid, agent.identity, exc,
        )
        # Compensating action: free the agent and put the call back in the
        # queue so the next free agent (or the next /queue/wait poll) tries
        # again. Idempotent because :meth:`CallQueue.enqueue` checks for
        # duplicates.
        agent_store.set_idle(agent.identity, only_if_call_sid=queued.call_sid, tenant_id=tenant_id)
        call_queue.enqueue(queued)
        broadcast_event(_EVT_QUEUE_UPDATED, _queue_snapshot(tenant_id), tenant_id=tenant_id)
        return None

    if call:
        call_store.update(queued.call_sid, agent_identity=agent.identity, status="ringing")

    broadcast_event(
        "call.dispatched_from_queue",
        {
            "call_sid": queued.call_sid,
            "call_uuid": queued.call_sid,
            "agent_identity": agent.identity,
        },
        tenant_id=tenant_id,
    )
    dispatched = agent_store.get(agent.identity, tenant_id=tenant_id)
    if dispatched is not None:
        broadcast_event("agent.updated", dispatched.to_dict(), tenant_id=tenant_id)
    broadcast_event(_EVT_QUEUE_UPDATED, _queue_snapshot(tenant_id), tenant_id=tenant_id)

    logger.info("📤 Dispatched queued call %s → agent %s", queued.call_sid, agent.identity)
    return (queued.call_sid, agent.identity)


def enqueue_with_broadcast(
    *,
    call_sid: str,
    from_number: str,
    to_number: str,
    service_code: str | None = None,
    service_label: str | None = None,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> int:
    """Add a call to the queue and notify subscribers. Returns 1-based position."""
    from app.models import QueuedCall

    pos = call_queue.enqueue(QueuedCall(
        call_sid=call_sid,
        tenant_id=tenant_id,
        from_number=from_number,
        to_number=to_number,
        service_code=service_code,
        service_label=service_label,
    ))
    broadcast_event(_EVT_QUEUE_UPDATED, _queue_snapshot(tenant_id), tenant_id=tenant_id)
    return pos


def remove_with_broadcast(call_sid: str, tenant_id: str = DEFAULT_TENANT_ID) -> None:
    """Drop a call from the queue (e.g. caller hung up while waiting)."""
    if call_queue.remove(call_sid, tenant_id=tenant_id):
        broadcast_event(_EVT_QUEUE_UPDATED, _queue_snapshot(tenant_id), tenant_id=tenant_id)


def _queue_snapshot(tenant_id: str = DEFAULT_TENANT_ID) -> dict:
    return {
        "depth": call_queue.depth(tenant_id),
        "calls": [c.to_dict() for c in call_queue.list_calls(tenant_id=tenant_id)],
    }


def queue_snapshot(tenant_id: str = DEFAULT_TENANT_ID) -> dict:
    """Public snapshot helper used by the WS handler."""
    return _queue_snapshot(tenant_id)
