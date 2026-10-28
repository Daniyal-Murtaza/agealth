"""Seed a believable fortnight so the family page can be shown before the first real call.

``sunday demo`` creates Mom (Rosemary Walker, Columbus, Ohio), three grown
children, and fourteen days of calls with notes, observations, two alerts, a
missed morning, memories and a queue for tomorrow. Everything is plainly
labelled as demo data; it never touches a real parent. Deterministic, no
model, no network.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sunday.repository import Repository

PARENT_PHONE = "+1 614 555 0150"
PARENT_TZ = "America/New_York"
FAMILY = [
    # name, relation, phone, tier, primary
    ("Daniel", "son", "+1 614 555 0151", "info", True),
    ("Sarah", "daughter", "+1 312 555 0152", "now", False),
    ("Michael", "son", "+1 206 555 0153", "today", False),
]

# (days ago, mood, summary, observations, alerts)
DAYS: list[dict] = [
    dict(
        ago=13,
        mood="ok",
        summary=(
            "Mom sounded okay. Took her blood pressure pill with her coffee. Slept alright. "
            "Asked after Emma's soccer."
        ),
        obs=[("medication", "took blood pressure pill", "taken", "took it with my coffee", False)],
    ),
    dict(
        ago=12,
        mood="bright",
        summary=(
            "Bright this morning. Linda from next door came by with banana bread. Pill taken. "
            "Planning to sit on the porch."
        ),
        obs=[
            ("medication", "took blood pressure pill", "taken", "yes honey, took it", False),
            ("social", "Linda visited with banana bread", "", "Linda brought banana bread", False),
        ],
    ),
    dict(
        ago=11,
        mood="ok",
        summary=("Okay. Slept a bit late. Pill taken. The kitchen faucet has started dripping."),
        obs=[
            ("medication", "took blood pressure pill", "taken", "I took it", False),
            ("sleep", "slept late", "ok", "slept a bit late", False),
            ("household", "kitchen faucet dripping", "", "the faucet is dripping", True),
        ],
    ),
    dict(ago=10, missed=True),
    dict(
        ago=9,
        mood="low",
        summary=(
            "A bit low today. Said the house is quiet. Pill taken. The plumber hasn't come yet."
        ),
        obs=[
            ("medication", "took blood pressure pill", "taken", "yes, took it", False),
            ("mood", "house feels quiet", "low", "the house is so quiet these days", False),
            ("household", "plumber has not come", "", "the plumber still hasn't come", True),
        ],
    ),
    dict(
        ago=8,
        mood="ok",
        summary=(
            "Okay. Knee bothering her a little on the stairs. Pill taken. Plumber came and "
            "fixed the faucet."
        ),
        obs=[
            ("medication", "took blood pressure pill", "taken", "took it", False),
            (
                "pain",
                "knee bothering her on stairs",
                "mild",
                "my knee is bothering me a little on the stairs",
                False,
            ),
            ("household", "plumber came, faucet fixed", "", "the plumber came, it's fixed", False),
        ],
    ),
    dict(
        ago=7,
        mood="ok",
        summary=(
            "Okay. Forgot her pill this morning and took it during the call. Talked about "
            "Noah's drawing."
        ),
        obs=[
            (
                "medication",
                "forgot pill, took it during the call",
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
            "Knee hurt a lot today, she could barely get to the kitchen. Pill taken. Sarah is "
            "visiting Thursday."
        ),
        obs=[
            ("medication", "took blood pressure pill", "taken", "yes taken", False),
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
                "Mom mentioned significant pain today.",
                "my knee hurts so much today I could barely walk to the kitchen",
            )
        ],
    ),
    dict(
        ago=5,
        mood="ok",
        summary=(
            "Knee a little better. Pill taken. Looking forward to Sarah's visit. Asked Sunday "
            "to remind her about the clinic."
        ),
        obs=[
            ("medication", "took blood pressure pill", "taken", "took it, honey", False),
            ("pain", "knee a little better", "mild", "the knee is a bit better today", False),
        ],
    ),
    dict(
        ago=4,
        mood="bright",
        summary=(
            "Bright. Sarah visited with the kids. Pill taken. Ate well: pot roast for dinner."
        ),
        obs=[
            ("medication", "took blood pressure pill", "taken", "yes, with breakfast", False),
            ("meal", "pot roast for dinner", "", "we had pot roast", False),
            ("social", "Sarah and the kids visited", "", "Sarah came with the kids", False),
        ],
    ),
    dict(
        ago=3,
        mood="ok",
        summary=(
            "Okay. Pill taken. Slept well. Said yes to the ride to the clinic on Tuesday, "
            "after eleven."
        ),
        obs=[
            ("medication", "took blood pressure pill", "taken", "taken", False),
            ("sleep", "slept well", "good", "slept well", False),
        ],
    ),
    dict(
        ago=2,
        mood="ok",
        summary=(
            "Clinic went fine, Dr. Patel is happy with the blood pressure. Pill taken. Tired "
            "afterwards."
        ),
        obs=[
            (
                "medication",
                "took blood pressure pill",
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
            "Okay. Pill taken. Slept a bit late. Wants to know when Michael is next calling "
            "from Seattle."
        ),
        obs=[
            ("medication", "took blood pressure pill", "taken", "took it with coffee", False),
            ("sleep", "slept late", "ok", "slept a bit late", False),
            ("social", "asked when Michael will call", "", "when is Michael calling", True),
        ],
    ),
    dict(
        ago=0,
        mood="ok",
        summary=(
            "Okay this morning. Took her pill with coffee. Oatmeal for breakfast. Asked after "
            "Noah's cough."
        ),
        obs=[
            (
                "medication",
                "took blood pressure pill",
                "taken",
                "took my pill with coffee",
                False,
            ),
            ("meal", "oatmeal and coffee", "", "just oatmeal and coffee", False),
        ],
    ),
]

MEMORIES = [
    ("Grandchildren are Emma (7) and Noah (4).", "family"),
    ("Linda next door stops by most Wednesdays, usually with something baked.", "social"),
    ("Her knee has bothered her since the winter; stairs are the hard part.", "health"),
    ("Dr. Patel at the clinic on High Street looks after her blood pressure.", "health"),
    ("Michael lives in Seattle and calls on Sundays.", "family"),
    ("Likes to sit on the porch after breakfast when it's not too cold.", "likes"),
    ("Goes to church on Sunday mornings; the call should be after eleven that day.", "routine"),
]

TURNS_TEMPLATE = [
    (
        "sunday",
        "Hello Mom, it's Sunday calling, the daily call from your son Daniel. How are you today?",
    ),
    ("parent", "I'm alright, honey. {first}"),
    ("sunday", "I'm glad to hear it. {second}"),
    ("parent", "{third}"),
    ("sunday", "That sounds good. Did you sleep alright last night?"),
    ("parent", "Slept okay, a bit late."),
    ("sunday", "Have you eaten something this morning?"),
    ("parent", "Just oatmeal and coffee."),
    ("sunday", "Lovely. What are you planning to do today?"),
    ("parent", "Sit on the porch for a while if it's not too cold."),
    (
        "sunday",
        "It was lovely talking to you, Mom. I'll let the family know you're well. Speak tomorrow.",
    ),
]


def seed_demo(repo: Repository) -> str:
    """Create the demo parent and history. Returns the parent id. Safe to re-run: if the demo
    parent already exists it is returned untouched."""
    existing = repo.parent_by_phone(PARENT_PHONE)
    if existing is not None:
        return existing.id

    parent = repo.add_parent(
        name="Rosemary Walker",
        preferred_name="Mom",
        phone=PARENT_PHONE,
        language="en",
        timezone=PARENT_TZ,
        call_time="09:00",
        medications=[{"name": "blood pressure pill", "when": "morning"}],
        introduced_by="your son Daniel",
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
    tz = ZoneInfo(PARENT_TZ)
    today = now.astimezone(tz).date()

    def morning(ago: int, hh: int = 9, mm: int = 4) -> datetime:
        # Anchor on Mom's local day so the fortnight strip is the same at any UTC hour.
        return datetime.combine(today - timedelta(days=ago), time(hh, mm), tzinfo=tz).astimezone(
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
        second = "Did you take your blood pressure pill this morning?"
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
                row.acknowledged_by = "Daniel" if day["ago"] > 2 else ""

    # The missed morning raised a note that the family has already seen.
    missed = next(d for d in DAYS if d.get("missed"))
    at = morning(missed["ago"], 9, 25)
    a = repo.add_alert(
        parent.id,
        tier="today",
        kind="no_answer",
        text="Mom did not answer this morning. Worth a call.",
    )
    with repo.session() as s:
        from sunday.models import Alert

        row = s.get(Alert, a.id)
        row.at = at
        row.notified_at = at
        row.acknowledged_by = "Sarah"

    repo.queue_message(parent.id, from_name="Sarah", text="I'll call Thursday after work.")
    repo.queue_message(
        parent.id,
        from_name="Michael",
        kind="ask",
        text="Would she like to come to Seattle for Thanksgiving, or should we come to her?",
    )
    repo.queue_message(
        parent.id,
        from_name="Daniel",
        kind="remind",
        text="Dr. Patel's follow-up is Tuesday at 10. Daniel will pick you up at 9:30.",
        remind_on=(now + timedelta(days=5)).date(),
    )
    return parent.id
