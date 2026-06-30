"""Twilio Programmable Voice webhooks.

These endpoints are called by Twilio's servers in response to call events.
They MUST be reachable from the public internet (use ngrok in dev) and
they return TwiML XML that tells Twilio what to do next.

Webhook URLs to configure in the Twilio console:

* Phone-number → Voice "A Call Comes In"
      POST  {PUBLIC_BASE_URL}/twilio/voice/incoming
* Phone-number → Voice "Call Status Changes"      (optional but recommended)
      POST  {PUBLIC_BASE_URL}/twilio/voice/status
* TwiML App   → Voice → Request URL
      POST  {PUBLIC_BASE_URL}/twilio/voice/outgoing
"""

from __future__ import annotations

import logging
import time

from flask import Blueprint, Response, request
from twilio.twiml.voice_response import VoiceResponse

from app.db import DEFAULT_TENANT_ID
from app.models import Call, agent_store, call_queue, call_store
from app.services.ivr import (
    IVR_REPEAT_DIGITS,
    lookup as ivr_lookup,
    twiml_invalid_then_repeat,
    twiml_selection_confirm,
    twiml_welcome_menu,
)
from app.services.queue_service import (
    enqueue_with_broadcast,
    remove_with_broadcast,
    try_dispatch_next,
)
from app.services.realtime import broadcast_event
from app.services.sim_service import resolve_caller_id
from app.services.tenant_context import (
    get_tenant,
    resolve_tenant_id_for_call,
    resolve_tenant_id_for_number,
    sara_config,
)
from app.services.twilio_service import (
    twiml_connect_sara,
    twiml_dial_agents,
    twiml_dial_number,
    twiml_enqueue_hold,
    twiml_queue_voicemail,
    twiml_route_inbound_sip,
    twiml_voicemail_fallback,
)
from app.utils import verify_twilio_signature
from config import settings

logger = logging.getLogger(__name__)

webhooks_bp = Blueprint("webhooks", __name__)


def _twiml(response: VoiceResponse | str) -> Response:
    return Response(str(response), mimetype="application/xml")


def _tenant_for_call(call_sid: str, *, to_number: str = "", from_number: str = "") -> str:
    """Resolve the owning tenant for a Twilio webhook.

    Order of precedence:

    1. The tracked call's ``tenant_id`` (set when the call was created).
    2. The dialed ``To`` number → SIM owner (inbound).
    3. The ``From`` number → SIM owner (outbound legs).
    4. The default tenant (single-workspace / ``.env`` deployments).
    """
    if call_sid:
        tid = resolve_tenant_id_for_call(call_sid)
        if tid != DEFAULT_TENANT_ID:
            return tid
    if to_number:
        tid = resolve_tenant_id_for_number(to_number)
        if tid != DEFAULT_TENANT_ID:
            return tid
    if from_number:
        tid = resolve_tenant_id_for_number(from_number)
        if tid != DEFAULT_TENANT_ID:
            return tid
    return DEFAULT_TENANT_ID


# --------------------------------------------------------------------------- #
#  Inbound — customer calls our support number
# --------------------------------------------------------------------------- #

