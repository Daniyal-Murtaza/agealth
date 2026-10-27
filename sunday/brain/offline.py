"""An offline stand-in brain so Sunday runs with no API key at all.

It is not clever. It recognises which of our three prompt types it is being
asked (next line, progress bookkeeping, after-call analysis) and answers with
simple rules: walks the plan in order with fixed warm questions, ticks plan
items off when the parent replies, and writes a plain summary from the
transcript. Good enough to exercise every path end to end, in the simulator
and in tests, and to demo the product on a plane.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from sunday.brain.model import Message, Reply

_QUESTIONS = {
    "wellbeing": "How are you feeling today?",
    "sleep": "Did you sleep alright last night?",
    "medication": "Did you take your tablets this morning?",
    "meals": "Have you eaten something today?",
    "follow_ups": "And the thing from yesterday, did that get sorted?",
    "family_messages": "The family sent a message for you. Shall I read it?",
    "open_talk": "What are you planning to do today?",
    "story": "Tell me about when you were young.",
}
_ACKS = ["That's good to hear.", "I see.", "Alright.", "Thank you for telling me.", "Lovely."]


@dataclass
class OfflineChat:
    name: str = "offline"
    calls: list[list[Message]] = field(default_factory=list)
    _ack: int = 0

    def chat(
        self, messages: Sequence[Message], *, temperature: float = 0.6, max_tokens: int = 300
    ) -> Reply:
        self.calls.append(list(messages))
        system = messages[0].content if messages and messages[0].role == "system" else ""
        if "track a phone call" in system:
            return Reply(text=self._progress(system, messages), model=self.name)
        if "write notes for their family" in system:
            return Reply(text=self._analysis(messages[-1].content), model=self.name)
        return Reply(text=self._next_line(system, messages), model=self.name)

    # -- next line: acknowledge, then ask about the first remaining plan item ----------

    def _next_line(self, system: str, messages: Sequence[Message]) -> str:
        remaining = _between(system, "Plan for this call:", "\n")
        covered = _between(system, "Already covered", "\n")
        covered = covered.split(":", 1)[1].strip() if ":" in covered else ""
        done = {x.strip() for x in covered.split(",")} if covered else set()
        queued = _between(system, "Messages from the family to pass on this call", "Plan for")
        ack = _ACKS[self._ack % len(_ACKS)]
        self._ack += 1
        for item in [x.strip() for x in remaining.split(",")]:
            if item in done or item == "greeting":
                continue
            if item == "close":
                break
            if item == "family_messages":
                if "] From " not in queued:
                    continue
                m = re.search(
                    r"- \[(msg_[0-9a-f]+)\] From (\w+): (?:Tell them|ASK them and wait for their "
                    r"answer|Remind them): (.+)",
                    queued,
                )
                if m:
                    return f"{ack} {m.group(2)} asked me to tell you: {m.group(3).strip()}"
            if item == "follow_ups" and "- none" in _between(
                system, "Things to follow up", "Messages"
            ):
                continue
            return f"{ack} {_QUESTIONS.get(item, 'Tell me more about that.')}"
        return f"{ack} It was lovely talking to you."

    # -- progress: the last asked item counts as covered once the parent answers ---------

    def _progress(self, system: str, messages: Sequence[Message]) -> str:
        user = messages[-1].content
        remaining = [
            x.strip() for x in _between(user, "Remaining plan items:", "\n").split(",") if x.strip()
        ]
        exchange = _between(user, "Latest exchange:", None)
        last_sunday = [ln for ln in exchange.splitlines() if ln.startswith("sunday:")]
        covered: list[str] = []
        if last_sunday:
            said = last_sunday[-1].lower()
            for item, q in _QUESTIONS.items():
                if q.lower()[:20] in said and item in remaining:
                    covered.append(item)
            if "asked me to tell you" in said:
                covered.append("family_messages")
        delivered = (
            re.findall(r"- (msg_[0-9a-f]+) \(", user) if "family_messages" in covered else []
        )
        parent_said = " ".join(
            ln for ln in exchange.splitlines() if ln.startswith("parent:")
        ).lower()
        tired = bool(
            re.search(
                r"\b(let'?s talk tomorrow|talk tomorrow|i('?m| am) (very|so|too) tired|"
                r"sleep now|bye now|khuda hafiz|allah hafiz|that'?s enough)\b",
                parent_said,
            )
        )
        return json.dumps(
            {
                "covered": covered,
                "delivered_messages": delivered,
                "parent_sounds_tired_or_wants_to_stop": tired,
            }
        )

    # -- analysis: a plain summary from what the parent said ------------------------------

    def _analysis(self, user: str) -> str:
        parent_lines = [
            ln[len("parent:") :].strip() for ln in user.splitlines() if ln.startswith("parent:")
        ]
        joined = " ".join(parent_lines)
        low = joined.lower()
        mood = (
            "low"
            if re.search(r"\b(sad|lonely|tired|bad|hurt|pain)\b", low)
            else "bright"
            if re.search(r"\b(great|wonderful|lovely|happy|good)\b", low)
            else "ok"
        )
        obs = []
        if re.search(r"\b(took|taken|had) (it|them|my|the)\b", low) and "tablet" not in low:
            obs.append(
                {
                    "kind": "medication",
                    "text": "said they took their medication",
                    "value": "taken",
                    "evidence": next(
                        (p for p in parent_lines if re.search(r"\btook|taken\b", p.lower())), ""
                    ),
                }
            )
        if re.search(r"\bslept (well|fine|okay|ok|alright)\b", low):
            obs.append(
                {
                    "kind": "sleep",
                    "text": "slept alright",
                    "value": "ok",
                    "evidence": next(p for p in parent_lines if "slept" in p.lower()),
                }
            )
        elif re.search(r"\bslept (badly|poorly)|didn'?t sleep\b", low):
            obs.append(
                {
                    "kind": "sleep",
                    "text": "slept badly",
                    "value": "poor",
                    "evidence": next(p for p in parent_lines if "sle" in p.lower()),
                }
            )
        summary = (f"Sounded {mood}. " if mood != "ok" else "Sounded okay. ") + (
            f'They said: "{parent_lines[0][:80]}"' if parent_lines else ""
        )
        return json.dumps(
            {
                "summary": summary.strip(),
                "mood": mood,
                "observations": obs,
                "alerts": [],
                "memories": [],
                "message_replies": [],
            }
        )


def _between(text: str, start: str, end: str | None) -> str:
    i = text.find(start)
    if i < 0:
        return ""
    i += len(start)
    if end is None:
        return text[i:].strip()
    j = text.find(end, i)
    return text[i : j if j >= 0 else None].strip()
