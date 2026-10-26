"""The conversation brain: what Sunday says next.

A call follows a loose plan, not a script. The plan gives the model a
checklist (greeting, how are you, sleep, medication, family messages, any
follow-ups from yesterday, open conversation, warm close) and the model
decides how to move through it naturally. Code tracks which items are done,
how long the call has run, and when to close.

Context the model sees every turn:
* who the parent is and how to address them
* durable memories (grandchildren, the knee, the neighbour)
* yesterday's follow-ups ("did the plumber come?")
* messages the family queued for this call
* the plan with completed items ticked off
* the transcript so far

Rules the model is held to (and that code enforces where it can):
* short sentences, one question at a time, never two
* never medical advice; sympathise and suggest telling the doctor/family
* never ask for money, cards, passwords, codes; say so if it comes up
* always honest that it is an assistant sent by the family
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from sunday.brain.model import ChatModel, Message, ModelError, complete_json
from sunday.logging_setup import get_logger
from sunday.models import Memory, Observation, Parent, QueuedMessage

log = get_logger("brain.conversation")

PlanItem = Literal[
    "greeting",
    "wellbeing",
    "sleep",
    "medication",
    "meals",
    "follow_ups",
    "family_messages",
    "open_talk",
    "story",
    "close",
]

DEFAULT_PLAN: tuple[PlanItem, ...] = (
    "greeting",
    "wellbeing",
    "sleep",
    "medication",
    "meals",
    "follow_ups",
    "family_messages",
    "open_talk",
    "close",
)

LANGUAGE_NAMES = {
    "en": "English",
    "ur": "Urdu",
    "hi": "Hindi",
    "pa": "Punjabi",
    "es": "Spanish",
    "ar": "Arabic",
    "bn": "Bengali",
}


@dataclass
class CallContext:
    parent: Parent
    memories: list[Memory]
    follow_ups: list[Observation]
    queued: list[QueuedMessage]
    family_names: list[str]
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    plan: tuple[PlanItem, ...] = DEFAULT_PLAN
    done: set[str] = field(default_factory=set)
    delivered_message_ids: set[str] = field(default_factory=set)

    def remaining(self) -> list[str]:
        return [p for p in self.plan if p not in self.done]

    def minutes_elapsed(self) -> float:
        return (datetime.now(UTC) - self.started_at).total_seconds() / 60


SYSTEM_PROMPT = """\
You are Sunday, a friendly caller who phones {name} every day on behalf of their family
({family}). You are an assistant, not a person, and you say so if asked. You call because the
family loves them and wants to know they are well.

How to speak:
- {language}. Warm, unhurried, plain words. Like a kind neighbour, not a nurse or a robot.
- SHORT turns: one or two sentences, then stop and listen. ONE question at a time. Never a list.
- Address them as "{address}". Use their name sometimes, not every turn.
- If they ramble, let them. If they repeat themselves, respond as if it is the first time.
- If you did not understand, say so simply and ask again. Never guess what they said.
- Pick up on what they say. If they mention a grandchild, a neighbour, the garden, ask about it.
- Follow the plan loosely; weave the items into natural talk, do not interrogate.

Hard rules:
- NEVER give medical advice, dosages, or diagnoses. If something sounds wrong, show concern and
  say you will let the family know, and that the doctor is the right person to ask.
- NEVER ask for money, bank details, cards, PINs, passwords or codes, and if they mention anyone
  asking for those, tell them plainly: that is a scam, hang up on such calls, and the family
  will be told.
- If they say they have fallen, have chest pain, cannot breathe, are bleeding, or need help:
  stop everything else, stay calm, keep them talking, tell them the family is being called,
  and suggest {emergency} if they can reach a phone. Do not hang up.
- Do not pretend to be a family member. Do not invent facts about their life; use only the
  memories below.
- Stay within {max_minutes} minutes. When the plan is done or they sound tired, close warmly.

What you know about {name} (from the family and earlier calls):
{memories}

Things to follow up from recent calls:
{follow_ups}

Messages from the family to pass on this call (say them naturally, then note the reply):
{queued}

Plan for this call: {plan}
Already covered: {done}
Minutes so far: {minutes:.0f}

