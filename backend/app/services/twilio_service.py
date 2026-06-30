"""Twilio integration — REST client + Voice SDK access tokens + call control.

The Twilio REST client is created lazily so the rest of the app can boot
even when credentials aren't present (handy in CI / local UI demos).
"""

from __future__ import annotations

import logging
from typing import Dict, Optional

from twilio.jwt.access_token import AccessToken
from twilio.jwt.access_token.grants import VoiceGrant
from twilio.rest import Client
from twilio.twiml.voice_response import Dial, Sip, VoiceResponse

from config import settings

logger = logging.getLogger(__name__)

# Webhook paths Twilio calls back into. Centralised so the per-leg
# callback wiring can't drift across helpers.
_LEG_STATUS_PATH = "/twilio/voice/leg-status"
_LEG_STATUS_EVENTS = "initiated ringing answered completed"
_RECORDING_PATH = "/twilio/voice/recording"


# --------------------------------------------------------------------------- #
#  REST client (one cached client per tenant)
# --------------------------------------------------------------------------- #

_clients: Dict[str, Client] = {}


def get_client(tenant_id: Optional[str] = None) -> Client:
    """Return a cached Twilio REST client for the given (or current) tenant.

    Credentials resolve from the tenant's DB columns, falling back to the
    process-wide ``.env`` values. Raises if neither provides full creds.
    """
    from app.services.tenant_context import (
        current_tenant_id,
        get_tenant,
        twilio_creds,
    )

    tid = tenant_id or current_tenant_id()
    cached = _clients.get(tid)
    if cached is not None:
        return cached

    creds = twilio_creds(get_tenant(tid))
    if not creds.is_configured():
        raise RuntimeError(
            "Twilio is not configured for this tenant — set credentials in "
            "the workspace settings or .env"
        )
    client = Client(creds.api_key_sid, creds.api_key_secret, creds.account_sid)
    _clients[tid] = client
    return client


def invalidate_client(tenant_id: Optional[str] = None) -> None:
    """Drop a cached client (call after a tenant rotates its Twilio creds)."""
    if tenant_id is None:
        _clients.clear()
    else:
        _clients.pop(tenant_id, None)


# --------------------------------------------------------------------------- #
#  Access tokens for Voice SDK (web + mobile)
# --------------------------------------------------------------------------- #

def issue_access_token(
    identity: str,
    *,
    ttl_seconds: int = 3600,
    tenant_id: Optional[str] = None,
    push_credential_sid: Optional[str] = None,
) -> str:
    """
    Mint a short-lived JWT that the Twilio Voice SDK uses to register
    a softphone (browser, iOS, Android, React Native) under ``identity``,
    using the calling tenant's Twilio credentials.
    """
    from app.services.tenant_context import (
        current_tenant_id,
        get_tenant,
        twilio_creds,
    )

    tid = tenant_id or current_tenant_id()
    creds = twilio_creds(get_tenant(tid))
    if not (
        creds.account_sid
        and creds.api_key_sid
        and creds.api_key_secret
        and creds.twiml_app_sid
    ):
        raise RuntimeError(
            "Cannot issue access token — this tenant has no Twilio account "
            "SID, API key SID/secret and TwiML app SID configured."
        )

    token = AccessToken(
        creds.account_sid,
        creds.api_key_sid,
        creds.api_key_secret,
        identity=identity,
        ttl=ttl_seconds,
    )
    grant = VoiceGrant(
        outgoing_application_sid=creds.twiml_app_sid,
        incoming_allow=True,  # allow this identity to receive calls via <Client>
    )
    # Native mobile softphones (React Native / iOS / Android) receive incoming
    # calls as a push notification, which requires a Twilio Push Credential
    # SID on the grant. The browser/desktop SDK ignores it, so this stays a
    # no-op for web until TWILIO_PUSH_CREDENTIAL_SID is configured.
    push_sid = push_credential_sid or settings.twilio_push_credential_sid or None
    if push_sid:
        grant.push_credential_sid = push_sid
    token.add_grant(grant)
    return token.to_jwt()


# --------------------------------------------------------------------------- #
#  Call control via Twilio REST API
# --------------------------------------------------------------------------- #

def hangup_call(call_sid: str, *, tenant_id: Optional[str] = None) -> None:
    """Force-end a live call. Twilio raises if the call already ended."""
    get_client(tenant_id).calls(call_sid).update(status="completed")


def redirect_call(call_sid: str, twiml: str, *, tenant_id: Optional[str] = None) -> None:
    """Replace the running TwiML for a call (used for forward / hold)."""
    get_client(tenant_id).calls(call_sid).update(twiml=twiml)


