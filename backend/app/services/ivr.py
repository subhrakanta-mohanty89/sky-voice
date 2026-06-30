"""IVR (Interactive Voice Response) — welcome menu for inbound calls.

When a customer dials our support number, we play a short greeting +
menu (e.g. *"Press 1 for new consultation, press 2 for…"*) before any
agent ringing happens. The selected option is persisted on the
:class:`Call` so the operator UI can show the reason **before** they pick
the call up.

The actual routing logic (eligible agents → queue → fallback) lives in
:mod:`app.services.routing`. This module is responsible only for the
TwiML that drives the menu + the digit → service mapping.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from twilio.twiml.voice_response import Gather, VoiceResponse

from config import settings


# --------------------------------------------------------------------------- #
#  Menu definition
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class IVROption:
    digit: str
    code: str
    label: str
    prompt: str  # short label spoken in the menu, e.g. "Press 1 for…"


# Single source of truth — change here to update both the TTS prompt and
# the badge that lights up in the operator UI. ``digit`` must be unique.
IVR_OPTIONS: List[IVROption] = [
    IVROption(
        digit="1",
        code="new-consultation",
        label="New Legal Consultation",
        prompt="Press 1 for a new legal consultation",
    ),
    IVROption(
        digit="2",
        code="corporate",
        label="Corporate & Business Legal Services",
        prompt="Press 2 for corporate and business legal services",
    ),
    IVROption(
        digit="3",
        code="ip",
        label="Intellectual Property Services",
        prompt="Press 3 for intellectual property services",
    ),
    IVROption(
        digit="4",
        code="litigation",
        label="Litigation & Dispute Resolution",
        prompt="Press 4 for litigation and dispute resolution",
    ),
    IVROption(
        digit="5",
        code="existing-client",
        label="Existing Client Support & Case Status",
        prompt="Press 5 for existing client support or case status",
    ),
]

# Special re-play digit. Twilio "Gather" treats any unmatched digit as
# "no input and try again", so we just have to make sure the menu plays
# again when this comes in (the handler does that explicitly).
IVR_REPEAT_DIGITS = {"9", "0", "*"}

_OPTIONS_BY_DIGIT: Dict[str, IVROption] = {opt.digit: opt for opt in IVR_OPTIONS}


def lookup(digit: str) -> Optional[IVROption]:
    return _OPTIONS_BY_DIGIT.get((digit or "").strip())


def options_public() -> List[Dict]:
    """Public dict shape used by API consumers (admin UI dropdowns etc.)."""
    return [
        {"digit": o.digit, "code": o.code, "label": o.label}
        for o in IVR_OPTIONS
    ]


# --------------------------------------------------------------------------- #
#  TwiML
# --------------------------------------------------------------------------- #

def _menu_text(*, include_welcome: bool, repeat: int) -> str:
    """Build the spoken script for the menu.

    On the first round we include the company welcome. On repeats we
    skip it to keep the loop tight. We always trail with the
    "press 9 to listen again" hint so the caller knows it's possible.
    """
    parts: List[str] = []
    if include_welcome and repeat == 0:
        parts.append(
            f"Welcome to {settings.ivr_company_name}. "
            "Please listen to the following options."
        )
    elif repeat > 0:
        parts.append("Let's try again.")
    for opt in IVR_OPTIONS:
        parts.append(opt.prompt + ".")
    parts.append("Press 9 to listen to the options again.")
    return " ".join(parts)


def twiml_welcome_menu(*, repeat: int = 0) -> str:
    """TwiML that plays the welcome + menu and gathers a single digit.

    ``repeat`` is the 0-based count of how many times we've already
    played the menu without getting a valid digit. The handler bumps it
    on each invalid attempt and stops re-playing once
    ``settings.ivr_max_repeats`` is reached.
    """
    response = VoiceResponse()

    if repeat >= settings.ivr_max_repeats:
        # Fall through — caller didn't pick anything; route them as if
        # they'd selected the "general" path. The handler builds this
        # branch directly, but we keep this guard so a stale TwiML
        # session can't loop forever.
        response.redirect(
            settings.webhook_url("/twilio/voice/ivr/timeout"),
            method="POST",
        )
        return str(response)

    gather = Gather(
        input="dtmf",
        num_digits=1,
        action=settings.webhook_url(f"/twilio/voice/ivr/handle?repeat={repeat}"),
        method="POST",
        timeout=settings.ivr_gather_timeout,
        # finishOnKey defaults to '#' which is fine; we only want a
        # single digit anyway.
    )
    gather.say(
        _menu_text(include_welcome=True, repeat=repeat),
        voice=settings.ivr_voice,
        language=settings.ivr_language,
    )
    response.append(gather)

    # If <Gather> times out, re-prompt by redirecting back to the menu
    # with an incremented repeat counter.
    response.redirect(
        settings.webhook_url(f"/twilio/voice/ivr/menu?repeat={repeat + 1}"),
        method="POST",
    )
    return str(response)


def twiml_invalid_then_repeat(*, repeat: int) -> str:
    """Play a short 'invalid choice' message, then loop back to the menu."""
    response = VoiceResponse()
    response.say(
        "Sorry, I didn't catch that.",
        voice=settings.ivr_voice,
        language=settings.ivr_language,
    )
    response.redirect(
        settings.webhook_url(f"/twilio/voice/ivr/menu?repeat={repeat}"),
        method="POST",
    )
    return str(response)


def twiml_selection_confirm(option: IVROption) -> str:
    """Brief confirmation before transferring to an agent.

    Kept short on purpose — extra dead air just makes the call feel slow.
    """
    response = VoiceResponse()
    response.say(
        f"Connecting you to {option.label}.",
        voice=settings.ivr_voice,
        language=settings.ivr_language,
    )
    response.redirect(
        settings.webhook_url(f"/twilio/voice/ivr/route?code={option.code}"),
        method="POST",
    )
    return str(response)