@webhooks_bp.post("/incoming")
@verify_twilio_signature
def incoming_call():
    """Twilio hits this when a customer dials our support number.

    Flow:

    1. **IVR welcome menu** (when ``IVR_ENABLED`` is on, the default).
       Plays *"Welcome to <company>. Press 1 for…"* and gathers a single
       digit. The handler at ``/ivr/handle`` records the selection on
       the call and then runs the routing logic below.
    2. **Routing** (either after IVR, or directly when IVR is off).

       a. If at least one agent is currently *eligible* (presence ==
          ``available`` AND not already on a call), ring all of them in
          parallel. The first to answer wins; the rest stop ringing.
       b. If everyone is busy/away/offline, the caller goes into a hold
          queue with music + a "you are #N in line" announcement. The
          leg-status webhook actively redirects the longest-waiting
          queued call to the next agent who frees up.
       c. As a last resort (max wait exceeded with nobody free) the
          queue falls through to the configured PSTN fallback or
          voicemail.
    """
    call_sid = request.form.get("CallSid", "")
    from_number = request.form.get("From", "")
    to_number = request.form.get("To", "")
    tenant_id = _tenant_for_call(call_sid, to_number=to_number)
    sara = sara_config(get_tenant(tenant_id))
    logger.info("📞 [incoming] call_sid=%s tenant=%s from=%s to=%s ivr=%s",
                call_sid, tenant_id, from_number, to_number, settings.ivr_enabled)

    # Track the call. The IVR handlers patch the same record with the
    # selected service before dispatching.
    call = call_store.create(Call(
        call_sid=call_sid,
        tenant_id=tenant_id,
        direction="inbound",
        from_number=from_number,
        to_number=to_number,
        status="ringing",
    ))
    broadcast_event("call.incoming", call.to_dict(), tenant_id=tenant_id)

    # --- concurrency cap --------------------------------------------------
    max_cc = settings.max_concurrent_calls
    if max_cc > 0:
        active_now = call_store.active_count(tenant_id=tenant_id)
        if active_now >= max_cc:
            logger.warning(
                "🛑 [incoming] %s → concurrency limit reached (%d/%d active), "
                "queuing or forwarding.",
                call_sid, active_now, max_cc,
            )
            # Queue the call — it will be dispatched when a line frees up,
            # or fall through to voicemail/Sara after queue_max_wait_seconds.
            if settings.ivr_enabled:
                return _twiml(twiml_welcome_menu(repeat=0))
            # Queue directly (skip agent ring since all lines are full)
            pos = enqueue_with_broadcast(
                call_sid=call_sid,
                from_number=from_number,
                to_number=to_number,
                service_code=call.service_code,
                service_label=call.service_label,
                tenant_id=tenant_id,
            )
            call_store.update(call_sid, status="queued")
            return _twiml(twiml_hold_music(pos, tenant_id=tenant_id))

    # AI-first (opt-in, OFF by default): only when SARA_ANSWER_FIRST=1 does
    # Sara answer every inbound call directly — no IVR menu and no agent
    # availability check. Requires Sara to be configured (Deepgram key).
    # Left OFF (the default), we fall through to the availability-first
    # routing below, which rings any available agent/admin and only hands
    # the call to Sara when nobody on the team is free.
    if sara.answer_first and sara.is_configured():
        logger.info(
            "🤖 [incoming] %s → AI-first (SARA_ANSWER_FIRST=1), connecting caller to Sara directly.",
            call_sid,
        )
        call_store.update(call_sid, status="in-progress")
        return _twiml(twiml_connect_sara(
            call_sid,
            from_number=from_number,
            to_number=to_number,
            from_agent=None,
        ))

    if settings.ivr_enabled:
        logger.info("🎛  [incoming] %s → playing welcome menu.", call_sid)
        return _twiml(twiml_welcome_menu(repeat=0))

    # Default path — availability-first routing. _route_inbound_call() rings
    # every eligible agent/admin first and ONLY falls back to Sara (the AI)
    # when no human is reachable.
    logger.info(
        "🧭 [incoming] %s → checking team availability first.",
        call_sid,
    )
    return _twiml(_route_inbound_call(call_sid))


# --------------------------------------------------------------------------- #
#  IVR — welcome menu + digit handler
# --------------------------------------------------------------------------- #

@webhooks_bp.post("/ivr/menu")
@verify_twilio_signature
def ivr_menu():
    """Re-play the welcome + menu (used after a Gather timeout or invalid digit)."""
    repeat_str = request.args.get("repeat") or request.form.get("repeat") or "0"
    try:
        repeat = max(0, int(repeat_str))
    except ValueError:
        repeat = 0
    if repeat >= settings.ivr_max_repeats:
        # Caller didn't select anything after several tries — route them
        # without a service tag so they still reach a human.
        call_sid = request.form.get("CallSid", "")
        logger.info("🎛  IVR: %s exceeded max repeats — routing without selection.", call_sid)
        return _twiml(_route_inbound_call(call_sid))
    return _twiml(twiml_welcome_menu(repeat=repeat))