Reply with ONLY what Sunday says next, in {language}. No labels, no stage directions.
"""


def _fmt_memories(memories: list[Memory]) -> str:
    return (
        "\n".join(f"- {m.text}" for m in memories[-40:])
        or "- (nothing yet; this may be an early call)"
    )


def _fmt_follow_ups(items: list[Observation]) -> str:
    lines = []
    for o in items[-6:]:
        when = f" (said on {o.at:%A})" if o.at else ""
        lines.append(f"- {o.text}{when}")
    return "\n".join(lines) or "- none"


def _fmt_queued(items: list[QueuedMessage], delivered: set[str]) -> str:
    lines = []
    for m in items:
        if m.id in delivered:
            continue
        verb = {"say": "Tell them", "ask": "Ask them", "remind": "Remind them"}.get(
            m.kind, "Tell them"
        )
        lines.append(f"- From {m.from_name}: {verb}: {m.text}")
    return "\n".join(lines) or "- none"


def build_system_prompt(ctx: CallContext, *, max_minutes: int, emergency_number: str) -> str:
    p = ctx.parent
    meds = ", ".join(f"{m.get('name')} ({m.get('when', 'daily')})" for m in (p.medications or []))
    memories = _fmt_memories(ctx.memories)
    if meds:
        memories += f"\n- Medication to ask about: {meds}"
    if p.notes:
        memories += f"\n- Family notes: {p.notes}"
    return SYSTEM_PROMPT.format(
        name=p.name,
        address=p.address(),
        family=", ".join(ctx.family_names) or "their family",
        language=LANGUAGE_NAMES.get(p.language, p.language),
        emergency=emergency_number,
        max_minutes=max_minutes,
        memories=memories,
        follow_ups=_fmt_follow_ups(ctx.follow_ups),
        queued=_fmt_queued(ctx.queued, ctx.delivered_message_ids),
        plan=", ".join(ctx.plan),
        done=", ".join(sorted(ctx.done)) or "nothing yet",
        minutes=ctx.minutes_elapsed(),
    )


def opening_line(ctx: CallContext) -> str:
    """The first thing said when the parent picks up. Fixed, so it is always honest."""
    p = ctx.parent
    who = p.introduced_by or ("your family" if not ctx.family_names else f"{ctx.family_names[0]}")
    me = p.address()
    lines = {
        "en": f"Hello {me}, it's Sunday calling, the daily call from {who}. How are you today?",
        "ur": f"السلام علیکم {me}، میں سنڈے ہوں، {who} کی طرف سے روز کی کال۔ آج آپ کیسی ہیں؟",
        "hi": f"नमस्ते {me}, मैं सनडे हूँ, {who} की तरफ़ से रोज़ का फ़ोन। आज आप कैसी हैं?",
        "pa": f"ਸਤਿ ਸ੍ਰੀ ਅਕਾਲ {me}, ਮੈਂ ਸੰਡੇ ਹਾਂ, {who} ਵੱਲੋਂ ਰੋਜ਼ ਦਾ ਫ਼ੋਨ। ਅੱਜ ਤੁਸੀਂ ਕਿਵੇਂ ਹੋ?",
    }
    return lines.get(p.language, lines["en"])


def next_line(
    model: ChatModel,
    ctx: CallContext,
    transcript: list[tuple[str, str]],
    *,
    max_minutes: int,
    emergency_number: str,
) -> str:
    """Ask the model for Sunday's next turn given the whole call so far."""
    system = build_system_prompt(ctx, max_minutes=max_minutes, emergency_number=emergency_number)
    convo = [Message("system", system)]
    for speaker, text in transcript[-24:]:
        convo.append(Message("assistant" if speaker == "sunday" else "user", text))
    reply = model.chat(convo, temperature=0.6, max_tokens=120).text
    return _clean(reply)


def _clean(text: str) -> str:
    text = text.strip().strip('"')
    for prefix in ("Sunday:", "SUNDAY:", "Assistant:"):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
    # Hold the model to short turns: keep at most three sentences.
    import re

    parts = re.split(r"(?<=[.!?؟।])\s+", text)
    return " ".join(parts[:3]).strip().strip('"').strip("'").strip()


# --------------------------------------------------------------------------
# progress tracking: which plan items has the conversation covered?
# --------------------------------------------------------------------------


class _Progress(BaseModel):
    covered: list[str] = Field(default_factory=list, description="plan items now covered")
    delivered_messages: list[str] = Field(
        default_factory=list, description="ids of family messages that were passed on"
    )
    parent_sounds_tired_or_wants_to_stop: bool = False


PROGRESS_SYSTEM = """\
You track a phone call between Sunday (an assistant) and an older person. Given the plan items,
the family message ids, and the latest exchange, say which plan items are now covered and which
family messages have been passed on. Be conservative: an item is covered only if the parent
actually answered about it. Plan items: %s
"""


def update_progress(model: ChatModel, ctx: CallContext, transcript: list[tuple[str, str]]) -> None:
    """Cheap bookkeeping call so the plan advances. Failure is harmless."""
    recent = "\n".join(f"{s}: {t}" for s, t in transcript[-6:])
    pending = [m for m in ctx.queued if m.id not in ctx.delivered_message_ids]
    msgs = "\n".join(f"- {m.id}: {m.text}" for m in pending) or "- none"
    prompt = (
        f"Remaining plan items: {', '.join(ctx.remaining())}\nFamily messages pending:\n{msgs}\n\n"
        f"Latest exchange:\n{recent}"
    )
    try:
        result = complete_json(
            model,
            [Message("system", PROGRESS_SYSTEM % ", ".join(ctx.plan)), Message("user", prompt)],
            _Progress,
            max_tokens=200,
        )
    except ModelError as exc:
        log.warning("progress tracking failed: %s", exc)
        return
    for item in result.covered:
        if item in ctx.plan:
            ctx.done.add(item)
    for mid in result.delivered_messages:
        if any(m.id == mid for m in ctx.queued):
            ctx.delivered_message_ids.add(mid)
    if result.parent_sounds_tired_or_wants_to_stop:
        ctx.done.update(p for p in ctx.plan if p != "close")
