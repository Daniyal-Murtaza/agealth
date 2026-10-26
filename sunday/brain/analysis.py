"""After the call: what did we learn, and who needs to know?

One structured model call turns the transcript into:

* a two-to-four line **summary** written for the family
* **observations** - typed facts with the parent's words as evidence
* **memories** - durable things worth remembering for future calls
* **follow-ups** - what tomorrow's call should ask about
* **alerts** proposed by the model, which code then merges with the safety
  signals detected during the call; code-detected ``now`` signals are never
  downgraded by the model's opinion

Everything cites the parent's words. Nothing is reported that was not said.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from sunday.brain.model import ChatModel, Message, ModelError, complete_json
from sunday.brain.safety import Signal
from sunday.logging_setup import get_logger
from sunday.models import Parent

log = get_logger("brain.analysis")


class ObservationOut(BaseModel):
    kind: Literal[
        "medication",
        "sleep",
        "meal",
        "mood",
        "pain",
        "mobility",
        "memory",
        "social",
        "safety",
        "household",
        "plan",
        "story",
        "other",
    ]
    text: str = Field(description="Short third-person note, e.g. 'took blood pressure tablet'")
    value: str = Field("", description="taken|missed|unclear, poor|ok|good, or a short value")
    evidence: str = Field(description="The parent's own words that support this (exact).")
    follow_up: bool = Field(False, description="Should tomorrow's call ask about this?")


class AlertOut(BaseModel):
    tier: Literal["info", "today", "now"]
    kind: str
    text: str = Field(description="One sentence for the family.")
    evidence: str


class MemoryOut(BaseModel):
    text: str = Field(description="A durable fact about the parent, third person.")
    topic: str = Field("", description="family|health|home|likes|social|routine")
    evidence: str


class MessageReply(BaseModel):
    message_id: str
    response: str = Field(description="What the parent said in reply, briefly.")


class CallAnalysis(BaseModel):
    summary: str = Field(description="2-4 short lines for the family. Plain, warm, factual.")
    mood: Literal["bright", "ok", "low", "unclear"] = "unclear"
    observations: list[ObservationOut] = Field(default_factory=list)
    alerts: list[AlertOut] = Field(default_factory=list)
    memories: list[MemoryOut] = Field(default_factory=list)
    message_replies: list[MessageReply] = Field(default_factory=list)


ANALYSIS_SYSTEM = """\
You read the transcript of a daily check-in call between Sunday (an assistant) and {name}, an
older person, and write notes for their family. Rules:
- The summary is for a worried adult child reading on their phone: 2-4 short lines. Lead with
  how they seemed, then medication, then anything the family should know. No filler.
- Every observation, alert and memory must quote the parent's exact words in `evidence`.
  If they did not say it, do not write it.
- Medication: record taken / missed / unclear for each medication asked about.
- Alerts: `now` only for emergencies (fall, chest pain, breathing, bleeding, scam in progress).
  `today` for pain, illness, confusion, low mood, missed medication, scam attempt, no food.
  `info` for small things worth a mention. No alert for ordinary days.
- Memories: durable facts worth knowing on future calls (names, routines, preferences, health
  background). Not today's weather.
- follow_up=true for things tomorrow's call should ask about ("did the plumber come").
- For each family message id listed, record what the parent replied, if anything.
Medications asked about: {meds}
Family messages delivered this call: {messages}
"""


def analyse_call(
    model: ChatModel,
    parent: Parent,
    transcript: str,
    *,
    delivered_messages: list[tuple[str, str]],
    signals: list[Signal],
) -> CallAnalysis:
    meds = ", ".join(m.get("name", "") for m in (parent.medications or [])) or "none listed"
    msgs = "\n".join(f"- {mid}: {text}" for mid, text in delivered_messages) or "- none"
    system = ANALYSIS_SYSTEM.format(name=parent.name, meds=meds, messages=msgs)
    try:
        analysis = complete_json(
            model,
            [Message("system", system), Message("user", f"Transcript:\n{transcript}")],
            CallAnalysis,
            max_tokens=1200,
        )
    except ModelError as exc:
        log.warning("analysis failed, using safe fallback: %s", exc)
        analysis = CallAnalysis(
            summary="The call happened but the notes could not be written "
            "automatically. Please read the transcript.",
            mood="unclear",
        )

    # Evidence must be real. Drop anything whose quote is not in the transcript.
    flat = " ".join(transcript.lower().split())

    def grounded(evidence: str) -> bool:
        e = " ".join(evidence.lower().split())
        return bool(e) and (
            e in flat
            or any(
                " ".join(e.split()[i : i + 4]) in flat for i in range(0, max(1, len(e.split()) - 3))
            )
        )

    analysis.observations = [o for o in analysis.observations if grounded(o.evidence)]
    analysis.memories = [m for m in analysis.memories if grounded(m.evidence)]
    analysis.alerts = [a for a in analysis.alerts if grounded(a.evidence)]

    # Code-detected signals always win and are never downgraded.
    existing = {(a.kind, a.tier) for a in analysis.alerts}
    for s in signals:
        if (s.kind, s.tier) in existing:
            continue
        analysis.alerts.append(
            AlertOut(tier=s.tier, kind=s.kind, text=_signal_text(parent, s), evidence=s.evidence)
        )
    # If the model called an emergency "today" but code said "now", raise it.
    now_kinds = {s.kind for s in signals if s.tier == "now"}
    for a in analysis.alerts:
        if a.kind in now_kinds:
            a.tier = "now"
    if now_kinds and analysis.mood in ("ok", "bright"):
        analysis.mood = "unclear"  # never "sounded okay" on a call where they fell
    analysis.alerts.sort(key=lambda a: {"now": 0, "today": 1, "info": 2}[a.tier])
    return analysis


def _signal_text(parent: Parent, s: Signal) -> str:
    who = parent.address()
    texts = {
        "fall": f"{who} said they fell. Please check on them immediately.",
        "chest_pain": f"{who} mentioned chest pain. Please check on them immediately.",
        "breathing": f"{who} said they are having trouble breathing. Please check immediately.",
        "stroke": f"{who} described symptoms that could be a stroke. Please check immediately.",
        "bleeding": f"{who} mentioned bleeding that may not be stopping. Please check immediately.",
        "emergency": f"{who} asked for help during the call. Please check immediately.",
        "scam_live": f"{who} may have given details to a scam caller. Please call them now.",
        "pain": f"{who} mentioned significant pain today.",
        "unwell": f"{who} sounded unwell today (fever, dizziness or similar).",
        "confusion": f"{who} seemed confused about time or place today.",
        "low_mood": f"{who} sounded low or lonely today.",
        "medication_concern": f"{who} mentioned a problem with their medication.",
        "scam": f"{who} mentioned a suspicious call or request for money.",
        "no_food": f"{who} said they have not eaten.",
    }
    return texts.get(s.kind, f"{who} said something worth checking: {s.evidence[:80]}")


def no_answer_alert(parent: Parent, consecutive: int) -> tuple[str, str] | None:
    """Tier and text when a call goes unanswered; None if not yet worth an alert."""
    who = parent.address()
    if consecutive >= 3:
        return "now", f"{who} has not answered for {consecutive} days. Please check on them."
    if consecutive == 2:
        return "today", f"{who} did not answer today or yesterday. Worth a call."
    return None