@webhooks_bp.post("/ivr/timeout")
@verify_twilio_signature
def ivr_timeout():
    """Last-chance route invoked when the menu has been re-prompted too many
    times. Routes the call without a service tag so the caller still
    reaches an agent.
    """
    call_sid = request.form.get("CallSid", "")
    logger.info("🎛  IVR timeout fallthrough for %s", call_sid)
    return _twiml(_route_inbound_call(call_sid))


@webhooks_bp.post("/ivr/handle")
@verify_twilio_signature
def ivr_handle():
    """Receives the digit the caller pressed.

    Twilio posts ``Digits`` (the gathered key sequence). We:

    * recognise *"repeat"* digits (9 / 0 / *) and re-play the menu;
    * map a valid 1-5 digit to a service, persist it on the call, and
      kick off routing;
    * on anything else, play a short "I didn't catch that" and re-prompt.
    """
    call_sid = request.form.get("CallSid", "")
    digits = (request.form.get("Digits") or "").strip()
    repeat_str = request.args.get("repeat") or request.form.get("repeat") or "0"
    try:
        repeat = max(0, int(repeat_str))
    except ValueError:
        repeat = 0

    # Caller asked to listen again.
    if digits in IVR_REPEAT_DIGITS:
        logger.info("🎛  IVR repeat requested for %s (digit=%s)", call_sid, digits)
        return _twiml(twiml_welcome_menu(repeat=0))

    option = ivr_lookup(digits)
    if option is None:
        logger.info(
            "🎛  IVR invalid choice for %s (digit=%r, repeat=%d)",
            call_sid, digits, repeat,
        )
        next_repeat = repeat + 1
        if next_repeat >= settings.ivr_max_repeats:
            return _twiml(_route_inbound_call(call_sid))
        return _twiml(twiml_invalid_then_repeat(repeat=next_repeat))

    # Persist the selection on the call so the operator UI can show the
    # service tag *before* the call is answered.
    call_store.update(
        call_sid,
        service_code=option.code,
        service_label=option.label,
    )
    call = call_store.get(call_sid)
    if call:
        broadcast_event("call.service_selected", call.to_dict(), tenant_id=call.tenant_id)
    logger.info(
        "🎛  IVR selection %s → %s (%s)",
        call_sid, option.code, option.label,
    )
    return _twiml(twiml_selection_confirm(option))


@webhooks_bp.post("/ivr/route")
@verify_twilio_signature
def ivr_route():
    """Final step of the IVR flow: actually dial / queue the caller.

    Split out so :func:`twiml_selection_confirm` can play a one-line
    "Connecting you to X" message *before* Twilio transitions to the
    Dial verb. (Putting <Say> + <Dial> in the same TwiML response works,
    but Twilio sometimes truncates the Say when the Dial answers
    quickly — a redirect is more reliable.)
    """
    call_sid = request.form.get("CallSid", "")
    return _twiml(_route_inbound_call(call_sid))


# --------------------------------------------------------------------------- #
#  Routing helper (shared by IVR + bypass paths)
# --------------------------------------------------------------------------- #

