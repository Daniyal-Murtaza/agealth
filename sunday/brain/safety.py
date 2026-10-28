"""Safety: things the call must notice and act on, no matter what the model does.

Two layers, both run on every parent utterance:

1. **Keyword triggers** (code, instant, multilingual). A parent saying "I fell"
   or "chest pain" in English, Urdu, Hindi or Punjabi raises a signal before
   any model is consulted. Cheap, deterministic, and it works even if the
   model is down.
2. **Model classification** (optional, slower) for things keywords miss:
   confusion, hopelessness, a scam in progress described indirectly.

Signals carry a tier:

* ``now``   - family is told immediately; the call stays on the line and
               keeps the parent calm; emergency script applies.
* ``today`` - family should check in today.
* ``info``  - noted in the summary.

The call engine must never downgrade a code-detected ``now`` signal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

Tier = Literal["info", "today", "now"]


@dataclass(frozen=True)
class Signal:
    kind: str
    tier: Tier
    evidence: str
    source: str = "keyword"  # keyword | model


# Patterns are matched case-insensitively against the transcript text in whatever
# language it arrives (Latin or native script). Keep them short and concrete.
_NOW: dict[str, list[str]] = {
    "fall": [
        r"\bme ca[ií]\b",
        r"\bme he ca[ií]do\b",
        r"\bestoy en el (piso|suelo)\b",
        r"\bno puedo levantarme\b",
        r"\bi fell\b",
        r"\bfallen\b",
        r"\bfell down\b",
        r"\bon the floor\b",
        r"\bcan'?t get up\b",
        r"\bgir ?gay[ia]\b",
        r"\bgir gai\b",
        r"\bgira\b",
        r"گر گ",
        r"गिर ग",
        r"ਡਿੱਗ",
    ],
    "chest_pain": [
        r"\bdolor (en|de) (el )?pecho\b",
        r"\bchest pain\b",
        r"\bpain in (my )?chest\b",
        r"\bheart attack\b",
        r"\bseene? me(in)? dard\b",
        r"سینے میں درد",
        r"सीने में दर्द",
        r"\bdil ka dard\b",
    ],
    "breathing": [
        r"\bno puedo respirar\b",
        r"\bme falta (el )?aire\b",
        r"\bcan'?t breathe\b",
        r"\bshort of breath\b",
        r"\bsaans nahi\b",
        r"سانس نہیں",
        r"सांस नहीं",
    ],
    "stroke": [
        r"\bface (is )?droop",
        r"\bcan'?t move my (arm|leg|face)\b",
        r"\bslurr",
        r"\bnumb\b",
    ],
    "bleeding": [
        r"\bsangrando\b",
        r"\bno para de sangrar\b",
        r"\bbleeding\b",
        r"\bblood (won'?t|will not) stop\b",
        r"\bkhoon\b",
        r"خون",
        r"खून",
    ],
    "emergency": [
        r"\bay[úu]d[ea]me\b",
        r"\bllam[ea]n? (a )?(una )?ambulancia\b",
        r"\bemergencia\b",
        r"\bcall (an )?ambulance\b",
        r"\bcall 911\b",
        r"\bcall 999\b",
        r"\bcall 1122\b",
        r"\bemergency\b",
        r"\bhelp me\b",
        r"\bmadad\b",
        r"مدد",
        r"मदद",
    ],
    "scam_live": [
        r"\b(gave|given|told) (him|her|them) (my|the) (card|pin|password|otp|code)\b",
        r"\bthey (are|were) asking for (my )?(bank|card|pin|otp)\b",
        r"\bsomeone (called|phoned|rang) (me )?(from|saying)\b.{0,40}\b(bank|tax|police|prize)\b",
    ],
}

_TODAY: dict[str, list[str]] = {
    "pain": [
        r"\bmucho dolor\b",
        r"\bme duele much[oí]simo\b",
        r"\b(bad|severe|terrible|a lot of) pain\b",
        r"\bhurts? (a lot|badly|so much)\b",
        r"\bbohot dard\b",
        r"\bbahut dard\b",
        r"بہت درد",
        r"बहुत दर्द",
    ],
    "unwell": [
        r"\bfiebre\b",
        r"\bmareo\b",
        r"\bmareada?\b",
        r"\bfever\b",
        r"\bvomit",
        r"\bdizzy\b",
        r"\bfaint",
        r"\bbukhar\b",
        r"بخار",
        r"बुखार",
        r"\bchakkar\b",
    ],
    "confusion": [
        r"\bwhat day is it\b",
        r"\bwhere am i\b",
        r"\bwho are you again\b",
        r"\bi don'?t remember (if|whether) i\b",
    ],
    "low_mood": [
        r"\bmuy sola?\b",
        r"\bnadie me llama\b",
        r"\bno point\b",
        r"\bdon'?t want to (live|be here|go on)\b",
        r"\bwhat'?s the use\b",
        r"\bnobody (cares|calls|comes)\b",
        r"\bso lonely\b",
        r"\bakela\b",
        r"\bakeli\b",
        r"اکیل",
        r"अकेल",
    ],
    "medication_concern": [
        r"\b(ran out|run out) of (my )?(tablets|pills|medicine)\b",
        r"\bdouble dose\b",
        r"\btook (them|it) twice\b",
        r"\bdawai? (khatam|nahi)\b",
        r"دوا ختم",
        r"दवा ख़त्म",
    ],
    "scam": [
        r"\bprize\b",
        r"\blottery\b",
        r"\bgift card\b",
        r"\bwire (the )?money\b",
        r"\bbank called\b",
        r"\bsuspicious call\b",
        r"\bsomeone wants money\b",
    ],
    "no_food": [
        r"\bno he comido\b",
        r"\bno hay comida\b",
        r"\bhaven'?t eaten\b",
        r"\bno food\b",
        r"\bnothing to eat\b",
        r"\bkhana nahi\b",
        r"کھانا نہیں",
        r"खाना नहीं",
    ],
}

_COMPILED_NOW = {k: [re.compile(p, re.IGNORECASE) for p in v] for k, v in _NOW.items()}
_COMPILED_TODAY = {k: [re.compile(p, re.IGNORECASE) for p in v] for k, v in _TODAY.items()}

# ``today`` kinds that are not a worry when the parent says it is about to be dealt with.
# "Haven't eaten yet, I'll make something later" at nine in the morning is breakfast, not
# an alert. "Haven't eaten since yesterday" still is.
_SOFTENERS: dict[str, re.Pattern[str]] = {
    "no_food": re.compile(
        r"\b(yet|later|about to|going to|will (make|eat|have|cook)|i'?ll (make|eat|have|cook)|"
        r"abhi|baad mein|thodi der)\b",
        re.IGNORECASE,
    ),
}


def keyword_signals(text: str) -> list[Signal]:
    """Deterministic triggers. Highest tier first; one signal per kind."""
    out: list[Signal] = []
    for kind, patterns in _COMPILED_NOW.items():
        for p in patterns:
            m = p.search(text)
            if m:
                out.append(Signal(kind=kind, tier="now", evidence=text.strip()))
                break
    negated = looks_negated(text)
    for kind, patterns in _COMPILED_TODAY.items():
        if negated and kind in ("pain", "unwell"):
            continue
        soft = _SOFTENERS.get(kind)
        if soft is not None and soft.search(text):
            continue
        for p in patterns:
            m = p.search(text)
            if m:
                out.append(Signal(kind=kind, tier="today", evidence=text.strip()))
                break
    return out


def looks_negated(text: str) -> bool:
    """ "No pain today", "didn't have any fever": the ``today`` triggers for pain and illness
    should stay quiet. Deliberately narrow; a ``now`` signal is never softened."""
    return bool(
        re.search(
            r"\b(no|not|never|didn'?t|haven'?t|without|hardly any)\s+(any\s+|much\s+)?"
            r"(pain|fever|dizz|vomit|bukhar|dard)",
            text,
            re.IGNORECASE,
        )
    )


@dataclass
class SafetyState:
    """Accumulates signals over a call so the engine can decide what to do."""

    signals: list[Signal] = field(default_factory=list)

    def add(self, new: list[Signal]) -> list[Signal]:
        fresh = []
        seen = {(s.kind, s.tier) for s in self.signals}
        for s in new:
            if (s.kind, s.tier) not in seen:
                self.signals.append(s)
                seen.add((s.kind, s.tier))
                fresh.append(s)
        return fresh

    @property
    def highest(self) -> Tier | None:
        order = {"info": 0, "today": 1, "now": 2}
        if not self.signals:
            return None
        return max((s.tier for s in self.signals), key=lambda t: order[t])

    def has_now(self) -> bool:
        return any(s.tier == "now" for s in self.signals)


# Script fragments the engine uses when a NOW signal fires. Plain, slow, repeated.
EMERGENCY_LINES = {
    "en": [
        "I'm here with you. Stay where you are, don't try to get up alone.",
        "I'm letting {family} know right now so someone can come.",
        "Is there a phone or a neighbour you can reach? If you can, please call {emergency} too.",
        "I'll stay on the line with you. Take slow breaths.",
    ],
    "ur": [
        "میں آپ کے ساتھ ہوں۔ جہاں ہیں وہیں رہیں، اکیلے اٹھنے کی کوشش نہ کریں۔",
        "میں ابھی {family} کو بتا رہی ہوں تاکہ کوئی آ جائے۔",
        "اگر ہو سکے تو {emergency} پر بھی فون کریں یا کسی پڑوسی کو بلائیں۔",
        "میں لائن پر آپ کے ساتھ رہوں گی۔ آہستہ سانس لیں۔",
    ],
    "hi": [
        "मैं आपके साथ हूँ। जहाँ हैं वहीं रहें, अकेले उठने की कोशिश न करें।",
        "मैं अभी {family} को बता रही हूँ ताकि कोई आ जाए।",
        "अगर हो सके तो {emergency} पर भी फ़ोन करें या किसी पड़ोसी को बुलाएँ।",
        "मैं लाइन पर आपके साथ रहूँगी। धीरे-धीरे साँस लें।",
    ],
    "es": [
        "Estoy aquí con usted. Quédese donde está, no intente levantarse sola.",
        "Ahora mismo le estoy avisando a {family} para que alguien venga.",
        "¿Hay un teléfono o un vecino cerca? Si puede, llame también al {emergency}.",
        "Me quedo en la línea con usted. Respire despacio.",
    ],
}

# Sunday serves the United States today. The table is kept so a future service area is a
# one-line change, and so a mis-filed number never hears the wrong emergency number.
EMERGENCY_NUMBERS = {"US": "911", "CA": "911", "GB": "999", "PK": "1122", "IN": "112", "AU": "000"}


def country_from_phone(phone: str | None) -> str | None:
    """ISO region for a +E.164 number, using the full number (Canada shares +1)."""
    if not phone:
        return None
    from sunday.geo import region_of

    return region_of(phone)


def emergency_number_for(phone: str | None, *, default: str = "911") -> str:
    return EMERGENCY_NUMBERS.get(country_from_phone(phone) or "", default)
