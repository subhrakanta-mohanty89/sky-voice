"""Real-time broadcaster — pushes call/agent events to subscribed admin UIs.

A very small **per-tenant** pub/sub: each WS connection registers itself
under its workspace's ``tenant_id``, and any code path that mutates state
calls ``broadcast_event`` to fan out a JSON message to *only that tenant's*
admin clients. ``broadcast_event`` defaults the tenant to the active
request's tenant, so authenticated REST handlers need no extra wiring;
unauthenticated paths (Twilio webhooks, the queue dispatcher, Sara) pass
``tenant_id`` explicitly.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Dict, List, Optional

from simple_websocket import Server as WSServer  # type: ignore

from app.db import DEFAULT_TENANT_ID

logger = logging.getLogger(__name__)


# tenant_id -> list of subscriber WebSockets
_subscribers: Dict[str, List[WSServer]] = {}
_lock = threading.RLock()


def subscribe(ws: WSServer, tenant_id: str = DEFAULT_TENANT_ID) -> None:
    with _lock:
        _subscribers.setdefault(tenant_id, []).append(ws)
        total = sum(len(v) for v in _subscribers.values())
    logger.info("WS subscriber added (tenant=%s) — total=%d", tenant_id, total)


def unsubscribe(ws: WSServer, tenant_id: Optional[str] = None) -> None:
    with _lock:
        buckets = (
            [_subscribers.get(tenant_id, [])]
            if tenant_id is not None
            else list(_subscribers.values())
        )
        for bucket in buckets:
            try:
                bucket.remove(ws)
            except ValueError:
                pass
        total = sum(len(v) for v in _subscribers.values())
    logger.info("WS subscriber removed — total=%d", total)


def broadcast_event(
    event: str,
    payload: Dict[str, Any],
    *,
    tenant_id: Optional[str] = None,
) -> None:
    """Send ``{event, payload}`` to a single tenant's connected admin clients.

    When ``tenant_id`` is omitted it resolves from the active request (the
    logged-in user's tenant), which covers every authenticated REST caller.
    """
    if tenant_id is None:
        try:
            from app.services.tenant_context import current_tenant_id
            tenant_id = current_tenant_id()
        except Exception:  # noqa: BLE001
            tenant_id = DEFAULT_TENANT_ID

    message = json.dumps(
        {"event": event, "payload": payload},
        default=str,
    )

    with _lock:
        targets = list(_subscribers.get(tenant_id, []))

    dead: List[WSServer] = []
    for ws in targets:
        try:
            ws.send(message)
        except Exception as exc:  # noqa: BLE001
            logger.debug("WS send failed (%s) — dropping subscriber.", exc)
            dead.append(ws)

    if dead:
        with _lock:
            bucket = _subscribers.get(tenant_id, [])
            for ws in dead:
                try:
                    bucket.remove(ws)
                except ValueError:
                    pass