def _route_inbound_call(call_sid: str) -> str:
    """Decide what TwiML to return for ``call_sid``: dial agents, enqueue,
    or fall back to voicemail. Pure function over ``call_store`` /
    ``agent_store`` state — safe to invoke from any IVR exit point.
    """
    call = call_store.get(call_sid)
    if call is None:
        # Shouldn't happen — Twilio called us back for a SID we don't
        # know — but bail safely rather than 500'ing.
        logger.warning("🛑 [route] unknown call_sid=%s — sending to voicemail.", call_sid)
        return str(twiml_voicemail_fallback())

    tenant_id = call.tenant_id
    from_number = call.from_number
    to_number = call.to_number

    eligible = agent_store.list_eligible_for_routing(sort_by_idle=True, tenant_id=tenant_id)
    registered_count = len(agent_store.list_all(tenant_id=tenant_id))
    caller_id = from_number or resolve_caller_id(tenant_id=tenant_id) or ""
    sara = sara_config(get_tenant(tenant_id))
    sara_ready = sara.auto_fallback and sara.is_configured()

    logger.info(
        "🧭 [route] call_sid=%s tenant=%s from=%s to=%s eligible=%d registered=%d "
        "sara_auto_fallback=%s sara_configured=%s",
        call_sid, tenant_id, from_number, to_number,
        len(eligible), registered_count,
        sara.auto_fallback, sara.is_configured(),
    )

    if eligible:
        identities = [a.identity for a in eligible]
        if settings.ring_strategy == "longest_idle":
            identities = identities[:1]
        logger.info(
            "👥 [route] %s → dialing agent(s)=%s", call_sid, identities,
        )
        return twiml_dial_agents(identities, caller_id=caller_id)

    # Nobody is currently available (whether they're not registered,
    # offline, away, busy, or simply not signed in). Hand the call to
    # Sara so the caller still reaches *someone* — the AI is always on.
    if sara_ready:
        logger.info(
            "🤖 [route] %s → no human available, handing to Sara.",
            call_sid,
        )
        return twiml_connect_sara(
            call_sid,
            from_number=from_number,
            to_number=to_number,
            from_agent=None,
        )

    # Sara is disabled or not configured — keep the legacy behaviour:
    # if no agents at all, send to voicemail; otherwise enqueue.
    if not agent_store.has_any_registered_agent(tenant_id=tenant_id):
        logger.warning(
            "📭 [route] %s → no registered agents AND Sara not ready "
            "(SARA_AUTO_FALLBACK=%s, DEEPGRAM_API_KEY set=%s) — voicemail.",
            call_sid, sara.auto_fallback, bool(sara.deepgram_api_key),
        )
        return twiml_dial_agents([], caller_id=caller_id)

    # Enqueue + send the caller to hold music. ``/queue/wait/<sid>`` will
    # re-evaluate every ``queue_poll_seconds``; the leg-status webhook
    # also actively redirects this call when an agent frees up.
    pos = enqueue_with_broadcast(
        call_sid=call_sid,
        from_number=from_number,
        to_number=to_number,
        service_code=call.service_code,
        service_label=call.service_label,
        tenant_id=tenant_id,
    )
    call_store.update(call_sid, status="queued")
    updated = call_store.get(call_sid)
    broadcast_event(
        "call.queued",
        {**updated.to_dict(), "queue_position": pos},  # type: ignore[union-attr]
        tenant_id=tenant_id,
    )
    logger.info(
        "🕒 [route] %s → enqueued at position=%d (Sara not ready, agents busy).",
        call_sid, pos,
    )
    return twiml_enqueue_hold(call_sid, position=pos, greet=True)


# --------------------------------------------------------------------------- #
#  Queue waiter — polled by the on-hold call every queue_poll_seconds
# --------------------------------------------------------------------------- #

def _queue_timeout_twiml(call_sid: str, call, tenant_id: str) -> Response:
    """Caller waited past the max — hand to Sara (if ready) or voicemail."""
    remove_with_broadcast(call_sid, tenant_id)
    if call:
        call_store.update(call_sid, status="no-answer")
    sara = sara_config(get_tenant(tenant_id))
    if sara.auto_fallback and sara.is_configured():
        logger.info("⏰ Queued call %s exceeded max wait — handing to Sara.", call_sid)
        return _twiml(twiml_connect_sara(
            call_sid,
            from_number=(call.from_number if call else ""),
            to_number=(call.to_number if call else ""),
            from_agent=None,
        ))
    logger.info("⏰ Queued call %s exceeded max wait — sending to voicemail.", call_sid)
    return _twiml(twiml_queue_voicemail())


