"""After the call: analyse, store, and tell the family.

``finish_call`` is the one function the transports call when a call ends.
It is idempotent: running it twice for the same call does nothing the second
time, so a retry after a crash is safe.

Order matters:
1. ``now`` alerts raised *during* the call were already persisted by the
   session; they are sent here first, before any model call, so a slow model
   can never delay an emergency message.
2. The transcript is analysed into summary, observations, memories, alerts.
3. Everything is stored.
4. The summary (and any new alerts) go to each family member who wants them.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sunday.brain.analysis import CallAnalysis, analyse_call
from sunday.brain.model import ChatModel
from sunday.brain.safety import Signal
from sunday.logging_setup import get_logger
from sunday.models import Alert, Call, FamilyMember, Parent
from sunday.notify.messenger import Messenger, wants
from sunday.repository import Repository

log = get_logger("notify.pipeline")

TIER_LABEL = {"now": "URGENT", "today": "Please check today", "info": "FYI"}


def format_alert(parent: Parent, alert: Alert) -> str:
    head = TIER_LABEL.get(alert.tier, "FYI")
    body = f"{head} - Sunday about {parent.address()}\n{alert.text}"
    if alert.evidence:
        body += f'\nThey said: "{alert.evidence[:160]}"'
    if alert.tier == "now":
        body += "\nSunday is staying on the line with them. Please call them or go now."
    return body


def format_summary(parent: Parent, call: Call, analysis: CallAnalysis) -> str:
    when = (call.ended_at or datetime.now(UTC)).strftime("%a %d %b, %H:%M")
    mood = {
        "bright": "sounded bright",
        "ok": "sounded okay",
        "low": "sounded a bit low",
        "unclear": "",
    }.get(analysis.mood, "")
    lines = [
        f"Sunday called {parent.address()} - {when}" + (f" - {mood}" if mood else ""),
        "",
        analysis.summary.strip(),
    ]
    follow = [o.text for o in analysis.observations if o.follow_up]
    if follow:
        lines += ["", "Tomorrow I'll ask about: " + "; ".join(follow[:3])]
    replies = [r for r in analysis.message_replies if r.response.strip()]
    if replies:
        lines += ["", "Replies to your messages:"] + [f"- {r.response}" for r in replies[:3]]
    return "\n".join(lines)


def _deliver_alerts(
    repo: Repository, parent: Parent, family: list[FamilyMember], messenger: Messenger
) -> int:
    sent = 0
    for alert in repo.unnotified_alerts(parent.id):
        text = format_alert(parent, alert)
        for member in family:
            if wants(member, alert.tier):
                try:
                    messenger.send(member, text)
                    sent += 1
                except Exception as exc:  # noqa: BLE001 - one bad number must not stop the rest
                    log.error("alert to %s failed: %s", member.name, exc)
        repo.mark_alert_notified(alert.id)
    return sent


def finish_call(
    repo: Repository,
    model: ChatModel,
    messenger: Messenger,
    call_id: str,
    *,
    signals: list[Signal],
    delivered_message_ids: set[str],
) -> CallAnalysis | None:
    call = repo.get_call(call_id)
    if call is None or call.notified_at is not None:
        return None  # already processed
    parent = repo.get_parent(call.parent_id)
    if parent is None:
        return None
    family = repo.family_for(parent.id)

    # 1. Emergencies first, before anything slow.
    _deliver_alerts(repo, parent, family, messenger)

    transcript = call.transcript()
    if sum(1 for t in call.turns if t.speaker == "parent") == 0:
        repo.update_call(
            call_id,
            notified_at=datetime.now(UTC),
            summary="Call connected but nothing was heard from them.",
        )
        return None

    # 2. Analyse.
    delivered = [
        (m.id, m.text)
        for m in repo.undelivered_messages(parent.id)
        if m.id in delivered_message_ids
    ]
    analysis = analyse_call(
        model, parent, transcript, delivered_messages=delivered, signals=signals
    )

    # 3. Store.
    repo.update_call(call_id, summary=analysis.summary, mood=analysis.mood)
    repo.add_observations(call_id, parent.id, [o.model_dump() for o in analysis.observations])
    for m in analysis.memories:
        repo.add_memory(parent.id, m.text, topic=m.topic, source=call_id, evidence=m.evidence)
    existing = {
        (a.kind, a.tier) for a in repo.alerts_since(parent.id, days=1) if a.call_id == call_id
    }
    for a in analysis.alerts:
        if (a.kind, a.tier) not in existing:
            repo.add_alert(
                parent.id,
                call_id=call_id,
                tier=a.tier,
                kind=a.kind,
                text=a.text,
                evidence=a.evidence,
            )
    replies = {r.message_id: r.response for r in analysis.message_replies}
    for mid, _ in delivered:
        repo.mark_message_delivered(mid, call_id, response=replies.get(mid, ""))

    # 4. Tell the family.
    _deliver_alerts(repo, parent, family, messenger)  # anything new from the analysis
    text = format_summary(parent, repo.get_call(call_id), analysis)
    for member in family:
        if wants(member, "info"):
            try:
                messenger.send(member, text)
            except Exception as exc:  # noqa: BLE001
                log.error("summary to %s failed: %s", member.name, exc)
    repo.update_call(call_id, notified_at=datetime.now(UTC))
    log.info(
        "call %s finished: mood=%s obs=%d alerts=%d",
        call_id,
        analysis.mood,
        len(analysis.observations),
        len(analysis.alerts),
    )
    return analysis


def notify_no_answer(repo: Repository, messenger: Messenger, parent_id: str) -> int:
    """Send any pending no-answer alerts (raised by the dialer) to the family."""
    parent = repo.get_parent(parent_id)
    if parent is None:
        return 0
    return _deliver_alerts(repo, parent, repo.family_for(parent_id), messenger)
