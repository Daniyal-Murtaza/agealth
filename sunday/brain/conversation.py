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

import re
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

    def pending_asks(self) -> list[QueuedMessage]:
        """ASK messages the family is waiting on; the call must not close with these open."""
        return [
            m for m in self.queued if m.kind == "ask" and m.id not in self.delivered_message_ids
        ]

    def minutes_elapsed(self) -> float:
        return (datetime.now(UTC) - self.started_at).total_seconds() / 60


SYSTEM_PROMPT = """\
You are Sunday, a friendly caller who phones {name} every day on behalf of their family
({family}). You are an assistant, not a person, and you say so if asked. You call because the
family loves them and wants to know they are well.

How to speak:
- {language}. Warm, unhurried, plain words. Like a kind neighbour, not a nurse or a robot.
- SHORT turns: one or two sentences, then stop and listen. ONE question at a time. Never a list.
- Address them as "{address}" at most once every few turns, never in every sentence.
- If they ramble, let them. If they repeat themselves, respond as if it is the first time.
- If you did not understand, say so simply and ask again. Never guess what they said.
- Pick up on what they say. If they mention a grandchild, a neighbour, the garden, ask about it.
- Follow the plan loosely; weave the items into natural talk, do not interrogate. Ask about
  each plan item ONCE; if they already answered (e.g. they said how they slept), do not ask again.
- Never ask a confirmation question about something they just told you. If they say "I took
  it", do not ask "did you take it this morning?"; acknowledge it and move on.
- Only promise to pass something on when it is a reply to a family message or something the
  family should know. Never say "I will tell Farida" or similar about people outside the family.
- Family messages marked ASK are questions you must actually put to them and get an answer to.
  Never claim you have told the family something they have not yet answered.

Hard rules:
- NEVER give medical advice, dosages, or diagnoses. Do not ask diagnostic questions (is it
  swollen, is it red, do you have a temperature). If something sounds wrong, show concern, ask
  how they are managing and whether someone can look at it, and say you will let the family
  know; the doctor is the right person to ask.
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

Messages from the family to pass on this call (say them naturally, then note the reply).
Deliver these early, right after asking how they are; do not leave them to the end:
{queued}

Plan for this call: {plan}
Already covered (do NOT ask about these again): {done}
{ask_nudge}
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
        verb = {
            "say": "Tell them",
            "ask": "ASK them and wait for their answer",
            "remind": "Remind them",
        }.get(m.kind, "Tell them")
        lines.append(f"- [{m.id}] From {m.from_name}: {verb}: {m.text}")
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
        ask_nudge=(
            "IMPORTANT: the family is waiting for an answer to: "
            + " | ".join(m.text for m in ctx.pending_asks())
            + ". Ask it plainly now, as a question, and wait for the reply."
            if ctx.pending_asks()
            else ""
        ),
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
    reply = _clean(model.chat(convo, temperature=0.6, max_tokens=120).text)
    recent = [t for sp, t in transcript if sp == "sunday"]
    return _trim_address(reply, ctx.parent.address(), recent)


def _trim_address(text: str, address: str, recent_sunday_turns: list[str]) -> str:
    """Older people notice when their name is used in every sentence. Allow it in at most
    one of every three of Sunday's turns, and never more than once in a turn."""
    if not address:
        return text
    pat = re.compile(rf",?\s*\b{re.escape(address)}\b[,!]?", re.IGNORECASE)
    hits = list(pat.finditer(text))
    if not hits:
        return text
    used_recently = any(
        re.search(rf"\b{re.escape(address)}\b", t, re.IGNORECASE) for t in recent_sunday_turns[-2:]
    )
    keep = 0 if used_recently else 1
    out, last = [], 0
    for i, m in enumerate(hits):
        if i < keep:
            continue
        out.append(text[last : m.start()])
        last = m.end()
    out.append(text[last:])
    cleaned = "".join(out)
    cleaned = re.sub(r"\s+([.,!?])", r"\1", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    cleaned = re.sub(r"^[,.\s]+", "", cleaned)
    return (cleaned[:1].upper() + cleaned[1:]) if cleaned else text


def _clean(text: str) -> str:
    text = text.strip().strip('"')
    for prefix in ("Sunday:", "SUNDAY:", "Assistant:"):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
    # Hold the model to short turns: keep at most three sentences.
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
family messages have been passed on.
- An item is covered when the parent has given the information, whether or not Sunday asked for
  it directly. "slept a bit late" covers sleep; "took it with my chai" covers medication;
  "paratha and chai" covers meals. Do not make Sunday ask again for something already said.
- A "Tell them" message counts as delivered only when Sunday has said its actual content to
  the parent. Sunday saying "I'll let Hira know" does NOT deliver Hira's message.
- An "ASK them" message counts as delivered ONLY when Sunday asked that exact question AND the
  parent answered it. Sunday mentioning the topic in passing does not count.
- "family_messages" is covered only when every listed message is delivered.
Plan items: %s
"""


def update_progress(model: ChatModel, ctx: CallContext, transcript: list[tuple[str, str]]) -> None:
    """Cheap bookkeeping call so the plan advances. Failure is harmless."""
    recent = "\n".join(f"{s}: {t}" for s, t in transcript[-6:])
    pending = [m for m in ctx.queued if m.id not in ctx.delivered_message_ids]
    msgs = (
        "\n".join(f"- {m.id} ({m.kind.upper()} from {m.from_name}): {m.text}" for m in pending)
        or "- none"
    )
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
    sunday_turns = [t for sp, t in transcript if sp == "sunday"]
    for mid in result.delivered_messages:
        msg = next((m for m in ctx.queued if m.id == mid), None)
        # Code gate: the model marks "I'll let Hira know" as delivering Hira's message. A
        # message is delivered only if Sunday actually said its content to the parent.
        if msg is not None and message_was_said(msg.text, sunday_turns):
            ctx.delivered_message_ids.add(mid)
    if result.parent_sounds_tired_or_wants_to_stop:
        ctx.done.update(p for p in ctx.plan if p != "close")
    if ctx.pending_asks():
        ctx.done.discard("family_messages")  # code gate: an open ASK keeps the call going


_STOP = frozenset(
    {
        "the", "and", "that", "this", "with", "from", "your", "they", "them", "their", "will",
        "would", "could", "should", "have", "about", "after", "before", "been", "being", "were",
        "what", "when", "where", "which", "while", "there", "here", "into", "over", "then",
        "than", "just", "also", "very", "really", "please", "tell", "does", "want",
    }
)  # fmt: skip


def _content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"\w+", text.lower()) if len(w) >= 4 and w not in _STOP}


def message_was_said(message: str, sunday_turns: list[str]) -> bool:
    """Did Sunday actually say this family message to the parent? At least half of the
    message's content words must appear in one of Sunday's turns. "I'll call Thursday after
    work" is not delivered by "I'll let Hira know"; it is by "Hira says she'll call Thursday"."""
    words = _content_words(message)
    if not words:
        return bool(sunday_turns)
    need = max(1, (len(words) + 1) // 2)
    return any(len(words & _content_words(t)) >= need for t in sunday_turns)
