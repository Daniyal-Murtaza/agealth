"""Trends and the weekly digest: patterns a family would otherwise miss.

Pure code over stored observations; no model needed. The output is plain
language and never a diagnosis: counts, streaks, and "worth a look" flags.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sunday.models import Alert, Call, Observation, Parent
from sunday.repository import Repository


@dataclass
class Trends:
    days: int
    calls_completed: int = 0
    calls_missed: int = 0
    medication_taken: int = 0
    medication_missed: int = 0
    medication_unclear: int = 0
    sleep_poor_days: int = 0
    pain_mentions: int = 0
    low_mood_mentions: int = 0
    lonely_mentions: int = 0
    confusion_mentions: int = 0
    mood_counts: Counter = field(default_factory=Counter)
    alerts_now: int = 0
    alerts_today: int = 0
    open_follow_ups: list[str] = field(default_factory=list)
    worth_a_look: list[str] = field(default_factory=list)


def compute_trends(repo: Repository, parent: Parent, *, days: int = 7) -> Trends:
    t = Trends(days=days)
    since = datetime.now(UTC) - timedelta(days=days)
    calls: list[Call] = repo.recent_calls(parent.id, days=days, limit=100)
    t.calls_completed = sum(1 for c in calls if c.status == "completed")
    t.calls_missed = sum(1 for c in calls if c.status == "no_answer")
    for c in calls:
        if c.mood:
            t.mood_counts[c.mood] += 1

    obs: list[Observation] = repo.observations_since(parent.id, days=days)
    for o in obs:
        if o.kind == "medication":
            if o.value == "taken":
                t.medication_taken += 1
            elif o.value == "missed":
                t.medication_missed += 1
            else:
                t.medication_unclear += 1
        elif o.kind == "sleep" and o.value in ("poor", "bad"):
            t.sleep_poor_days += 1
        elif o.kind == "pain":
            t.pain_mentions += 1
        elif o.kind == "mood" and o.value in ("low", "sad", "lonely"):
            t.low_mood_mentions += 1
        elif o.kind == "social" and "lonely" in (o.text + o.value).lower():
            t.lonely_mentions += 1
        elif o.kind == "memory":
            t.confusion_mentions += 1
    t.open_follow_ups = [o.text for o in repo.pending_follow_ups(parent.id, days=days)][-5:]

    who = parent.address()
    # Alerts raised this week come first. A fall on Tuesday is the headline of the week,
    # whatever the medication count says.
    alerts: list[Alert] = repo.alerts_since(parent.id, days=days)
    for a in alerts:
        if a.tier == "now":
            t.alerts_now += 1
            t.worth_a_look.append(f"URGENT on {a.at:%A}: {a.text}")
    today_alerts = [a for a in alerts if a.tier == "today"]
    t.alerts_today = len(today_alerts)
    if today_alerts:
        kinds = Counter(a.kind.replace("_", " ") for a in today_alerts)
        detail = ", ".join(f"{k} x{n}" if n > 1 else k for k, n in kinds.most_common())
        t.worth_a_look.append(
            f"{len(today_alerts)} 'check today' alert{'s' if len(today_alerts) > 1 else ''}: "
            f"{detail}."
        )
    if t.medication_missed >= 2:
        t.worth_a_look.append(f"{who} missed medication {t.medication_missed} times this week.")
    if t.sleep_poor_days >= 3:
        t.worth_a_look.append(f"Poor sleep on {t.sleep_poor_days} days.")
    if t.pain_mentions >= 3:
        t.worth_a_look.append(f"Pain came up {t.pain_mentions} times.")
    if t.low_mood_mentions + t.lonely_mentions >= 2:
        t.worth_a_look.append("Mentioned feeling low or lonely more than once.")
    if t.confusion_mentions >= 2:
        t.worth_a_look.append("Some confusion about days or details came up more than once.")
    if t.calls_missed >= 2:
        t.worth_a_look.append(f"{t.calls_missed} calls went unanswered.")
    _ = since
    return t


def format_digest(parent: Parent, t: Trends) -> str:
    who = parent.address()
    lines = [f"Sunday - {who}'s week", ""]
    lines.append(f"Calls: {t.calls_completed} answered, {t.calls_missed} missed.")
    if t.alerts_now or t.alerts_today:
        lines.append(f"Alerts: {t.alerts_now} urgent, {t.alerts_today} check-today.")
    if t.medication_taken + t.medication_missed + t.medication_unclear:
        lines.append(
            f"Medication: taken {t.medication_taken}, missed {t.medication_missed}"
            + (f", unclear {t.medication_unclear}" if t.medication_unclear else "")
            + "."
        )
    if t.mood_counts:
        parts = [f"{v} {k}" for k, v in t.mood_counts.most_common() if k != "unclear"]
        if parts:
            lines.append("Mood: " + ", ".join(parts) + ".")
    if t.sleep_poor_days:
        lines.append(f"Poor sleep on {t.sleep_poor_days} day(s).")
    if t.worth_a_look:
        lines += ["", "Worth a look:"] + [f"- {w}" for w in t.worth_a_look]
    else:
        lines += ["", "Nothing worrying this week."]
    if t.open_follow_ups:
        lines += ["", "Still open:"] + [f"- {f}" for f in t.open_follow_ups]
    lines += ["", "Sunday is an assistant, not a doctor. If anything here worries you, call them."]
    return "\n".join(lines)
