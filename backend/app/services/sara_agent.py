"""
Sara — K&K Legal AI receptionist
================================
Two-layer brain:

  1.  Deterministic **state machine** drives the qualification script
      verbatim (greet → membership check → plan check → field
      collection → wrap up). Because every prompt is hard-coded, this
      layer has 100 % accuracy by construction — no LLM hallucination
      risk.

  2.  LLM **fallback** kicks in when the caller asks an off-script
      question ("what does K&K Legal do?", "where are you located?").
      The relevant chunks from the knowledge book (BM25 RAG) are
      stuffed into the prompt so answers stay grounded.

The state machine exposes a single :meth:`on_user_speech` method that
returns the text Sara should speak next, plus whether the conversation
is finished. The Twilio Media Streams bridge calls this on every final
transcript from Deepgram STT.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
#  Conversation states (the verbatim K&K Legal script)
# --------------------------------------------------------------------------- #

class State(str, Enum):
    GREET = "greet"
    ASK_PURPOSE = "ask_purpose"
    ASK_MEMBER = "ask_member"
    # Non-member (regular client) path
    NM_NAME = "nm_name"
    NM_EMAIL = "nm_email"
    NM_ASK_QUERIES = "nm_ask_queries"
    NM_ANSWER_QUERIES = "nm_answer_queries"
    # Member path
    ASK_PLAN = "ask_plan"
    # MetLife branch
    MET_ELIG_ID = "met_elig_id"
    MET_ELIG_CONFIRM = "met_elig_confirm"
    MET_SERVICE = "met_service"
    MET_EMAIL = "met_email"
    MET_FULLNAME = "met_fullname"
    MET_ZIP = "met_zip"
    # ARAG branch
    AR_FULLNAME = "ar_fullname"
    AR_CASE_ID = "ar_case_id"
    AR_CASE_NUM = "ar_case_num"
    AR_EMAIL = "ar_email"
    AR_ZIP = "ar_zip"
    AR_SERVICE = "ar_service"
    AR_PHONE = "ar_phone"
    # Generic read-back confirmation: every collected field is read back to
    # the caller for a yes/no check before it is stored and we advance.
    CONFIRM_FIELD = "confirm_field"
    # Terminal
    WRAP_UP = "wrap_up"
    DONE = "done"


# Lines Sara speaks at each step. Keep them concise — every extra
# sentence is 1-2 s of latency before the caller can respond.
PROMPTS = {
    State.GREET: (
        "Hi, Thanks for your Call, you've reached K and K Legal Associates. "
        "This is the Admin assistant. How can I help you? "
        "Are you calling as a legal plan member or as a regular client? "
        "Please answer after the beep for the best service."
    ),
    State.ASK_PURPOSE: (
        "Could you briefly tell me what you're calling about?"
    ),
    State.ASK_MEMBER: (
        "Are you calling as a legal plan member or as a regular client?"
    ),
    State.NM_NAME: (
        "Could you please share your full name?"
    ),
    State.NM_EMAIL: (
        "And your email address for correspondence?"
    ),
    State.NM_ASK_QUERIES: (
        "Do you have any questions or queries I can help you with?"
    ),
    State.NM_ANSWER_QUERIES: (
        "Please go ahead and ask your question."
    ),
    State.ASK_PLAN: (
        "Great. Is your legal plan with MetLife or ARAG?"
    ),
    State.MET_ELIG_ID: (
        "Could you share your nine-character MetLife Eligibility I D? "
        "It's a mix of letters and numbers, exactly nine characters long."
    ),
    State.MET_SERVICE: (
        "Which legal service are you interested in?"
    ),
    State.MET_EMAIL: (
        "What email address should we use to reach you?"
    ),
    State.MET_FULLNAME: (
        "May I have the plan member's full legal name?"
    ),
    State.MET_ZIP: (
        "And the plan member's five-digit ZIP code?"
    ),
    State.AR_FULLNAME: (
        "Could you share the plan member's full name?"
    ),
    State.AR_CASE_ID: (
        "What's your ARAG Case I D?"
    ),
    State.AR_CASE_NUM: (
        "And the case number?"
    ),
    State.AR_EMAIL: (
        "Email address, please?"
    ),
    State.AR_ZIP: (
        "Five-digit ZIP code, please?"
    ),
    State.AR_SERVICE: (
        "Which legal service are you interested in?"
    ),
    State.AR_PHONE: (
        "And lastly, the best phone number to reach you on?"
    ),
    State.WRAP_UP: (
        "Thank you. We'll verify your membership and a K and K Legal "
        "team member will get back to you shortly. Have a great day!"
    ),
}


# The intent of each "collection" state — used both for prompting and
# for tagging fields in the call summary email.
FIELD_LABELS = {
    State.NM_NAME: "Full legal name",
    State.NM_EMAIL: "Email",
    State.MET_ELIG_ID: "MetLife Eligibility ID",
    State.MET_SERVICE: "Legal service",
    State.MET_EMAIL: "Email",
    State.MET_FULLNAME: "Member full name",
    State.MET_ZIP: "ZIP",
    State.AR_FULLNAME: "Member full name",
    State.AR_CASE_ID: "ARAG Case ID",
    State.AR_CASE_NUM: "Case number",
    State.AR_EMAIL: "Email",
    State.AR_ZIP: "ZIP",
    State.AR_SERVICE: "Legal service",
    State.AR_PHONE: "Phone",
}


def _strip_punct(text: str) -> str:
    return re.sub(r"[^\w\s']", " ", text or "")


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", _strip_punct(text).strip().lower())


def _classify_yes_no(text: str) -> Optional[bool]:
    t = _normalise(text)
    if not t:
        return None
    tokens = t.split()
    head = set(tokens[:6])  # consider only the first 6 words
    # Strong negatives win over a leading 'yes' chitchat ("yes but no")
    for tok in ("no", "nope", "negative"):
        if tok in head:
            # but not when 'no' is part of "i'm a no member" — keep the
            # simple word-level test, false positives here are
            # acceptable because we re-prompt anyway.
            return False
    if any(t.startswith(p) for p in ("i am not", "i'm not", "not a", "not yet")):
        return False
    if any(tok in head for tok in ("yes", "yeah", "yep", "yup", "correct",
                                    "sure", "absolutely", "right",
                                    "member", "plan")):
        return True
    if t.startswith("i am") or t.startswith("i'm"):
        return True
    return None


def _classify_plan(text: str) -> Optional[str]:
    t = _normalise(text)
    if "metlife" in t or "met life" in t or t == "met":
        return "metlife"
    if "arag" in t or "a rag" in t:
        return "arag"
    return None


def _looks_like_question(text: str) -> bool:
    """Heuristic: caller is asking Sara something rather than answering
    her current question. Used to route to the LLM-backed Q&A path
    without breaking the qualification flow. We deliberately ignore
    short conversational openers like 'can you hear me' / 'hello' so
    they don't hijack the script.
    """
    t = _normalise(text)
    if not t:
        return False
    # Conversational filler — never escalate to the LLM.
    FILLER = ("hello", "hi", "can you hear me", "are you there",
              "is anyone there", "helloo", "are you on the line")
    for f in FILLER:
        if t == f or t.startswith(f + " ") or t.endswith(" " + f):
            return False
    if any(t.startswith(s) for s in (
        "what ", "where ", "when ", "who ", "why ", "how ",
        "tell me ", "explain ",
    )):
        return True
    return False


def _is_repeat_request(text: str) -> bool:
    """Caller wants the current question repeated. Triggered by phrases
    like 'can you repeat that', 'say again', 'what was that', 'I didn't
    catch you', etc. Returns True for any of those; False for everything
    else (so genuine yes/no answers are unaffected).
    """
    t = " " + _normalise(text) + " "
    if not t.strip():
        return False
    PHRASES = (
        " repeat ", " say again ", " say that again ", " come again ",
        " once more ", " one more time ", " pardon ",
        " what was that ", " what did you say ",
        " didnt catch ", " didn t catch ",
        " didnt get that ", " didn t get that ",
        " i didnt hear ", " i didn t hear ",
        " could you repeat ", " can you repeat ",
        " could you say that again ", " can you say that again ",
    )
    return any(p in t for p in PHRASES)


# --------------------------------------------------------------------------- #
#  Field validators
# --------------------------------------------------------------------------- #
#
# When the caller speaks an ID/ZIP/email, Deepgram sometimes hands us a
# whole sentence ("my eligibility id is dzea3ici4 thanks"). Every
# validator below first tries to *extract* the relevant token from the
# raw transcript, then enforces the format. On success it returns
# (cleaned_value, None); on failure it returns (text, retry_prompt) so
# the agent can re-ask the same question without advancing state.

# Words to strip when we're trying to read a digit/letter sequence as a
# spoken ID. Deepgram often writes numbers as words ("two five eight").
_NUMBER_WORDS = {
    "zero": "0", "oh": "0", "o": "0",
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9",
}


def _spoken_to_compact(text: str) -> str:
    """Turn 'd z e a 3 i c i 4' or 'two five eight zero six' into a
    contiguous alphanumeric run so regexes can match it. Single-letter
    or number-word tokens are joined into one block; multi-character
    word tokens are kept as separate words so phrases like 'my id is
    DZEA3ICI4' don't fuse into 'myidisdzea3ici4'.
    """
    t = (text or "").lower()
    pieces: list[str] = []
    buf: list[str] = []  # collect short tokens that form a single ID

    def _flush():
        if buf:
            pieces.append("".join(buf))
            buf.clear()

    for tok in re.findall(r"[a-z0-9']+", t):
        tok = tok.replace("'", "")
        if not tok:
            continue
        if tok in _NUMBER_WORDS:
            buf.append(_NUMBER_WORDS[tok])
            continue
        if len(tok) == 1:
            buf.append(tok)
            continue
        # Multi-character word/number → end the current ID block and
        # keep the word as its own piece.
        _flush()
        pieces.append(tok)
    _flush()
    return " ".join(pieces)


def _extract_metlife_eligibility_id(text: str) -> Optional[str]:
    """Find a 9-character alphanumeric ID with at least one digit and
    one letter (rejects '123456789' typed-on-keypad or pure-prose
    'application'). Returns the uppercased ID, or None if nothing
    plausibly matches.
    """
    compact = _spoken_to_compact(text)
    candidates = re.findall(r"(?<![a-z0-9])([a-z0-9]{9})(?![a-z0-9])", compact)
    for c in candidates:
        has_digit = any(ch.isdigit() for ch in c)
        has_alpha = any(ch.isalpha() for ch in c)
        if has_digit and has_alpha:
            return c.upper()
    # Fallback: any 9-char run with mixed letters+digits, even if it's
    # part of a longer alnum string (rare, but defensive).
    for m in re.finditer(r"[a-z0-9]{9}", compact):
        c = m.group(0)
        has_digit = any(ch.isdigit() for ch in c)
        has_alpha = any(ch.isalpha() for ch in c)
        if has_digit and has_alpha:
            return c.upper()
    return None


def _extract_zip(text: str) -> Optional[str]:
    """Find a 5-digit ZIP. Tolerates ZIP+4 ('08536-1234') by taking the
    first 5 digits.
    """
    compact = _spoken_to_compact(text)
    m = re.search(r"(?<!\d)(\d{5})(?:-?\d{4})?(?!\d)", compact)
    if m:
        return m.group(1)
    return None


def _extract_email(text: str) -> Optional[str]:
    """Find an email address. STT often spells it out as 'name at domain
    dot com' — handle that too.
    """
    raw = (text or "").strip()
    # Direct hit on a literal email.
    m = re.search(r"[\w\.\-+]+@[\w\.\-]+\.[a-z]{2,}", raw, flags=re.IGNORECASE)
    if m:
        return m.group(0).lower()
    # Spoken form: "subhra at gmail dot com" -> "subhra@gmail.com"
    spoken = (raw or "").lower()
    spoken = re.sub(r"\s+at\s+", "@", spoken)
    spoken = re.sub(r"\s+dot\s+", ".", spoken)
    spoken = re.sub(r"\s+", "", spoken)
    m = re.search(r"[\w\.\-+]+@[\w\.\-]+\.[a-z]{2,}", spoken)
    if m:
        return m.group(0)
    return None


def _extract_phone(text: str) -> Optional[str]:
    """Pull out 10-15 contiguous digits as the phone number."""
    compact = _spoken_to_compact(text)
    digits = re.sub(r"\D", "", compact)
    if 10 <= len(digits) <= 15:
        return digits
    return None


def _validate_field(state: "State", raw: str) -> tuple[str, Optional[str]]:
    """Returns (cleaned_value, retry_prompt_or_None).

    retry_prompt None  → validation passed, store cleaned_value
    retry_prompt set   → ask the caller again with a corrective hint
    """
    text = (raw or "").strip()
    if not text:
        return text, "Sorry, I didn't catch that — could you repeat?"

    if state == State.MET_ELIG_ID:
        eid = _extract_metlife_eligibility_id(text)
        if eid:
            return eid, None
        return text, (
            "Sorry, I need exactly nine characters — a mix of letters "
            "and numbers, like A B C 1 2 3 4 5 6. Could you say your "
            "MetLife Eligibility I D again, one character at a time?"
        )

    if state in (State.MET_ZIP, State.AR_ZIP):
        z = _extract_zip(text)
        if z:
            return z, None
        return text, (
            "I need a five-digit ZIP code. Could you read the five "
            "digits again, please?"
        )

    if state in (State.NM_EMAIL, State.MET_EMAIL, State.AR_EMAIL):
        e = _extract_email(text)
        if e:
            return e, None
        return text, (
            "I didn't catch a valid email. Could you spell it out, "
            "for example 'name at gmail dot com'?"
        )

    if state == State.AR_PHONE:
        p = _extract_phone(text)
        if p:
            return p, None
        return text, (
            "Could you say the phone number with all the digits, "
            "including the area code?"
        )

    if state == State.AR_CASE_ID:
        # ARAG Case ID is alphanumeric, typically 6-12 chars; require
        # at least one digit to reject pure-prose answers.
        compact = _spoken_to_compact(text)
        m = re.search(r"[a-z0-9]{4,}", compact)
        if m and any(c.isdigit() for c in m.group(0)):
            return m.group(0).upper(), None
        return text, (
            "Could you read your ARAG Case I D, one character at a time?"
        )

    if state == State.AR_CASE_NUM:
        # Case number is digits-only.
        compact = _spoken_to_compact(text)
        digits = re.sub(r"\D", "", compact)
        if len(digits) >= 3:
            return digits, None
        return text, (
            "Could you read the case number digits, one at a time?"
        )

    # Free-text fields (names, company, legal service): accept as-is
    # but reject obvious garbage / very short answers.
    if state in (State.NM_NAME,
                 State.MET_FULLNAME, State.MET_SERVICE,
                 State.AR_FULLNAME, State.AR_SERVICE):
        if len(text) < 2:
            return text, "Sorry, I didn't catch that — could you say it again?"
        return text, None

    # Anything else: accept as-is.
    return text, None


# --------------------------------------------------------------------------- #
#  Spelled-code accumulation
# --------------------------------------------------------------------------- #
#
# Callers read out IDs / ZIPs / phone numbers a character (or a few) at a
# time, with natural pauses. Deepgram's endpointing finalises a separate
# transcript on every pause, so a single 9-character ID arrives as several
# fragments ("abc55", "00", "12"). Validating each fragment on its own can
# never satisfy a fixed-length rule — which is exactly why the old flow
# looped forever. For these states we instead ACCUMULATE the fragments and
# validate the running buffer, accepting as soon as it forms a complete
# value.

_CODE_STATES = {
    State.MET_ELIG_ID,
    State.MET_ZIP,
    State.AR_ZIP,
    State.AR_PHONE,
    State.AR_CASE_ID,
    State.AR_CASE_NUM,
}

# The most characters a field can plausibly hold. Once the buffer has this
# many usable characters but still won't validate, we treat it as garbled
# and re-ask clearly rather than waiting forever for more.
_CODE_FIELD_MAX = {
    State.MET_ELIG_ID: 9,
    State.MET_ZIP: 5,
    State.AR_ZIP: 5,
    State.AR_PHONE: 15,
    State.AR_CASE_ID: 14,
    State.AR_CASE_NUM: 16,
}

_DIGIT_ONLY_STATES = {
    State.MET_ZIP, State.AR_ZIP,
    State.AR_PHONE,
    State.AR_CASE_NUM,
}


def _code_char_count(state: "State", text: str) -> int:
    """How many usable characters the caller has supplied so far for a
    spelled code field — digits only for numeric fields, alphanumerics for
    the alphanumeric ones.
    """
    compact = _spoken_to_compact(text)
    if state in _DIGIT_ONLY_STATES:
        return len(re.sub(r"\D", "", compact))
    return len(re.sub(r"[^a-z0-9]", "", compact))


def _is_restart_request(text: str) -> bool:
    """Caller wants to re-spell from scratch ('start over', 'that's wrong',
    'scratch that').
    """
    t = " " + _normalise(text) + " "
    PHRASES = (
        " start over ", " start again ", " from the beginning ",
        " scratch that ", " let me start ", " restart ", " redo ",
        " thats wrong ", " that is wrong ", " no wait ",
    )
    return any(p in t for p in PHRASES)


def _spell_out(value: str) -> str:
    """Render an ID as individual characters so the TTS engine reads each
    one aloud: 'ABC5500AB' -> 'A, B, C, 5, 5, 0, 0, A, B'.
    """
    return ", ".join(list(value or ""))


# States whose value is an email address — read these back in spoken form
# ("name at gmail dot com") so the caller can verify what we captured.
_EMAIL_STATES = {State.NM_EMAIL, State.MET_EMAIL, State.AR_EMAIL}


def _readback_value(state: "State", value: str) -> str:
    """Turn a stored field value into a spoken read-back string. Spelled
    codes are read character-by-character, emails are de-symbolised, and
    free-text fields (names, company, service) are read as-is.
    """
    if state in _EMAIL_STATES:
        return (value or "").replace("@", " at ").replace(".", " dot ")
    if state in _CODE_STATES:
        return _spell_out(value)
    return value or ""


# --------------------------------------------------------------------------- #
#  Sara session
# --------------------------------------------------------------------------- #

@dataclass
class Turn:
    role: str       # "sara" | "caller"
    text: str
    ts: float = field(default_factory=time.time)


@dataclass
class SaraSession:
    call_sid: str
    from_number: str = ""
    to_number: str = ""
    tenant_id: str = "default"
    state: State = State.GREET
    plan: Optional[str] = None     # "metlife" | "arag" | None
    fields: dict = field(default_factory=dict)
    transcript: List[Turn] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    transferred_from_agent: Optional[str] = None
    initial_purpose: Optional[str] = None
    # Spelled-code accumulation state (see _collect_code). ``awaiting_more``
    # tells the bridge to keep the mic open and stay silent while the caller
    # finishes spelling an ID/ZIP/phone split across several STT fragments.
    awaiting_more: bool = False
    _code_buf: str = ""
    _code_buf_state: Optional[State] = None
    # Candidate MetLife Eligibility ID held during read-back confirmation.
    _pending_elig_id: str = ""
    # Generic field confirmation: the field state being confirmed and the
    # candidate value held until the caller says "yes" (see _begin_confirm).
    _pending_confirm_state: Optional[State] = None
    _pending_confirm_value: str = ""

    # ------------------------------------------------------------------ #
    def _say(self, text: str) -> str:
        # An empty string is the "stay silent / keep listening" signal used
        # during code accumulation — never record it as a spoken turn.
        if (text or "").strip():
            self.transcript.append(Turn("sara", text))
        return text or ""

    def _heard(self, text: str) -> None:
        if (text or "").strip():
            self.transcript.append(Turn("caller", text.strip()))

    # ------------------------------------------------------------------ #
    def greeting(self) -> str:
        """First line Sara speaks when the call connects."""
        self.state = State.ASK_MEMBER  # next user reply will be yes/no
        return self._say(PROMPTS[State.GREET])

    # ------------------------------------------------------------------ #
    def _confirm_elig_prompt(self, value: str) -> str:
        """Spoken read-back so the caller can confirm their MetLife ID."""
        spelled = _spell_out(value)
        return (
            "Let me make sure I have that right. Your MetLife Eligibility "
            f"I D is {spelled}. Is that correct?"
        )

    def _confirm_field_prompt(self, field_state: "State", value: str) -> str:
        """Generic spoken read-back: repeat a freshly collected field value
        back to the caller and ask them to confirm it.
        """
        spoken = _readback_value(field_state, value)
        return f"Just to confirm, I have {spoken}. Is that correct?"

    def _begin_confirm(self, field_state: "State", value: str) -> tuple[str, bool]:
        """Hold a freshly collected field value and switch to the read-back
        confirmation state. The value is only stored once the caller says
        "yes" (handled in ``_advance`` under ``State.CONFIRM_FIELD``).
        """
        self._pending_confirm_state = field_state
        self._pending_confirm_value = value
        self.awaiting_more = False
        self.state = State.CONFIRM_FIELD
        return self._confirm_field_prompt(field_state, value), False

    def _current_prompt(self) -> str:
        """The prompt to replay on a 'repeat that' request — handles the
        dynamic MetLife / field read-backs as well as the static prompts.
        """
        if self.state == State.MET_ELIG_CONFIRM and self._pending_elig_id:
            return self._confirm_elig_prompt(self._pending_elig_id)
        if self.state == State.CONFIRM_FIELD and self._pending_confirm_state is not None:
            return self._confirm_field_prompt(
                self._pending_confirm_state, self._pending_confirm_value,
            )
        return PROMPTS.get(self.state, "")

    def current_prompt(self) -> str:
        """Public accessor used by the bridge's no-response watchdog to decide
        which line to repeat when the caller stays silent.
        """
        return self._current_prompt()

    # ------------------------------------------------------------------ #
    def on_user_speech(self, raw_text: str) -> tuple[str, bool]:
        """Feed a final transcript from the caller and get Sara's reply.

        Returns ``(reply_text, done)`` — when ``done`` is true the
        bridge should play the reply then politely end the call.
        """
        self._heard(raw_text)
        # Default to "not waiting"; the code-accumulation path re-arms this
        # when it needs more spelled characters.
        self.awaiting_more = False
        text = (raw_text or "").strip()
        if not text:
            return self._say("Sorry, I didn't catch that — could you repeat?"), False

        # Caller asked us to repeat — replay the current prompt and
        # stay in the same state. This must run BEFORE the LLM Q&A
        # check so 'can you repeat that?' doesn't get answered as a
        # general question.
        if _is_repeat_request(text):
            current = self._current_prompt()
            logger.info(
                "🔁 [sara-agent] %s REPEAT-REQUEST state=%s",
                self.call_sid, self.state.name,
            )
            return self._say("Of course. " + current), False

        # Yes/no states are exempt from the LLM Q&A path. The caller
        # may include filler ("yes can you hear me") but we should
        # always classify and advance — never punt to the LLM, which
        # adds 2-6 s of latency and tends to drop the qualifier.
        bypass_qna = self.state in (
            State.ASK_MEMBER, State.ASK_PLAN, State.GREET,
            State.MET_ELIG_CONFIRM, State.CONFIRM_FIELD,
            State.WRAP_UP, State.DONE, State.NM_ASK_QUERIES,
            State.NM_ANSWER_QUERIES,
        )

        # Off-script question → answer from the knowledge book, then
        # return to the same state (we re-ask the current prompt so the
        # collection flow doesn't stall).
        if (not bypass_qna) and _looks_like_question(text):
            answer = self._answer_question(text)
            current_prompt = PROMPTS.get(self.state, "")
            combined = (answer + " " + current_prompt).strip()
            return self._say(combined), False

        # Normal qualification flow.
        try:
            reply, done = self._advance(text)
        except Exception:  # noqa: BLE001
            logger.exception("[%s] Sara state machine error", self.call_sid)
            reply, done = (
                "I'm sorry, I had a small hiccup. Let me transfer you to a "
                "human agent shortly. Goodbye for now.",
                True,
            )
            self.state = State.DONE
        if done:
            self.finished_at = time.time()
        return self._say(reply), done

    # ------------------------------------------------------------------ #
    def _advance(self, text: str) -> tuple[str, bool]:
        """The actual state machine."""
        if self.state == State.ASK_MEMBER:
            yn = _classify_yes_no(text)
            if yn is None:
                # Try to interpret as the initial purpose statement;
                # then re-ask the membership question.
                self.initial_purpose = text
                return PROMPTS[State.ASK_MEMBER], False
            if yn:
                self.state = State.ASK_PLAN
                return PROMPTS[State.ASK_PLAN], False
            # Non-member path
            self.state = State.NM_NAME
            return PROMPTS[State.NM_NAME], False

        if self.state == State.ASK_PLAN:
            plan = _classify_plan(text)
            if plan is None:
                return (
                    "Sorry, I didn't catch the plan name. "
                    "Is it MetLife or ARAG?"
                ), False
            self.plan = plan
            if plan == "metlife":
                self.state = State.MET_ELIG_ID
                return PROMPTS[State.MET_ELIG_ID], False
            self.state = State.AR_FULLNAME
            return PROMPTS[State.AR_FULLNAME], False

        if self.state == State.MET_ELIG_CONFIRM:
            yn = _classify_yes_no(text)
            if yn is True:
                self.fields[FIELD_LABELS[State.MET_ELIG_ID]] = self._pending_elig_id
                self._pending_elig_id = ""
                self.state = State.MET_ELIG_ID  # anchor for the chain lookup
                return self._next_after_field()
            if yn is False:
                # Wrong read-back — clear it and collect the ID again.
                self._pending_elig_id = ""
                self._code_buf = ""
                self._code_buf_state = None
                self.state = State.MET_ELIG_ID
                return (
                    "No problem, let's try again. " + PROMPTS[State.MET_ELIG_ID]
                ), False
            # Unclear yes/no — ask the confirmation once more.
            return self._confirm_elig_prompt(self._pending_elig_id), False

        # Generic field read-back confirmation. Every collected field lands
        # here (via _begin_confirm) so the caller can verify what we heard
        # before it is stored and we advance.
        if self.state == State.CONFIRM_FIELD:
            yn = _classify_yes_no(text)
            field_state = self._pending_confirm_state
            if yn is True and field_state is not None:
                self.fields[FIELD_LABELS[field_state]] = self._pending_confirm_value
                self._pending_confirm_state = None
                self._pending_confirm_value = ""
                self.state = field_state  # anchor for the chain lookup
                return self._next_after_field()
            if yn is False and field_state is not None:
                # Wrong read-back — discard the value and re-ask the field.
                self._pending_confirm_state = None
                self._pending_confirm_value = ""
                self._code_buf = ""
                self._code_buf_state = None
                self.state = field_state
                return (
                    "No problem, let's try again. "
                    + PROMPTS.get(field_state, "Could you say that again?")
                ), False
            # Unclear yes/no — ask the confirmation once more.
            return self._confirm_field_prompt(
                field_state, self._pending_confirm_value,
            ), False

        # Regular client: asked if they have queries
        if self.state == State.NM_ASK_QUERIES:
            yn = _classify_yes_no(text)
            if yn is True:
                # Caller has questions — ask them to state the question
                self.state = State.NM_ANSWER_QUERIES
                return PROMPTS[State.NM_ANSWER_QUERIES], False
            if yn is False:
                # No questions — wrap up
                self.state = State.DONE
                return PROMPTS[State.WRAP_UP], True
            # Unclear — re-ask
            return PROMPTS[State.NM_ASK_QUERIES], False

        # Regular client: answering their question
        if self.state == State.NM_ANSWER_QUERIES:
            # Answer using LLM, then wrap up
            answer = self._answer_question(text)
            self.state = State.DONE
            return self._say(answer + " " + PROMPTS[State.WRAP_UP]), True

        # Field-collection states — collect the answer, then read it back for
        # confirmation before advancing.
        if self.state in FIELD_LABELS:
            # Spelled codes (ID/ZIP/phone/case) are accumulated across the
            # several STT fragments Deepgram emits while the caller spells.
            if self.state in _CODE_STATES:
                return self._collect_code(text)
            cleaned, retry_msg = _validate_field(self.state, text)
            if retry_msg is not None:
                # Validation failed — stay in the same state and ask
                # again with a clearer hint so the caller knows what
                # format we need.
                logger.info(
                    "✨ [sara-agent] %s VALIDATION-FAIL state=%s raw=%r",
                    self.call_sid, self.state.name, text[:80],
                )
                return retry_msg, False
            # Read the value back and wait for a yes/no before storing it.
            return self._begin_confirm(self.state, cleaned)

        # Default: wrap up.
        self.state = State.DONE
        return PROMPTS[State.WRAP_UP], True

    # ------------------------------------------------------------------ #
    def _next_after_field(self) -> tuple[str, bool]:
        nm_chain = [State.NM_NAME, State.NM_EMAIL, State.NM_ASK_QUERIES]
        met_chain = [State.MET_ELIG_ID, State.MET_SERVICE, State.MET_EMAIL,
                     State.MET_FULLNAME, State.MET_ZIP]
        ar_chain = [State.AR_FULLNAME, State.AR_CASE_ID, State.AR_CASE_NUM,
                    State.AR_EMAIL, State.AR_ZIP, State.AR_SERVICE, State.AR_PHONE]
        if self.state in nm_chain:
            chain = nm_chain
        elif self.state in met_chain:
            chain = met_chain
        elif self.state in ar_chain:
            chain = ar_chain
        else:
            chain = []
        if not chain:
            self.state = State.DONE
            return PROMPTS[State.WRAP_UP], True
        i = chain.index(self.state)
        if i + 1 < len(chain):
            self.state = chain[i + 1]
            return PROMPTS[self.state], False
        # End of the chain → wrap up.
        self.state = State.DONE
        return PROMPTS[State.WRAP_UP], True

    # ------------------------------------------------------------------ #
    def _collect_code(self, text: str) -> tuple[str, bool]:
        """Accumulate a spelled ID/ZIP/phone/case across multiple STT
        fragments and only advance once the running buffer forms a complete,
        valid value.

        Returns the usual ``(reply, done)`` pair. A reply of ``""`` means
        "stay silent and keep listening" — the bridge leaves the mic open and
        its watchdog nudges the caller if they go quiet.
        """
        # Fresh buffer whenever we land on a new collection state.
        if self._code_buf_state != self.state:
            self._code_buf = ""
            self._code_buf_state = self.state

        if _is_restart_request(text):
            self._code_buf = ""
            self.awaiting_more = False
            return "No problem — go ahead and start over.", False

        # Append this fragment to whatever we've gathered so far.
        self._code_buf = (self._code_buf + " " + text).strip()

        cleaned, retry_msg = _validate_field(self.state, self._code_buf)
        if retry_msg is None:
            logger.info(
                "\u2705 [sara-agent] %s CODE-COMPLETE state=%s value=%r",
                self.call_sid, self.state.name, cleaned,
            )
            self._code_buf = ""
            self._code_buf_state = None
            self.awaiting_more = False
            # MetLife Eligibility ID keeps its tailored read-back wording.
            if self.state == State.MET_ELIG_ID:
                self._pending_elig_id = cleaned
                self.state = State.MET_ELIG_CONFIRM
                return self._confirm_elig_prompt(cleaned), False
            # Every other spelled code (ZIP / phone / case) is read back for
            # confirmation before it is stored and we advance.
            return self._begin_confirm(self.state, cleaned)

        # Not a complete value yet.
        char_count = _code_char_count(self.state, self._code_buf)
        max_len = _CODE_FIELD_MAX.get(self.state, 9)
        if char_count >= max_len:
            # As many characters as the field can hold but still unparseable
            # → it's garbled. Reset and re-ask with the corrective hint.
            logger.info(
                "\u2728 [sara-agent] %s CODE-GARBLED state=%s buf=%r",
                self.call_sid, self.state.name, self._code_buf[:80],
            )
            self._code_buf = ""
            self.awaiting_more = False
            return retry_msg, False

        # Still mid-spell — Deepgram just split the utterance on a pause.
        # Stay silent so we don't talk over the caller's next characters.
        logger.info(
            "\u23f3 [sara-agent] %s CODE-PARTIAL state=%s chars=%d buf=%r",
            self.call_sid, self.state.name, char_count, self._code_buf[:80],
        )
        self.awaiting_more = True
        return "", False

    # ------------------------------------------------------------------ #
    def nudge_collection(self) -> Optional[str]:
        """Re-ask for a spelled code when the caller has gone quiet while we
        were still waiting for more characters. Called by the bridge's
        silence watchdog. Resets the buffer so the caller starts fresh.
        """
        self.awaiting_more = False
        if self.state not in _CODE_STATES:
            return None
        _, retry_msg = _validate_field(self.state, self._code_buf)
        self._code_buf = ""
        self._code_buf_state = self.state
        return self._say(retry_msg or "Sorry, could you say that again?")

    # ------------------------------------------------------------------ #
    def _answer_question(self, text: str) -> str:
        """Off-script Q&A grounded in the tenant's knowledge book."""
        # Local imports keep cold-start cheap.
        from app.services.rag import retrieve
        from app.services.tenant_context import get_tenant, sara_config

        cfg = sara_config(get_tenant(self.tenant_id))

        chunks = retrieve(
            text, k=4,
            pdf_path=cfg.knowledge_pdf or None,
            text=cfg.knowledge_text or None,
        )
        context = "\n\n---\n\n".join(chunks) if chunks else ""

        if not cfg.ai_api_key:
            # Without an LLM key we can't compose a free-form answer.
            # Give the caller a graceful canned reply rather than
            # silence, then re-ask the current qualification prompt.
            if chunks:
                # Best-effort: trim the first chunk to ~1 sentence.
                snippet = re.split(r"(?<=[\.\?!])\s+", chunks[0].strip())[0]
                if len(snippet) > 240:
                    snippet = snippet[:240].rsplit(" ", 1)[0] + "…"
                return snippet
            return (
                "I'll make a note of that and a team member will follow up "
                "with details."
            )

        firm = cfg.company_name or "the firm"
        try:
            from openai import OpenAI
            client = OpenAI(
                api_key=cfg.ai_api_key,
                base_url=cfg.ai_base_url or None,
            )
            system = (
                f"You are the front-desk assistant for {firm}. "
                "Answer the caller in one or two short, "
                "natural spoken sentences — no markdown, no bullet points. "
                "Do not introduce yourself by name; just speak on behalf of "
                "the firm. Use ONLY the context below; if the context "
                "doesn't cover the question, say so politely and offer to "
                "take a message. Speak in first person on behalf of the firm."
            )
            user = f"CONTEXT:\n{context}\n\nCALLER QUESTION:\n{text}"
            resp = client.chat.completions.create(
                model=cfg.ai_model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.3,
                max_tokens=140,
                timeout=6,
            )
            answer = (resp.choices[0].message.content or "").strip()
            return answer or "Let me get a team member to follow up on that."
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] Sara LLM call failed: %s", self.call_sid, exc)
            return "I'll make a note and have a team member follow up."

    # ------------------------------------------------------------------ #
    def force_wrap_up(self) -> str:
        self.state = State.DONE
        self.finished_at = time.time()
        return self._say(PROMPTS[State.WRAP_UP])

    def transcript_text(self) -> str:
        lines = []
        for t in self.transcript:
            who = "Sara" if t.role == "sara" else "Caller"
            lines.append(f"{who}: {t.text}")
        return "\n".join(lines)

    def summary_email_body(self) -> str:
        rows = []
        rows.append(f"Call SID: {self.call_sid}")
        rows.append(f"From: {self.from_number or 'unknown'}")
        rows.append(f"To:   {self.to_number or 'unknown'}")
        rows.append(f"Duration: {int((self.finished_at or time.time()) - self.started_at)} s")
        if self.transferred_from_agent:
            rows.append(f"Transferred from: {self.transferred_from_agent}")
        rows.append("")
        rows.append("== Caller details collected ==")
        if self.plan:
            rows.append(f"Plan: {self.plan.upper()}")
        if self.initial_purpose:
            rows.append(f"Stated purpose: {self.initial_purpose}")
        if self.fields:
            for k, v in self.fields.items():
                rows.append(f"  • {k}: {v}")
        else:
            rows.append("  (none collected)")
        rows.append("")
        rows.append("== Full transcript ==")
        rows.append(self.transcript_text())
        return "\n".join(rows)
