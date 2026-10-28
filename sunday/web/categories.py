"""Sort what Sunday heard into the six things a family member actually asks about.

Pure functions over stored calls, observations, alerts and queued messages. Every
line here is short, plain, and built from data; where the parent's own words
exist they are shown. Nothing is a diagnosis. The one-line ``line`` on each
category is the point: it should answer the question before anyone taps.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from sunday.models import Parent
from sunday.repository import Repository

HEALTH_KINDS = ("pain", "mobility", "sleep", "mood", "memory")
DAY_KINDS = ("meal", "social", "household", "story", "plan", "other")
APPOINTMENT_WORDS = re.compile(
    r"\b(appointment|clinic|doctor|dr\.?|hospital|dentist|check-?up|blood test|scan|x-ray|"
    r"follow-?up|physio)\b",
    re.IGNORECASE,
)
TIER_WORD = {"now": "urgent", "today": "to check today", "info": "to note"}


@dataclass
class Item:
    when: date
    text: str
    quote: str = ""
    tag: str = ""  # short label: "taken", "missed", "urgent", "from Hira", ...
    tone: str = ""  # ok | warn | bad | "" for the tag colour
    call_id: str = ""
    action: str = ""  # ack:<alert_id> | remove:<msg_id>
    sort_key: tuple = ()


@dataclass
class Category:
    key: str
    title: str
    blurb: str  # what lives here, for the tile
    line: str  # the answer, from data
    tone: str = ""  # ok | warn | bad | ""
    count: int = 0
    items: list[Item] = field(default_factory=list)
    empty: str = ""  # shown on the detail page when there is nothing


def _cap(text: str) -> str:
    text = text.strip().rstrip(".")
    return text[:1].upper() + text[1:] if text else text


def _first_sentence(text: str) -> str:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return parts[0] if parts and parts[0] else text.strip()


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _day_label(d: date, today: date) -> str:
    if d == today:
        return "Today"
    if d == today - timedelta(days=1):
        return "Yesterday"
    if today - d < timedelta(days=7):
        return d.strftime("%A")
    return d.strftime("%a %d %b")


def build_categories(repo: Repository, parent: Parent, *, days: int = 14) -> dict[str, Category]:
    today = parent.local_today()
    week_ago = today - timedelta(days=7)
    local_day = lambda dt: parent.local_now(dt).date()  # noqa: E731

    calls = [c for c in repo.recent_calls(parent.id, days=days, limit=80)]
    completed = [c for c in calls if c.status == "completed" and c.summary]
    latest = completed[0] if completed else None
    obs = repo.observations_since(parent.id, days=days)
    alerts = repo.alerts_since(parent.id, days=days)
    queued = repo.undelivered_messages(parent.id, include_future=True)
    who = parent.address()

    cats: dict[str, Category] = {}

    # -- Amma's day --------------------------------------------------------------------
    day_items = [
        Item(
            when=local_day(o.at),
            text=_cap(o.text),
            quote=o.evidence,
            tag=o.kind,
            call_id=o.call_id,
            sort_key=(local_day(o.at), o.at),
        )
        for o in obs
        if o.kind in DAY_KINDS and not (o.kind == "plan" and APPOINTMENT_WORDS.search(o.text))
    ]
    day_items.sort(key=lambda i: i.sort_key, reverse=True)
    if latest:
        when = local_day(latest.started_at or latest.ended_at)
        line = _first_sentence(latest.summary)
        if when != today:
            line = f"{_day_label(when, today)}: {line}"
    else:
        line = "No calls yet."
    cats["day"] = Category(
        key="day",
        title=f"{who}'s day",
        blurb="Meals, visitors, what she did",
        line=line,
        count=len([i for i in day_items if i.when > week_ago]),
        items=day_items,
        empty=(
            f"Nothing noted yet. After a few calls, what {who} eats, who visits and what she "
            "does will appear here."
        ),
    )

    # -- Health ------------------------------------------------------------------------
    health = [o for o in obs if o.kind in HEALTH_KINDS]
    bad_this_week = [
        o
        for o in health
        if local_day(o.at) > week_ago
        and (
            o.kind in ("pain", "mobility", "memory")
            or (o.kind == "sleep" and o.value in ("poor", "bad"))
            or (o.kind == "mood" and o.value in ("low", "sad", "lonely", "poor"))
        )
    ]
    parts = []
    n = sum(1 for o in bad_this_week if o.kind == "pain")
    if n:
        parts.append(f"{_plural(n, 'mention')} of pain")
    n = sum(1 for o in bad_this_week if o.kind == "sleep")
    if n:
        parts.append(f"{_plural(n, 'poor night')}")
    n = sum(1 for o in bad_this_week if o.kind == "mood")
    if n:
        parts.append(f"{_plural(n, 'low day')}")
    n = sum(1 for o in bad_this_week if o.kind == "mobility")
    if n:
        parts.append(f"{_plural(n, 'note')} on getting about")
    n = sum(1 for o in bad_this_week if o.kind == "memory")
    if n:
        parts.append(f"{_plural(n, 'moment')} of confusion")
    if parts:
        line = _cap(", ".join(parts[:3])) + " this week."
        tone = "warn"
    elif health:
        line = "Nothing worrying this week."
        tone = "ok"
    else:
        line = "Nothing noted yet."
        tone = ""
    health_items = sorted(
        (
            Item(
                when=local_day(o.at),
                text=_cap(o.text),
                quote=o.evidence,
                tag=f"{o.kind}{': ' + o.value if o.value else ''}",
                tone="bad" if o in bad_this_week else "",
                call_id=o.call_id,
                sort_key=(local_day(o.at), o.at),
            )
            for o in health
        ),
        key=lambda i: i.sort_key,
        reverse=True,
    )
    cats["health"] = Category(
        key="health",
        title="Health",
        blurb="Pain, sleep, mood, in her words",
        line=line,
        tone=tone,
        count=len(bad_this_week),
        items=health_items,
        empty=(
            "Nothing noted. Sunday records what she says about pain, sleep and mood, and never "
            "guesses."
        ),
    )

    # -- Medication --------------------------------------------------------------------
    meds = [o for o in obs if o.kind == "medication"]
    by_day: dict[date, str] = {}
    for o in sorted(meds, key=lambda o: o.at):
        d = local_day(o.at)
        v = o.value or "unclear"
        # A later "taken" the same day wins over an earlier "missed" (she took it on the call).
        if by_day.get(d) != "taken":
            by_day[d] = v
    last7 = [today - timedelta(days=i) for i in range(7)]
    taken7 = sum(1 for d in last7 if by_day.get(d) == "taken")
    asked7 = sum(1 for d in last7 if d in by_day)
    today_v = by_day.get(today)
    if not parent.medications:
        line, tone = "No medications listed. Add them in Settings.", ""
    elif today_v == "taken":
        line, tone = f"Taken today. {taken7} of the last {asked7 or 7} days.", "ok"
    elif today_v == "missed":
        line, tone = f"Missed today. {taken7} of the last {asked7 or 7} days.", "bad"
    elif today_v:
        line, tone = f"Unclear today. {taken7} of the last {asked7 or 7} days.", "warn"
    elif asked7:
        line, tone = f"Not asked yet today. Taken {taken7} of the last {asked7} days.", ""
    else:
        line, tone = "Not asked yet.", ""
    med_items = [
        Item(
            when=local_day(o.at),
            text=_cap(o.text),
            quote=o.evidence,
            tag=o.value or "unclear",
            tone={"taken": "ok", "missed": "bad"}.get(o.value or "", "warn"),
            call_id=o.call_id,
            sort_key=(local_day(o.at), o.at),
        )
        for o in meds
    ]
    med_items.sort(key=lambda i: i.sort_key, reverse=True)
    cats["medication"] = Category(
        key="medication",
        title="Medication",
        blurb=", ".join(m.get("name", "") for m in (parent.medications or []))
        or "What the call asks about",
        line=line,
        tone=tone,
        count=sum(1 for d in last7 if by_day.get(d) == "missed"),
        items=med_items,
        empty="Sunday asks every morning and records taken, missed or unclear from what she says.",
    )

    # -- Appointments ------------------------------------------------------------------
    appt_items = []
    for m in queued:
        if m.remind_on and m.remind_on >= today:
            appt_items.append(
                Item(
                    when=m.remind_on,
                    text=_cap(m.text),
                    tag=f"from {m.from_name}",
                    action=f"remove:{m.id}",
                    sort_key=(m.remind_on,),
                )
            )
    for o in obs:
        if o.kind == "plan" and APPOINTMENT_WORDS.search(o.text):
            appt_items.append(
                Item(
                    when=local_day(o.at),
                    text=_cap(o.text),
                    quote=o.evidence,
                    tag="she mentioned",
                    call_id=o.call_id,
                    sort_key=(local_day(o.at),),
                )
            )
    upcoming = sorted((i for i in appt_items if i.when >= today), key=lambda i: i.when)
    past = sorted((i for i in appt_items if i.when < today), key=lambda i: i.when, reverse=True)
    if upcoming:
        nxt = upcoming[0]
        when = (
            _day_label(nxt.when, today)
            if nxt.when - today < timedelta(days=7)
            else nxt.when.strftime("%a %d %b")
        )
        line = f"{nxt.text}. {when}."
        tone = ""
    else:
        line, tone = "Nothing coming up.", "ok"
    cats["appointments"] = Category(
        key="appointments",
        title="Appointments",
        blurb="Sunday reminds her on the day",
        line=line,
        tone=tone,
        count=len(upcoming),
        items=upcoming + past,
        empty="Nothing coming up. Add an appointment and Sunday will remind her on the day.",
    )

    # -- Needs you ---------------------------------------------------------------------
    open_alerts = [a for a in alerts if not a.acknowledged_by]
    need_items = [
        Item(
            when=local_day(a.at),
            text=a.text,
            quote=a.evidence,
            tag=TIER_WORD.get(a.tier, a.tier),
            tone={"now": "bad", "today": "warn"}.get(a.tier, ""),
            call_id=a.call_id or "",
            action=f"ack:{a.id}",
            sort_key=({"now": 2, "today": 1}.get(a.tier, 0), a.at),
        )
        for a in open_alerts
    ]
    need_items.sort(key=lambda i: i.sort_key, reverse=True)
    n_now = sum(1 for a in open_alerts if a.tier == "now")
    n_today = sum(1 for a in open_alerts if a.tier == "today")
    if n_now or n_today:
        bits = []
        if n_now:
            bits.append(f"{n_now} urgent")
        if n_today:
            bits.append(f"{n_today} to check today")
        line, tone = _cap(", ".join(bits)) + ".", "bad" if n_now else "warn"
    elif open_alerts:
        line, tone = f"{_plural(len(open_alerts), 'small thing')} to note.", ""
    else:
        line, tone = "Nothing right now.", "ok"
    seen = sorted(
        (
            Item(
                when=local_day(a.at),
                text=a.text,
                quote=a.evidence,
                tag=f"seen by {a.acknowledged_by}",
                call_id=a.call_id or "",
                sort_key=(a.at,),
            )
            for a in alerts
            if a.acknowledged_by
        ),
        key=lambda i: i.sort_key,
        reverse=True,
    )
    cats["needs"] = Category(
        key="needs",
        title="Needs you",
        blurb="Alerts and missed mornings",
        line=line,
        tone=tone,
        count=len(open_alerts),
        items=need_items + seen,
        empty=(
            "Nothing right now. Urgent things reach your phone the moment they happen; this is "
            "where they are kept."
        ),
    )

    # -- Follow-ups --------------------------------------------------------------------
    follow = [o for o in obs if o.follow_up and local_day(o.at) > week_ago]
    fu_items = [
        Item(
            when=local_day(o.at),
            text=_cap(o.text),
            quote=o.evidence,
            tag="Sunday will ask",
            tone="",
            call_id=o.call_id,
            sort_key=(1, o.at),
        )
        for o in follow
    ]
    for m in queued:
        if not m.remind_on or m.remind_on < today:
            kind = {"ask": "ask", "remind": "remind", "say": "tell"}.get(m.kind, "tell")
            fu_items.append(
                Item(
                    when=m.created_at.date(),
                    text=_cap(m.text),
                    tag=f"{kind} · from {m.from_name}",
                    action=f"remove:{m.id}",
                    sort_key=(0, m.created_at),
                )
            )
    fu_items.sort(key=lambda i: i.sort_key, reverse=True)
    if follow:
        line = f"Sunday will ask about: {follow[-1].text.rstrip('.')}."
    elif fu_items:
        line = f"{_plural(len(fu_items), 'thing')} queued for the next call."
    else:
        line = "Nothing open."
    cats["followups"] = Category(
        key="followups",
        title="Follow-ups",
        blurb="What Sunday asks next, what you've queued",
        line=line,
        tone="ok" if not fu_items else "",
        count=len(fu_items),
        items=fu_items,
        empty=(
            "Nothing open. Things she mentions that are unfinished, and anything the family "
            "queues, live here."
        ),
    )
    return cats


def headline(parent: Parent, cats: dict[str, Category], latest, latest_local) -> str:
    """The one sentence under the greeting."""
    who = parent.address()
    if latest is None:
        return f"Sunday hasn't spoken to {who} yet."
    today = parent.local_today()
    mood = {"bright": "sounded bright", "ok": "sounded okay", "low": "sounded a bit low"}.get(
        latest.mood or "", "spoke to Sunday"
    )
    d = latest_local.date()
    when = (
        "this morning"
        if d == today and latest_local.hour < 12
        else "today"
        if d == today
        else "yesterday"
        if d == today - timedelta(days=1)
        else f"on {latest_local:%A}"
    )
    needs = cats["needs"]
    tail = (
        "Nothing needs you right now."
        if needs.count == 0
        else f"{needs.line.rstrip('.')} needs you."
    )
    if needs.count == 1 and needs.line.startswith("1 "):
        tail = f"One thing needs you: {needs.items[0].text.rstrip('.').lower()}."
    return f"{who} {mood} {when}. {tail}"
