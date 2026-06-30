"""WebSocket routes.

* ``/ws/admin`` — admin UI subscribes to receive live call/agent events.
  Authenticate via ``?token=<jwt>`` query string (browsers can't set
  Authorization headers on a WebSocket upgrade).
* ``/ws/sara/<call_sid>`` — Twilio Media Stream connects here when the
  call is routed to Sara (the AI receptionist). No auth — Twilio
  authenticates by signature on the preceding TwiML POST. The stream
  itself is bound to a one-time, server-generated TwiML URL so it
  can't be replayed.
"""

from __future__ import annotations

import json
import logging

from flask import request
from flask_sock import Sock

from app.models import agent_store, call_store, user_store
from app.routes.agents import ensure_agents_for_users
from app.services.auth_service import decode_jwt
from app.services.queue_service import queue_snapshot
from app.services.realtime import broadcast_event, subscribe, unsubscribe
from app.services.sara_bridge import SaraBridge

logger = logging.getLogger(__name__)


def register_websocket_routes(sock: Sock) -> None:
    @sock.route("/ws/admin")
    def admin_ws(ws):  # noqa: ANN001
        # Authenticate the WS connection.
        token = request.args.get("token") or ""
        payload = decode_jwt(token) if token else None
        user = user_store.get(payload.get("sub", "")) if payload else None
        if not user or user.status != "active":
            try:
                ws.send(json.dumps({"event": "error", "payload": {"error": "unauthenticated"}}))
                ws.close()
            except Exception:  # noqa: BLE001
                pass
            return

        logger.info("Admin WS connected for user=%s", user.email)
        tid = user.tenant_id

        # Send a snapshot so the UI can render immediately.
        # Make sure every workspace user has an agent record so freshly
        # invited teammates appear in the transfer picker right away
        # (offline) instead of only after they've logged in for the first
        # time and fetched a Voice SDK token.
        ensure_agents_for_users(tid)
        try:
            ws.send(json.dumps({
                "event": "snapshot",
                "payload": {
                    "active_calls": [c.to_dict() for c in call_store.list_active(tenant_id=tid)],
                    "agents": [a.to_dict() for a in agent_store.list_all(tenant_id=tid)],
                    "user": user.to_public(),
                    "queue": queue_snapshot(tenant_id=tid),
                },
            }))
        except Exception:  # noqa: BLE001
            return

        subscribe(ws, tid)
        try:
            while True:
                msg = ws.receive(timeout=60)
                if msg is None:
                    continue
                _handle_admin_message(msg, tid)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Admin WS closed: %s", exc)
        finally:
            unsubscribe(ws, tid)

    @sock.route("/ws/sara/<call_sid>")
    def sara_ws(ws, call_sid):  # noqa: ANN001
        """Twilio Media Stream → Sara bridge.

        Twilio opens this WebSocket immediately after the TwiML
        ``<Connect><Stream>`` is returned. The bridge handles the
        full duplex audio loop until either side closes.
        """
        logger.info("🤖 [sara-ws] OPEN call_sid=%s", call_sid)
        try:
            SaraBridge(ws, call_sid).run()
        except Exception:  # noqa: BLE001
            logger.exception("🤖 [sara-ws] CRASHED call_sid=%s", call_sid)
        finally:
            logger.info("🤖 [sara-ws] CLOSED call_sid=%s", call_sid)


def _handle_admin_message(raw: str, tenant_id: str) -> None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return

    if data.get("type") == "agent.heartbeat":
        identity = data.get("identity") or "admin"
        # ``status`` (legacy) and ``presence`` (new) are accepted; anything
        # other than the manual presence values is ignored — busy/idle
        # is auto-managed by the leg-status webhook.
        presence = data.get("presence") or data.get("status") or "available"
        if presence in {"available", "away", "offline"}:
            agent = agent_store.set_presence(identity, presence, tenant_id=tenant_id)
            if agent:
                broadcast_event("agent.updated", agent.to_dict(), tenant_id=tenant_id)
