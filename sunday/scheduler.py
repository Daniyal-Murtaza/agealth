"""Who is due a call right now? Pure code; run from a cron, a loop, or the CLI."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sunday.models import Parent
from sunday.repository import Repository

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


RETRY_AFTER_MINUTES = 15  # one more try if the first ring went unanswered, then leave it


def is_due(
    parent: Parent, repo: Repository, *, now: datetime | None = None, window_minutes: int = 20
) -> bool:
    """True if we should ring now: it is the parent's call time, nothing has been tried
    today, or exactly one attempt went unanswered at least fifteen minutes ago. Never more
    than two attempts in a day; an older person who is out should not come home to six
    missed calls, and two attempts must not look like two days of silence."""
    if not parent.active or not parent.intro_done:
        return False
    now = now or datetime.now(UTC)
    local = parent.local_now(now)
    if DAYS[local.weekday()] not in (parent.call_days or DAYS):
        return False
    hh, mm = (parent.call_time or "09:00").split(":")
    target = local.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    if not (target <= local < target + timedelta(minutes=window_minutes + RETRY_AFTER_MINUTES)):
        return False
    today = [
        c
        for c in repo.recent_calls(parent.id, days=2, limit=20)
        if parent.local_now(c.started_at or c.scheduled_for).date() == local.date()
    ]
    if not today:
        return local < target + timedelta(minutes=window_minutes)
    if len(today) >= 2 or any(c.status not in ("no_answer", "failed", "cancelled") for c in today):
        return False  # answered, in progress, still ringing, or already retried
    first = today[0]
    tried_at = parent.local_now(first.scheduled_for or first.started_at)
    return local - tried_at >= timedelta(minutes=RETRY_AFTER_MINUTES)


def due_parents(repo: Repository, *, now: datetime | None = None) -> list[Parent]:
    return [p for p in repo.list_parents() if is_due(p, repo, now=now)]


DIGEST_DAY = 6  # Sunday
DIGEST_HOUR = 18  # local; the window is the whole evening so one cron tick is enough


def digest_due(parent: Parent, *, now: datetime | None = None) -> bool:
    """Sunday evening, local time, and we have not sent this week's report yet."""
    if not parent.active:
        return False
    local = parent.local_now(now or datetime.now(UTC))
    if local.weekday() != DIGEST_DAY or not (DIGEST_HOUR <= local.hour < 22):
        return False
    last = parent.last_digest_at
    return last is None or (local - last.astimezone(local.tzinfo)) > timedelta(days=6)


def digest_due_parents(repo: Repository, *, now: datetime | None = None) -> list[Parent]:
    return [p for p in repo.list_parents() if digest_due(p, now=now)]