@webhooks_bp.post("/queue/wait/<call_sid>")
@verify_twilio_signature
def queue_wait(call_sid: str):
    """Re-evaluate a queued call: dial it now, keep it on hold, or voicemail."""
    queued = call_queue.peek(call_sid)
    call = call_store.get(call_sid)

    # If something already pulled this call out of the queue (e.g. an
    # active dispatch redirected it mid-poll), Twilio is just catching up
    # — let Twilio's own state win and hang up cleanly.
    if not queued:
        logger.info("🕒 [queue/wait] %s already dispatched, hanging up cleanly.", call_sid)
        return _twiml(VoiceResponse().hangup())

    tenant_id = queued.tenant_id
    logger.info(
        "🕒 [queue/wait] %s tenant=%s waited=%ss limit=%ss",
        call_sid, tenant_id, queued.wait_seconds, settings.queue_max_wait_seconds,
    )

    # Caller has been waiting too long → Sara (if enabled) or voicemail.
    if queued.wait_seconds >= settings.queue_max_wait_seconds:
        return _queue_timeout_twiml(call_sid, call, tenant_id)

    # If an agent is now free, dial them immediately. We re-use
    # try_dispatch_next so the broadcasting + book-keeping stays
    # consistent with the active path. If it returns this call, we just
    # hang up — the REST redirect already replaced the running TwiML.
    dispatched = try_dispatch_next(tenant_id)
    if dispatched and dispatched[0] == call_sid:
        return _twiml(VoiceResponse().hangup())

    # Still busy — keep holding (and re-poll after one music loop).
    pos = call_queue.position(call_sid, tenant_id) or 1
    return _twiml(twiml_enqueue_hold(call_sid, position=pos, greet=False))


# --------------------------------------------------------------------------- #
#  Per-leg status — fired for each <Client> dialed leg
# --------------------------------------------------------------------------- #

@webhooks_bp.post("/leg-status")
@verify_twilio_signature
def leg_status():
    """Tracks which agent is currently bridged on a call.

    Twilio fires this for the dialed *child* leg of every ``<Dial><Client>``
    we emit. We use it to flip the agent's busy state automatically:

    * On ``in-progress`` → mark the agent busy + record the parent
      CallSid as their ``current_call_sid``. Routing skips them from
      now on.
    * On any terminal status → mark them idle and try to drain the next
      queued call (if any) onto them.
    """
    call_status = (request.form.get("CallStatus") or "").lower()
    parent_sid = request.form.get("ParentCallSid") or request.form.get("CallSid", "")
    leg_sid = request.form.get("CallSid", "")
    to = request.form.get("To", "")

    identity = ""
    if to.startswith("client:"):
        identity = to.split(":", 1)[1].strip()

    if not identity:
        # Not a <Client> leg (e.g. a PSTN child) — nothing to do.
        return ("", 204)

    tenant_id = _tenant_for_call(parent_sid or leg_sid)

    if not agent_store.get(identity, tenant_id=tenant_id):
        return ("", 204)

    def _broadcast_agent() -> None:
        agent = agent_store.get(identity, tenant_id=tenant_id)
        if agent:
            broadcast_event("agent.updated", agent.to_dict(), tenant_id=tenant_id)

    if call_status in {"in-progress", "answered"}:
        agent_store.set_busy(identity, parent_sid or leg_sid, tenant_id=tenant_id)
        _broadcast_agent()
    elif call_status in {"completed", "busy", "no-answer", "canceled", "failed"}:
        agent_store.set_idle(identity, only_if_call_sid=parent_sid or leg_sid, tenant_id=tenant_id)
        _broadcast_agent()
        # Fastest possible queue drain: if a customer is waiting, redirect
        # them to this newly-free agent right now.
        try:
            try_dispatch_next(tenant_id)
        except Exception:  # noqa: BLE001
            logger.exception("Queue dispatch from leg-status failed")

    return ("", 204)


# --------------------------------------------------------------------------- #
#  Inbound SIP — call lands on our SIP Domain (e.g. sip:alice@x.sip.twilio.com)
# --------------------------------------------------------------------------- #

