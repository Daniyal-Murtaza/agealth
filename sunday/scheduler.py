"""Who is due a call right now? Pure code; run from a cron, a loop, or the CLI."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sunday.models import Parent
from sunday.repository import Repository

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def is_due(
    parent: Parent, repo: Repository, *, now: datetime | None = None, window_minutes: int = 20
) -> bool:
    """True if it is the parent's call time (within the window) and we have not called today."""
    if not parent.active:
        return False
    now = now or datetime.now(UTC)
    try:
        local = now.astimezone(ZoneInfo(parent.timezone or "UTC"))
    except Exception:  # noqa: BLE001 - bad tz string in the DB should not crash the scheduler
        local = now
    if DAYS[local.weekday()] not in (parent.call_days or DAYS):
        return False
    hh, mm = (parent.call_time or "09:00").split(":")
    target = local.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    if not (target <= local < target + timedelta(minutes=window_minutes)):
        return False
    for call in repo.recent_calls(parent.id, days=1, limit=10):
        if call.started_at and call.started_at.astimezone(local.tzinfo).date() == local.date():
            return False
    return True


def due_parents(repo: Repository, *, now: datetime | None = None) -> list[Parent]:
    return [p for p in repo.list_parents() if is_due(p, repo, now=now)]