def fetch_call(call_sid: str, *, tenant_id: Optional[str] = None) -> Optional[Dict]:
    """Fetch a single call straight from Twilio's REST API (tenant-scoped).

    Returns the handful of fields we track, or ``None`` when the SID doesn't
    exist in this tenant's Twilio account (HTTP 404). This lets call-control
    endpoints recover a *live* call that isn't in this process's in-memory
    store — e.g. when Cloud Run scaled out to another instance or restarted
    on a deploy and wiped the store. Scoped to the tenant's own credentials,
    so it can only ever see calls in that tenant's account (IDOR-safe).
    """
    from twilio.base.exceptions import TwilioRestException

    try:
        c = get_client(tenant_id).calls(call_sid).fetch()
    except TwilioRestException as exc:
        if exc.status == 404:
            return None
        raise

    raw_dir = (getattr(c, "direction", "") or "").lower()
    direction = "inbound" if raw_dir.startswith("inbound") else "outbound"
    return {
        "call_sid": getattr(c, "sid", call_sid),
        "direction": direction,
        "from_number": getattr(c, "from_", None) or getattr(c, "from_formatted", "") or "",
        "to_number": getattr(c, "to", None) or getattr(c, "to_formatted", "") or "",
        "status": getattr(c, "status", "in-progress") or "in-progress",
        "parent_call_sid": getattr(c, "parent_call_sid", None),
    }


def make_outbound_call(
    *,
    to: str,
    agent_identity: str,
    from_number: str,
    tenant_id: Optional[str] = None,
) -> str:
    """
    Place a PSTN→agent call: dial the customer, then bridge the agent's
    softphone (Voice SDK Client) when they pick up.

    ``from_number`` is the SIM / verified caller ID to present to the
    callee — required (callers should run it through
    :func:`app.services.sim_service.resolve_caller_id` first).

    Returns the new Twilio CallSid.
    """
    if not settings.public_base_url:
        raise RuntimeError("PUBLIC_BASE_URL must be set so Twilio can reach this server.")
    if not from_number:
        raise RuntimeError("from_number is required — add a SIM in the UI first.")

    # The agent's softphone is registered under their identity. We dial the
    # customer, and use a status callback so the backend tracks the lifecycle.
    call = get_client(tenant_id).calls.create(
        to=to,
        from_=from_number,
        # When the customer answers, bridge to the agent's <Client>.
        twiml=_build_outbound_twiml(agent_identity=agent_identity, from_number=from_number),
        status_callback=settings.webhook_url("/twilio/voice/status"),
        status_callback_event=["initiated", "ringing", "answered", "completed"],
        status_callback_method="POST",
        record=settings.record_calls,
        recording_status_callback=(
            settings.webhook_url(_RECORDING_PATH)
            if settings.record_calls else None
        ),
        recording_status_callback_event=(
            ["completed"] if settings.record_calls else None
        ),
    )
    return call.sid


def _build_outbound_twiml(*, agent_identity: str, from_number: str) -> str:
    """TwiML for an admin-initiated PSTN call: bridge to agent's softphone."""
    response = VoiceResponse()
    dial = Dial(
        caller_id=from_number,
        timeout=settings.incoming_ring_timeout,
        answer_on_bridge=True,
    )
    dial.client(
        agent_identity,
        status_callback=settings.webhook_url(_LEG_STATUS_PATH),
        status_callback_event=_LEG_STATUS_EVENTS,
        status_callback_method="POST",
    )
    response.append(dial)
    return str(response)


# --------------------------------------------------------------------------- #
#  TwiML builders used by webhook routes
# --------------------------------------------------------------------------- #

def twiml_dial_agents(agent_identities: list[str], *, caller_id: str) -> str:
    """Ring one or more agents in parallel. Falls back to voicemail."""
    response = VoiceResponse()

    if not agent_identities:
        if settings.fallback_forward_number:
            dial = Dial(
                caller_id=caller_id,
                timeout=settings.incoming_ring_timeout,
                answer_on_bridge=True,
            )
            dial.number(settings.fallback_forward_number)
            response.append(dial)
        else:
            response.say(
                "Sorry, all of our agents are currently unavailable. "
                "Please try again later.",
                voice="alice",
            )
            response.hangup()
        return str(response)

    dial = Dial(
        caller_id=caller_id,
        timeout=settings.incoming_ring_timeout,
        answer_on_bridge=True,
        action=settings.webhook_url("/twilio/voice/dial-status"),
        method="POST",
    )
    leg_status_url = settings.webhook_url(_LEG_STATUS_PATH)
    for identity in agent_identities:
        # Per-leg status callbacks let us mark each agent busy/idle the
        # moment their <Client> leg connects or terminates. That's what
        # makes the auto-routing (skip busy agents) actually correct.
        dial.client(
            identity,
            status_callback=leg_status_url,
            status_callback_event=_LEG_STATUS_EVENTS,
            status_callback_method="POST",
        )
    response.append(dial)
    return str(response)