@webhooks_bp.post("/sip/incoming")
@verify_twilio_signature
def incoming_sip_call():
    """Voice URL for the SIP Domain (see twilio.txt §17 & §11).

    Twilio POSTs the same params as ``/incoming`` plus SIP-specific ones:
        SipDomain      = the matched ``*.sip.twilio.com`` domain
        SipCallId      = the SIP-protocol Call-ID header
        SipSourceIp    = the originating SIP edge IP (when source-IP ACL'd)
        SipUsername    = the digest-auth username (when credential-list auth'd)
        To             = ``sip:user@yourdomain.sip.twilio.com``
        From           = ``sip:caller@somewhere`` (or PSTN if INVITE came from PSTN)
    """
    call_sid = request.form.get("CallSid", "")
    from_uri = request.form.get("From", "")
    to_uri = request.form.get("To", "")
    sip_user = (
        request.form.get("SipUsername")
        or _user_from_sip_uri(to_uri)
        or ""
    ).strip().lower()

    logger.info("📞 SIP call %s from %s → %s (sip_user=%s)",
                call_sid, from_uri, to_uri, sip_user)

    tenant_id = _tenant_for_call(call_sid, to_number=to_uri, from_number=from_uri)

    call = call_store.create(Call(
        call_sid=call_sid,
        tenant_id=tenant_id,
        direction="inbound",
        from_number=from_uri,
        to_number=to_uri,
        status="ringing",
    ))
    broadcast_event("call.incoming", call.to_dict(), tenant_id=tenant_id)

    twiml = twiml_route_inbound_sip(
        sip_user,
        caller_id=from_uri or resolve_caller_id(tenant_id=tenant_id) or "",
    )
    return _twiml(twiml)


def _user_from_sip_uri(uri: str) -> str:
    """Extract the user portion from ``sip:user@host;params``."""
    if not uri:
        return ""
    # strip scheme
    if "://" in uri:
        uri = uri.split("://", 1)[1]
    elif ":" in uri:
        uri = uri.split(":", 1)[1]
    # strip "@host..." onwards
    user = uri.split("@", 1)[0]
    # strip any URI parameters (sip:alice;tag=…@host)
    return user.split(";", 1)[0].split("?", 1)[0]


# --------------------------------------------------------------------------- #
#  Outbound — agent's softphone places a call (TwiML App "Voice URL")
# --------------------------------------------------------------------------- #

@webhooks_bp.post("/outgoing")
@verify_twilio_signature
def outgoing_call():
    """
    Called by Twilio when a Voice SDK softphone (browser/mobile) initiates an
    outbound call via the TwiML App. The destination number is sent as
    ``To`` (we send it from the SDK's ``Device.connect({ params: { To } })``).
    """
    to = (request.form.get("To") or "").strip()
    from_identity = request.form.get("From", "")  # the agent's Voice SDK identity
    # The softphone may send a custom ``FromNumber`` param (the SIM the
    # agent picked in the dialer) — see Device.connect({ params: ... }).
    from_number_param = (request.form.get("FromNumber") or "").strip()

    if not to:
        # No destination → just drop with a friendly message.
        resp = VoiceResponse()
        resp.say("Sorry, no destination number was provided.", voice="alice")
        resp.hangup()
        return _twiml(resp)

    call_sid = request.form.get("CallSid", "")
    tenant_id = _tenant_for_call(call_sid, from_number=from_number_param)
    caller_id = resolve_caller_id(from_number_param, tenant_id=tenant_id) or ""
    if not caller_id:
        resp = VoiceResponse()
        resp.say(
            "No outbound number is configured. Please add a SIM card first.",
            voice="alice",
        )
        resp.hangup()
        return _twiml(resp)

    logger.info("\U0001f4e4 Outbound from agent=%s → %s (from=%s tenant=%s)",
                from_identity, to, caller_id, tenant_id)

    # Track in our store. Twilio will POST the CallSid via /status as well.
    if call_sid and not call_store.get(call_sid):
        call_store.create(Call(
            call_sid=call_sid,
            tenant_id=tenant_id,
            direction="outbound",
            from_number=caller_id,
            to_number=to,
            status="initiated",
            agent_identity=from_identity or "admin",
        ))
        broadcast_event("call.initiated", call_store.get(call_sid).to_dict(), tenant_id=tenant_id)

    twiml = twiml_dial_number(to, caller_id=caller_id)
    return _twiml(twiml)


# --------------------------------------------------------------------------- #
#  Status callback — call lifecycle updates
# --------------------------------------------------------------------------- #

