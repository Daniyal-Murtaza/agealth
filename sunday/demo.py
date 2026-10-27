"""Seed a believable fortnight so the family page can be shown before the first real call.

``sunday demo`` creates Amma, three family members, and fourteen days of
calls with notes, observations, two alerts, a missed morning, memories and a
queue for tomorrow. Everything is plainly labelled as demo data; it never
touches a real parent. Deterministic, no model, no network.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sunday.repository import Repository

PARENT_PHONE = "+92 300 0000 001"
FAMILY = [
    # name, relation, phone, tier, primary
    ("Daniyal", "son", "+1 555 010 0001", "info", True),
    ("Hira", "daughter", "+1 555 010 0002", "now", False),
    ("Jamal", "son", "+44 7700 900 003", "today", False),
]

# (days ago, mood, summary, observations, alerts, transcript)
DAYS: list[dict] = [
    dict(
        ago=13,
        mood="ok",
        summary=(
            "Amma sounded okay. Took her tablet with chai. Slept alright. Asked after "
            "Ayaan's school."
        ),
        obs=[("medication", "took blood pressure tablet", "taken", "took it with my chai", False)],
    ),
    dict(
        ago=12,
        mood="bright",
        summary=(
            "Bright this morning. Farida came by with halwa. Tablet taken. Planning to sit "
            "in the garden."
        ),
        obs=[
            ("medication", "took blood pressure tablet", "taken", "yes beta, taken", False),
            ("social", "Farida visited with halwa", "", "Farida brought halwa", False),
        ],
    ),
    dict(
        ago=11,
        mood="ok",
        summary=(
            "Okay. Slept a bit late. Tablet taken. The tap in the kitchen has started dripping."
        ),
        obs=[
            ("medication", "took blood pressure tablet", "taken", "I took it", False),
            ("sleep", "slept late", "ok", "slept a bit late", False),
            ("household", "kitchen tap dripping", "", "the tap is dripping", True),
        ],
    ),
    dict(ago=10, missed=True),
    dict(
        ago=9,
        mood="low",
        summary=(
            "A bit low today. Said the house is quiet. Tablet taken. The plumber has not come yet."
        ),
        obs=[
            ("medication", "took blood pressure tablet", "taken", "haan, le li", False),
            ("mood", "house feels quiet", "low", "the house is so quiet these days", False),
            ("household", "plumber has not come", "", "the plumber still hasn't come", True),
        ],
    ),
    dict(
        ago=8,
        mood="ok",
        summary=(
            "Okay. Knee bothering her a little on the stairs. Tablet taken. Plumber came "
            "and fixed the tap."
        ),
        obs=[
            ("medication", "took blood pressure tablet", "taken", "took it", False),
            (
                "pain",
                "knee bothering her on stairs",
                "mild",
                "my knee is bothering me a little on the stairs",
                False,
            ),
            ("household", "plumber came, tap fixed", "", "the plumber came, it's fixed", False),
        ],
    ),
    dict(
        ago=7,
        mood="ok",
        summary=(
            "Okay. Forgot her tablet this morning and took it during the call. Talked about "
            "Zara's drawing."
        ),
        obs=[
            (
                "medication",
                "forgot tablet, took it during the call",
                "missed",
                "oh, I forgot it, let me take it now",
                False,
            ),
        ],
    ),
    dict(
        ago=6,
        mood="ok",
        summary=(
            "Knee hurt a lot today, she could barely get to the kitchen. Tablet taken. Hira "
            "is visiting Thursday."
        ),
        obs=[
            ("medication", "took blood pressure tablet", "taken", "yes taken", False),
            (
                "pain",
                "severe knee pain, difficulty walking",
                "severe",
                "my knee hurts so much today I could barely walk to the kitchen",
                False,
            ),
        ],
        alerts=[
            (
                "today",
                "pain",
                "Amma mentioned significant pain today.",
                "my knee hurts so much today I could barely walk to the kitchen",
            )
        ],
    ),
    dict(
        ago=5,
        mood="ok",
        summary=(
            "Knee a little better. Tablet taken. Looking forward to Hira's visit. Asked "
            "Sunday to remind her about the clinic."
        ),
        obs=[
            ("medication", "took blood pressure tablet", "taken", "le li beta", False),
            ("pain", "knee a little better", "mild", "the knee is a bit better today", False),
        ],
    ),
    dict(
        ago=4,
        mood="bright",
        summary=(
            "Bright. Hira visited with the children. Tablet taken. Ate well: biryani for lunch."
        ),
        obs=[
            ("medication", "took blood pressure tablet", "taken", "yes, with breakfast", False),
            ("meal", "biryani for lunch", "", "we had biryani", False),
            ("social", "Hira and the children visited", "", "Hira came with the children", False),
        ],
    ),
    dict(
        ago=3,
        mood="ok",
        summary=(
            "Okay. Tablet taken. Slept well. Said yes to the lift to the clinic on Tuesday, "
            "after eleven."
        ),
        obs=[
            ("medication", "took blood pressure tablet", "taken", "taken", False),
            ("sleep", "slept well", "good", "slept well", False),
        ],
    ),
    dict(
        ago=2,
        mood="ok",
        summary=(
            "Clinic went fine, Dr Khan is happy with the blood pressure. Tablet taken. "
            "Tired afterwards."
        ),
        obs=[
            (
                "medication",
                "took blood pressure tablet",
                "taken",
                "yes I took it before we left",
                False,
            ),
            (
                "plan",
                "clinic visit done, doctor happy",
                "",
                "the doctor said the pressure is fine",
                False,
            ),
        ],
    ),
    dict(
        ago=1,
        mood="ok",
        summary=(
            "Okay. Tablet taken. Slept a bit late. Wants to know when Jamal is next calling "
            "from London."
        ),
        obs=[
            ("medication", "took blood pressure tablet", "taken", "took it with chai", False),
            ("sleep", "slept late", "ok", "slept a bit late", False),
            ("social", "asked when Jamal will call", "", "when is Jamal calling", True),
        ],
    ),
    dict(
        ago=0,
        mood="ok",
        summary=(
            "Okay this morning. Took her tablet with chai. Paratha for breakfast. Asked "
            "after Zara's cough."
        ),
        obs=[
            (
                "medication",
                "took blood pressure tablet",
                "taken",
                "took my tablet with chai",
                False,
            ),
            ("meal", "paratha and chai", "", "just paratha and chai", False),
        ],
    ),
]

MEMORIES = [
    ("Grandchildren are Ayaan (7) and Zara (4).", "family"),
    ("Neighbour Farida visits most Wednesdays, usually with something sweet.", "social"),
    ("Her knee has bothered her since the winter; stairs are the hard part.", "health"),
    ("Dr Khan at the clinic looks after her blood pressure.", "health"),
    ("Jamal lives in London and calls on Sundays.", "family"),
    ("Likes to sit in the garden after breakfast when it is not too hot.", "likes"),
]

TURNS_TEMPLATE = [
    (
        "sunday",
        "Hello Amma, it's Sunday calling, the daily call from your son Daniyal. How are you today?",
    ),
    ("parent", "I'm alright beta. {first}"),
    ("sunday", "I'm glad to hear it. {second}"),
    ("parent", "{third}"),
    ("sunday", "That sounds good. Did you sleep alright last night?"),
    ("parent", "Slept okay, a bit late."),
    ("sunday", "Have you eaten something this morning?"),
    ("parent", "Just paratha and chai."),
    ("sunday", "Lovely. What are you planning to do today?"),
    ("parent", "Sit in the garden for a while if it's not too hot."),
    (
        "sunday",
        "It was lovely talking to you, Amma. I'll let the family know you're well. Speak tomorrow.",
    ),
]


def seed_demo(repo: Repository) -> str:
    """Create the demo parent and history. Returns the parent id. Safe to re-run: if the demo
    parent already exists it is returned untouched."""
    existing = repo.parent_by_phone(PARENT_PHONE)
    if existing is not None:
        return existing.id

    parent = repo.add_parent(
        name="Fatima Zaidi",
        preferred_name="Amma",
        phone=PARENT_PHONE,
        language="en",
        timezone="Asia/Karachi",
        call_time="09:00",
        medications=[{"name": "blood pressure tablet", "when": "morning"}],
        introduced_by="your son Daniyal",
        notes="Demo data. Not a real person.",
    )
    for name, relation, phone, tier, primary in FAMILY:
        repo.add_family_member(
            parent.id,
            name=name,
            relation=relation,
            phone=phone,
            channel="console",
            min_alert_tier=tier,
            is_primary=primary,
        )
    for text, topic in MEMORIES:
        repo.add_memory(parent.id, text, topic=topic, source="family")

    now = datetime.now(UTC)
    khi = ZoneInfo("Asia/Karachi")
    today = now.astimezone(khi).date()

    def morning(ago: int, hh: int = 9, mm: int = 4) -> datetime:
        # Anchor on Amma's local day so the fortnight strip is the same at any UTC hour.
        return datetime.combine(today - timedelta(days=ago), time(hh, mm), tzinfo=khi).astimezone(
            UTC
        )

    for day in DAYS:
        at = morning(day["ago"])
        if day.get("missed"):
            c = repo.create_call(
                parent.id, status="no_answer", scheduled_for=at, end_reason="no-answer"
            )
            repo.update_call(c.id, ended_at=at + timedelta(seconds=30))
            continue
        c = repo.create_call(
            parent.id,
            status="completed",
            started_at=at,
            ended_at=at + timedelta(minutes=7),
            mood=day["mood"],
            summary=day["summary"],
            end_reason="completed",
            notified_at=at + timedelta(minutes=8),
        )
        first = day["obs"][0][3].capitalize() + "." if day.get("obs") else "All fine."
        second = "Did you take your blood pressure tablet this morning?"
        third = day["obs"][0][3] if day.get("obs") else "Yes, taken."
        for speaker, text in TURNS_TEMPLATE:
            repo.add_turn(c.id, speaker, text.format(first=first, second=second, third=third))
        with repo.session() as s:
            from sunday.models import Turn

            for i, t in enumerate(s.query(Turn).filter(Turn.call_id == c.id).order_by(Turn.id)):
                t.at = at + timedelta(seconds=20 * i)
        obs = [
            {
                "kind": k,
                "text": t,
                "value": v,
                "evidence": e,
                "follow_up": f,
                "at": at + timedelta(minutes=8),
            }
            for k, t, v, e, f in day.get("obs", [])
        ]
        if obs:
            repo.add_observations(c.id, parent.id, obs)
        for tier, kind, text, evidence in day.get("alerts", []):
            a = repo.add_alert(
                parent.id, call_id=c.id, tier=tier, kind=kind, text=text, evidence=evidence
            )
            with repo.session() as s:
                from sunday.models import Alert

                row = s.get(Alert, a.id)
                row.at = at + timedelta(minutes=3)
                row.notified_at = at + timedelta(minutes=3)
                row.acknowledged_by = "Daniyal" if day["ago"] > 2 else ""

    # The missed morning raised a note that the family has already seen.
    missed = next(d for d in DAYS if d.get("missed"))
    at = morning(missed["ago"], 9, 25)
    a = repo.add_alert(
        parent.id,
        tier="today",
        kind="no_answer",
        text="Amma did not answer this morning. Worth a call.",
    )
    with repo.session() as s:
        from sunday.models import Alert

        row = s.get(Alert, a.id)
        row.at = at
        row.notified_at = at
        row.acknowledged_by = "Hira"

    repo.queue_message(parent.id, from_name="Hira", text="I'll call Thursday after work.")
    repo.queue_message(
        parent.id,
        from_name="Jamal",
        kind="ask",
        text="Would she like us to come for Eid, or come to London?",
    )
    repo.queue_message(
        parent.id,
        from_name="Daniyal",
        kind="remind",
        text="Dr Khan's follow-up is Tuesday at 10. Daniyal will pick you up at 9:30.",
        remind_on=(now + timedelta(days=5)).date(),
    )
    return parent.id