def twiml_dial_number(to: str, *, caller_id: str) -> str:
    """TwiML used by softphone outbound calls (Voice SDK → PSTN)."""
    response = VoiceResponse()
    dial = Dial(caller_id=caller_id, answer_on_bridge=True)
    dial.number(to)
    response.append(dial)
    return str(response)


def twiml_dial_client(identity: str, *, caller_id: str) -> str:
    """TwiML for client-to-client calls (e.g. agent → another agent)."""
    response = VoiceResponse()
    dial = Dial(caller_id=caller_id, answer_on_bridge=True)
    dial.client(
        identity,
        status_callback=settings.webhook_url(_LEG_STATUS_PATH),
        status_callback_event=_LEG_STATUS_EVENTS,
        status_callback_method="POST",
    )
    response.append(dial)
    return str(response)


def twiml_enqueue_hold(call_sid: str, *, position: int, greet: bool = False) -> str:
    """TwiML used while a customer waits for an agent to free up.

    Plays a short announcement (only on the first round) followed by hold
    music, then ``<Redirect>``s back to ``/twilio/voice/queue/wait/<sid>``
    so the queue is re-evaluated. The leg-status webhook also actively
    redirects this call as soon as an agent frees up — this poll is the
    safety net for that path.
    """
    response = VoiceResponse()
    if greet:
        position_phrase = (
            "You are first in line." if position <= 1
            else f"You are number {position} in line."
        )
        response.say(
            f"{settings.queue_greeting} {position_phrase}",
            voice="alice",
        )
    response.play(settings.hold_music_url)
    response.redirect(
        settings.webhook_url(f"/twilio/voice/queue/wait/{call_sid}"),
        method="POST",
    )
    return str(response)


def twiml_queue_voicemail() -> str:
    """Final TwiML when a queued caller exceeds ``queue_max_wait_seconds``.

    Falls back to PSTN forwarding (if configured) so the caller still
    reaches a human; otherwise drops them into voicemail.
    """
    if settings.fallback_forward_number:
        response = VoiceResponse()
        response.say(
            "We're still busy. Connecting you to our backup line now.",
            voice="alice",
        )
        dial = Dial(
            caller_id=settings.fallback_forward_number,
            timeout=settings.incoming_ring_timeout,
            answer_on_bridge=True,
        )
        dial.number(settings.fallback_forward_number)
        response.append(dial)
        return str(response)
    return twiml_voicemail_fallback()


def twiml_play_hold_music() -> str:
    response = VoiceResponse()
    response.play(settings.hold_music_url, loop=0)
    return str(response)


def twiml_voicemail_fallback() -> str:
    response = VoiceResponse()
    response.say(
        "Sorry, we couldn't connect you to an agent. Please leave a message after the tone.",
        voice="alice",
    )
    response.record(
        max_length=120,
        play_beep=True,
        recording_status_callback=settings.webhook_url(_RECORDING_PATH),
        recording_status_callback_method="POST",
    )
    response.hangup()
    return str(response)


def twiml_connect_sara(
    call_sid: str,
    *,
    from_number: str = "",
    to_number: str = "",
    from_agent: Optional[str] = None,
) -> str:
    """Connect a Twilio call to the Sara AI bridge over Media Streams.

    Returns a ``<Connect><Stream>`` TwiML payload pointing at our
    ``/ws/sara/<call_sid>`` WebSocket endpoint. Custom ``<Parameter>``
    children are echoed back to us in the Media Stream ``start`` event,
    so the bridge knows the caller's number, the dialled number, and
    (when the call was admin-transferred) which agent initiated the
    transfer.
    """
    response = VoiceResponse()
    base = (settings.public_base_url or "").rstrip("/")
    if base.startswith("https://"):
        ws_base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        ws_base = "ws://" + base[len("http://"):]
    else:
        ws_base = "wss://" + base
    ws_url = f"{ws_base}/ws/sara/{call_sid}"

    connect = response.connect()
    stream = connect.stream(url=ws_url)
    if from_number:
        stream.parameter(name="from", value=from_number)
    if to_number:
        stream.parameter(name="to", value=to_number)
    if from_agent:
        stream.parameter(name="from_agent", value=from_agent)
    return str(response)


# --------------------------------------------------------------------------- #
#  SIP support (twilio.txt §10-§11 and §17-§24)
# --------------------------------------------------------------------------- #