@webhooks_bp.post("/status")
@verify_twilio_signature
def call_status():
    """Twilio POSTs every status change here (initiated/ringing/answered/completed)."""
    call_sid = request.form.get("CallSid", "")
    parent_sid = request.form.get("ParentCallSid", "")
    status = (request.form.get("CallStatus") or "").lower()
    direction_raw = (request.form.get("Direction") or "").lower()
    from_number = request.form.get("From", "")
    to_number = request.form.get("To", "")

    direction = "outbound" if "outbound" in direction_raw else "inbound"

    tenant_id = _tenant_for_call(call_sid, to_number=to_number, from_number=from_number)

    call = call_store.get(call_sid)
    if not call:
        # First time we've seen this leg — create it (e.g. dialed child leg).
        call = call_store.create(Call(
            call_sid=call_sid,
            tenant_id=tenant_id,
            direction=direction,
            from_number=from_number,
            to_number=to_number,
            status=status or "queued",
            parent_call_sid=parent_sid or None,
        ))

    # Map a few Twilio statuses to our internal lifecycle.
    if status == "in-progress":
        call_store.update(call_sid, status="in-progress", answered_at=time.time())
        broadcast_event("call.answered", call_store.get(call_sid).to_dict(), tenant_id=tenant_id)
    elif status in {"completed", "busy", "no-answer", "failed", "canceled"}:
        # If the caller hung up while still on hold, drop them from the queue.
        remove_with_broadcast(call_sid, tenant_id)
        ended = call_store.end(call_sid, status=status, ended_by="twilio")
        if ended:
            broadcast_event("call.ended", ended.to_dict(), tenant_id=tenant_id)
    else:
        call_store.update(call_sid, status=status or call.status)
        broadcast_event("call.status", call_store.get(call_sid).to_dict(), tenant_id=tenant_id)

    return ("", 204)


# --------------------------------------------------------------------------- #
#  Dial-action — fired when a <Dial> verb finishes
# --------------------------------------------------------------------------- #

@webhooks_bp.post("/dial-status")
@verify_twilio_signature
def dial_action():
    """
    The ``action`` URL on the inbound ``<Dial>``. Twilio hits this when the
    dial finishes (agent hung up, no-answer, etc.). If the dial failed,
    hand the caller to Sara when she's configured; otherwise fall back
    to voicemail. On a clean disconnect we just hang up.
    """
    dial_status = (request.form.get("DialCallStatus") or "").lower()
    call_sid = request.form.get("CallSid", "")
    logger.info("\u260E\uFE0F  dial-status fired with status=%s", dial_status)

    if dial_status in {"no-answer", "failed", "busy"}:
        call = call_store.get(call_sid)
        tenant_id = call.tenant_id if call else _tenant_for_call(call_sid)
        sara = sara_config(get_tenant(tenant_id))
        if sara.auto_fallback and sara.is_configured():
            logger.info(
                "\u260E\uFE0F  dial failed (%s) \u2014 handing %s to Sara.",
                dial_status, call_sid,
            )
            return _twiml(twiml_connect_sara(
                call_sid,
                from_number=(call.from_number if call else ""),
                to_number=(call.to_number if call else ""),
                from_agent=None,
            ))
        return _twiml(twiml_voicemail_fallback())

    resp = VoiceResponse()
    resp.hangup()
    return _twiml(resp)


# --------------------------------------------------------------------------- #
#  Recording callback
# --------------------------------------------------------------------------- #

@webhooks_bp.post("/recording")
@verify_twilio_signature
def recording_status():
    """Fires once the recording is ready. Stash the URL on the call."""
    call_sid = request.form.get("CallSid", "")
    recording_sid = request.form.get("RecordingSid", "")
    recording_url = request.form.get("RecordingUrl", "")
    duration = request.form.get("RecordingDuration", "")

    call_store.update(
        call_sid,
        recording_url=recording_url,
        recording_sid=recording_sid,
        recording_duration=int(duration) if duration.isdigit() else None,
    )
    call = call_store.get(call_sid)
    if call:
        broadcast_event("call.recording_ready", {
            "call_sid": call_sid,
            "recording_url": recording_url,
            "recording_sid": recording_sid,
        }, tenant_id=call.tenant_id)

    return ("", 204)
