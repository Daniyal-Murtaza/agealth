"""The call session: one live conversation, independent of how audio arrives.

A transport (Twilio WebSocket, terminal simulator, test harness) does three
things: tells the session the call started, feeds it each thing the parent
said, and plays back what the session returns. Everything else lives here:

* building the context (memories, follow-ups, family messages)
* the opening line, each reply, and the warm close
* safety: keyword triggers on every utterance; a ``now`` signal switches the
  session into emergency mode (scripted lines, stays on the line, alert
  raised immediately rather than after the call)
* limits: max turns and max minutes, enforced in code
* persistence: every turn is written to the database as it happens, so a
  dropped call still leaves a transcript

The session never raises to the transport for model failures; if the model is
unavailable it falls back to a fixed, safe line and ends the call politely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from sunday.brain.conversation import (
    CallContext,
    next_line,
    opening_line,
    update_progress,
)
from sunday.brain.model import ChatModel, ModelError
from sunday.brain.safety import EMERGENCY_LINES, EMERGENCY_NUMBERS, SafetyState, keyword_signals
from sunday.config import Settings
from sunday.logging_setup import get_logger
from sunday.models import Call, Parent
from sunday.repository import Repository

log = get_logger("calls.session")


class Phase(StrEnum):
    NOT_STARTED = "not_started"
    TALKING = "talking"
    EMERGENCY = "emergency"
    CLOSING = "closing"
    ENDED = "ended"


CLOSING_LINES = {
    "en": (
        "It was lovely talking to you, {name}. I'll let the family know you're well. "
        "Speak tomorrow."
    ),
    "ur": (
        "{name}، آپ سے بات کر کے بہت اچھا لگا۔ میں گھر والوں کو بتا دوں گی کہ آپ ٹھیک ہیں۔ "
        "کل بات ہوگی۔"
    ),
    "hi": ("{name}, आपसे बात करके बहुत अच्छा लगा। मैं घरवालों को बता दूँगी कि आप ठीक हैं। कल बात होगी।"),
}
FALLBACK_LINES = {
    "en": (
        "I'm sorry, I'm having a little trouble hearing today. I'll tell the family you're "
        "well and call again tomorrow."
    ),
    "ur": (
        "معاف کیجیے، آج مجھے سننے میں تھوڑی مشکل ہو رہی ہے۔ میں گھر والوں کو بتا دوں گی اور "
        "کل دوبارہ فون کروں گی۔"
    ),
    "hi": ("माफ़ कीजिए, आज मुझे सुनने में थोड़ी दिक़्क़त हो रही है। मैं घरवालों को बता दूँगी और कल फिर फ़ोन करूँगी।"),
}


@dataclass
class SessionEvent:
    """What the transport should do after feeding an utterance."""

    say: str
    end_call: bool = False
    emergency: bool = False
    alerts_raised: list[str] = field(default_factory=list)


class CallSession:
    def __init__(
        self,
        repo: Repository,
        model: ChatModel,
        settings: Settings,
        parent: Parent,
        *,
        call: Call | None = None,
        country: str = "US",
    ) -> None:
        self.repo = repo
        self.model = model
        self.settings = settings
        self.parent = parent
        self.call = call or repo.create_call(
            parent.id, status="in_progress", started_at=datetime.now(UTC)
        )
        self.phase = Phase.NOT_STARTED
        self.transcript: list[tuple[str, str]] = []
        self.safety = SafetyState()
        self.emergency_step = 0
        self.emergency_number = EMERGENCY_NUMBERS.get(country, "911")
        self.ctx = CallContext(
            parent=parent,
            memories=repo.memories_for(parent.id),
            follow_ups=repo.pending_follow_ups(parent.id),
            queued=repo.undelivered_messages(parent.id),
            family_names=[m.name for m in repo.family_for(parent.id)],
        )
        self._parent_turns = 0

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> SessionEvent:
        """Call answered. Returns the opening line."""
        self.repo.update_call(self.call.id, status="in_progress", started_at=datetime.now(UTC))
        self.phase = Phase.TALKING
        line = opening_line(self.ctx)
        self._say(line)
        self.ctx.done.add("greeting")
        return SessionEvent(say=line)

    def hear(self, text: str) -> SessionEvent:
        """The parent said something. Decide what Sunday says next."""
        text = text.strip()
        if not text or self.phase in (Phase.ENDED, Phase.NOT_STARTED):
            return SessionEvent(say="", end_call=self.phase == Phase.ENDED)
        self.transcript.append(("parent", text))
        self.repo.add_turn(self.call.id, "parent", text)
        self._parent_turns += 1

        fresh = self.safety.add(keyword_signals(text))
        raised = [self._raise_alert(s) for s in fresh]
        if self.safety.has_now():
            return self._emergency_turn(raised)

        if self.phase == Phase.CLOSING or self._over_budget():
            return self._close(raised)

        try:
            update_progress(self.model, self.ctx, self.transcript)
            if self.ctx.remaining() == ["close"] or not self.ctx.remaining():
                return self._close(raised)
            reply = next_line(
                self.model,
                self.ctx,
                self.transcript,
                max_minutes=self.settings.max_call_minutes,
                emergency_number=self.emergency_number,
            )
        except ModelError as exc:
            log.error("model unavailable mid-call %s: %s", self.call.id, exc)
            return self._end(
                FALLBACK_LINES.get(self.parent.language, FALLBACK_LINES["en"]),
                reason="model_error",
                alerts=raised,
            )
        if not reply:
            reply = FALLBACK_LINES.get(self.parent.language, FALLBACK_LINES["en"])
        self._say(reply)
        return SessionEvent(say=reply, alerts_raised=raised)

    def hangup(self, reason: str = "parent_hung_up") -> None:
        """Transport reports the line dropped."""
        if self.phase != Phase.ENDED:
            self.phase = Phase.ENDED
            self.repo.update_call(
                self.call.id, status="completed", ended_at=datetime.now(UTC), end_reason=reason
            )

    # -- internals ---------------------------------------------------------------

    def _say(self, text: str) -> None:
        self.transcript.append(("sunday", text))
        self.repo.add_turn(self.call.id, "sunday", text)

    def _over_budget(self) -> bool:
        return (
            self._parent_turns >= self.settings.max_turns
            or self.ctx.minutes_elapsed() >= self.settings.max_call_minutes
        )

    def _close(self, alerts: list[str]) -> SessionEvent:
        line = CLOSING_LINES.get(self.parent.language, CLOSING_LINES["en"]).format(
            name=self.parent.address()
        )
        return self._end(line, reason="completed", alerts=alerts)

    def _end(self, line: str, *, reason: str, alerts: list[str]) -> SessionEvent:
        self._say(line)
        self.phase = Phase.ENDED
        self.repo.update_call(
            self.call.id, status="completed", ended_at=datetime.now(UTC), end_reason=reason
        )
        return SessionEvent(say=line, end_call=True, alerts_raised=alerts)

    def _emergency_turn(self, alerts: list[str]) -> SessionEvent:
        """Scripted, calm, repeated. Never hangs up; the transport keeps the line open."""
        self.phase = Phase.EMERGENCY
        family = ", ".join(self.ctx.family_names[:2]) or "your family"
        lines = EMERGENCY_LINES.get(self.parent.language, EMERGENCY_LINES["en"])
        line = lines[min(self.emergency_step, len(lines) - 1)].format(
            family=family, emergency=self.emergency_number
        )
        self.emergency_step += 1
        self._say(line)
        return SessionEvent(say=line, emergency=True, alerts_raised=alerts)

    def _raise_alert(self, signal) -> str:
        """Persist an alert the moment a signal fires; the family is told right away."""
        from sunday.brain.analysis import _signal_text

        alert = self.repo.add_alert(
            self.parent.id,
            call_id=self.call.id,
            tier=signal.tier,
            kind=signal.kind,
            text=_signal_text(self.parent, signal),
            evidence=signal.evidence,
        )
        log.warning("alert %s/%s raised during call %s", signal.tier, signal.kind, self.call.id)
        return alert.id