def twiml_dial_sip(
    uri: str,
    *,
    caller_id: Optional[str] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
    timeout: Optional[int] = None,
    answer_on_bridge: bool = True,
    headers: Optional[dict] = None,
) -> str:
    """Build a TwiML response that bridges the current leg to a SIP endpoint.

    ``uri`` may be one of:
        ``sip:user@host``        — plain SIP URI
        ``sips:user@host``       — TLS-secured SIP URI
        ``sip:user@host?X-Foo=bar`` — extra SIP headers (X-* are forwarded)

    A ``;transport=tcp`` parameter is also accepted; ``;transport=tls`` is
    rejected by Twilio — use the ``sips:`` scheme instead.
    """
    if not uri or not (uri.lower().startswith("sip:") or uri.lower().startswith("sips:")):
        raise ValueError("uri must start with 'sip:' or 'sips:'")

    response = VoiceResponse()
    if not caller_id:
        from app.services.sim_service import resolve_caller_id  # local import
        caller_id = resolve_caller_id() or ""
    dial = Dial(
        caller_id=caller_id,
        timeout=timeout or settings.incoming_ring_timeout,
        answer_on_bridge=answer_on_bridge,
    )

    sip_kwargs: dict = {}
    if username:
        sip_kwargs["username"] = username
    if password:
        sip_kwargs["password"] = password

    # Append X-* headers to the URI per Twilio's SIP noun spec.
    if headers:
        extra = "&".join(f"{k}={v}" for k, v in headers.items() if k and v is not None)
        sep = "&" if "?" in uri else "?"
        uri = f"{uri}{sep}{extra}" if extra else uri

    dial.append(Sip(uri, **sip_kwargs))
    response.append(dial)
    return str(response)


def twiml_route_inbound_sip(
    requested_user: str,
    *,
    caller_id: str,
    fallback_message: str = "Sorry, that extension is not available.",
) -> str:
    """TwiML for the SIP-Domain ``voice_url``.

    Routes an inbound SIP call (e.g. someone dialing ``sip:alice@yourdomain.sip.twilio.com``)
    to the matching agent's Voice-SDK Client by identity. Falls back to
    ``fallback_forward_number`` (PSTN) when set, otherwise plays a friendly
    "unavailable" message and hangs up.
    """
    from app.models import agent_store  # local import to avoid cycle

    response = VoiceResponse()
    identity = (requested_user or "").strip().lower()

    if identity and agent_store.get(identity):
        dial = Dial(
            caller_id=caller_id,
            timeout=settings.incoming_ring_timeout,
            answer_on_bridge=True,
            action=settings.webhook_url("/twilio/voice/dial-status"),
            method="POST",
        )
        dial.client(identity)
        response.append(dial)
        return str(response)

    if settings.fallback_forward_number:
        dial = Dial(
            caller_id=caller_id,
            timeout=settings.incoming_ring_timeout,
            answer_on_bridge=True,
        )
        dial.number(settings.fallback_forward_number)
        response.append(dial)
        return str(response)

    response.say(fallback_message, voice="alice")
    response.hangup()
    return str(response)


def make_outbound_sip_call(
    *,
    to_sip_uri: str,
    from_number: Optional[str] = None,
    voice_url: Optional[str] = None,
    voice_method: str = "POST",
    timeout: Optional[int] = None,
    tenant_id: Optional[str] = None,
) -> str:
    """Place an outbound call to a SIP endpoint via the REST API (twilio.txt §10).

    Either ``voice_url`` (your TwiML handler) or the calling code's TwiML
    must drive the leg after the SIP endpoint answers. If ``voice_url`` is
    omitted, a short "connected" announcement is used so the call doesn't
    just hang in silence.
    """
    if not to_sip_uri or not to_sip_uri.lower().startswith(("sip:", "sips:")):
        raise ValueError("to_sip_uri must start with 'sip:' or 'sips:'")
    if not settings.public_base_url:
        raise RuntimeError("PUBLIC_BASE_URL must be set so Twilio can reach this server.")

    from app.services.sim_service import resolve_caller_id  # local import
    caller_id = from_number or resolve_caller_id()
    if not caller_id:
        raise RuntimeError("No SIM configured — add a number in the SIM Cards page first.")

    kwargs: dict = {
        "to": to_sip_uri,
        "from_": caller_id,
        "status_callback": settings.webhook_url("/twilio/voice/status"),
        "status_callback_event": ["initiated", "ringing", "answered", "completed"],
        "status_callback_method": "POST",
        "timeout": timeout or settings.incoming_ring_timeout,
    }
    if voice_url:
        kwargs["url"] = voice_url
        kwargs["method"] = voice_method
    else:
        kwargs["twiml"] = str(VoiceResponse().say(
            "You are connected to Sky Voice AI.", voice="alice",
        ))
    if settings.record_calls:
        kwargs["record"] = True
        kwargs["recording_status_callback"] = settings.webhook_url(_RECORDING_PATH)
        kwargs["recording_status_callback_event"] = ["completed"]

    return get_client(tenant_id).calls.create(**kwargs).sid
