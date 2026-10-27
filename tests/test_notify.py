"""Tests for the after-call pipeline, messengers and the weekly digest."""

import json
from datetime import UTC, datetime

import pytest

from sunday.brain.model import ScriptedChat
from sunday.brain.safety import Signal
from sunday.calls.session import CallSession
from sunday.calls.transport import Simulator
from sunday.config import Settings
from sunday.notify.digest import compute_trends, format_digest
from sunday.notify.messenger import ConsoleMessenger, wants
from sunday.notify.pipeline import finish_call, format_alert, notify_no_answer
from sunday.repository import Repository


@pytest.fixture
def world():
    repo = Repository(":memory:")
    parent = repo.add_parent(
        name="Fatima Zaidi",
        preferred_name="Amma",
        phone="+15551234567",
        medications=[{"name": "BP tablet", "when": "morning"}],
    )
    repo.add_family_member(
        parent.id,
        name="Daniyal",
        relation="son",
        phone="+15550001",
        channel="whatsapp",
        min_alert_tier="info",
        is_primary=True,
    )
    repo.add_family_member(
        parent.id,
        name="Hira",
        relation="daughter",
        phone="+15550002",
        channel="sms",
        min_alert_tier="now",
    )  # only wants emergencies
    msg = repo.queue_message(parent.id, from_name="Hira", text="I'll call Thursday after work.")
    return repo, parent, msg


def _analysis(**over):
    base = {
        "summary": "Amma sounded okay. Took her BP tablet. Knee still bothering her; plumber "
        "still hasn't come.",
        "mood": "ok",
        "observations": [
            {
                "kind": "medication",
                "text": "took BP tablet",
                "value": "taken",
                "evidence": "Yes I took it",
            },
            {
                "kind": "household",
                "text": "plumber has not come",
                "follow_up": True,
                "evidence": "plumber still hasn't come",
            },
        ],
        "alerts": [],
        "memories": [
            {
                "text": "Goes to Farida's on Wednesdays.",
                "topic": "social",
                "evidence": "Farida on Wednesdays",
            }
        ],
        "message_replies": [],
    }
    base.update(over)
    return json.dumps(base)


def _completed_call(repo, parent, lines: list[str]):
    call = repo.create_call(
        parent.id, status="completed", started_at=datetime.now(UTC), ended_at=datetime.now(UTC)
    )
    for i, line in enumerate(lines):
        repo.add_turn(call.id, "sunday" if i % 2 == 0 else "parent", line)
    return call


def test_finish_call_stores_and_notifies_respecting_tiers(world):
    repo, parent, msg = world
    call = _completed_call(
        repo,
        parent,
        [
            "Hello Amma",
            "Yes I took it. The plumber still hasn't come. I see Farida on Wednesdays.",
            "Hira says she'll call Thursday",
            "Tell her after eight",
        ],
    )
    model = ScriptedChat(
        script=[_analysis(message_replies=[{"message_id": msg.id, "response": "After eight."}])]
    )
    messenger = ConsoleMessenger(echo=False)
    analysis = finish_call(
        repo, model, messenger, call.id, signals=[], delivered_message_ids={msg.id}
    )
    assert analysis is not None and analysis.mood == "ok"

    stored = repo.get_call(call.id)
    assert stored.summary.startswith("Amma sounded okay") and stored.mood == "ok"
    assert stored.notified_at is not None
    assert [o.kind for o in stored.observations] == ["medication", "household"]
    assert repo.pending_follow_ups(parent.id)[0].text == "plumber has not come"
    assert repo.memories_for(parent.id)[0].text == "Goes to Farida's on Wednesdays."
    assert repo.undelivered_messages(parent.id) == []
    # Only Daniyal (min tier info) gets the summary; Hira only wants emergencies.
    assert [name for name, _ in messenger.sent] == ["Daniyal"]
    text = messenger.sent[0][1]
    assert "Sunday called Amma" in text and "sounded okay" in text
    assert "Tomorrow I'll ask about: plumber has not come" in text
    assert "- After eight." in text

    # Idempotent: a retry sends nothing twice.
    assert (
        finish_call(repo, model, messenger, call.id, signals=[], delivered_message_ids=set())
        is None
    )
    assert len(messenger.sent) == 1


def test_emergency_alert_goes_out_before_analysis_to_everyone(world):
    repo, parent, _ = world
    call = _completed_call(repo, parent, ["Hello Amma", "I fell and I can't get up"])
    repo.add_alert(
        parent.id,
        call_id=call.id,
        tier="now",
        kind="fall",
        text="Amma said they fell. Please check on them immediately.",
        evidence="I fell and I can't get up",
    )
    order: list[str] = []

    def slow_model(msgs):
        order.append("model")
        return _analysis(
            summary="Fall reported.",
            alerts=[{"tier": "now", "kind": "fall", "text": "Fall.", "evidence": "I fell"}],
        )

    class Recorder(ConsoleMessenger):
        def send(self, member, text):
            order.append(f"send:{member.name}:{text.splitlines()[0]}")
            return super().send(member, text)

    messenger = Recorder(echo=False)
    finish_call(
        repo,
        ScriptedChat(script=[slow_model]),
        messenger,
        call.id,
        signals=[Signal("fall", "now", "I fell and I can't get up")],
        delivered_message_ids=set(),
    )
    # Both family members get the URGENT alert, and it goes out before the model runs.
    assert order[:2] == [
        "send:Daniyal:URGENT - Sunday about Amma",
        "send:Hira:URGENT - Sunday about Amma",
    ]
    assert order[2] == "model"
    urgent = [t for _, t in messenger.sent if t.startswith("URGENT")]
    assert len(urgent) == 2 and "staying on the line" in urgent[0]
    assert 'They said: "I fell and I can\'t get up"' in urgent[0]
    # The same (kind, tier) is not stored twice by the analysis.
    assert len([a for a in repo.alerts_since(parent.id, days=1) if a.kind == "fall"]) == 1


def test_silent_call_is_recorded_without_model(world):
    repo, parent, _ = world
    call = _completed_call(repo, parent, ["Hello Amma"])
    model = ScriptedChat(script=["should not run"])
    out = finish_call(
        repo, model, ConsoleMessenger(echo=False), call.id, signals=[], delivered_message_ids=set()
    )
    assert out is None and model.calls == []
    assert "nothing was heard" in repo.get_call(call.id).summary


def test_messenger_failure_does_not_block_others(world):
    repo, parent, _ = world
    call = _completed_call(repo, parent, ["Hi", "fine thanks"])
    repo.add_alert(parent.id, call_id=call.id, tier="now", kind="emergency", text="Help asked.")

    class Flaky(ConsoleMessenger):
        def send(self, member, text):
            if member.name == "Daniyal":
                raise RuntimeError("number unreachable")
            return super().send(member, text)

    m = Flaky(echo=False)
    finish_call(
        repo,
        ScriptedChat(script=[_analysis()]),
        m,
        call.id,
        signals=[],
        delivered_message_ids=set(),
    )
    assert [n for n, _ in m.sent] == ["Hira"]  # Hira still got the emergency
    assert repo.unnotified_alerts(parent.id) == []


def test_no_answer_notification_and_wants(world):
    repo, parent, _ = world
    repo.add_alert(
        parent.id, tier="today", kind="no_answer", text="Amma did not answer today or yesterday."
    )
    m = ConsoleMessenger(echo=False)
    assert notify_no_answer(repo, m, parent.id) == 1  # Daniyal only (Hira wants 'now')
    assert "Please check today" in m.sent[0][1]
    fam = repo.family_for(parent.id)
    assert wants(fam[0], "info") and not wants(fam[1], "today") and wants(fam[1], "now")


def test_format_alert_tiers(world):
    repo, parent, _ = world
    a = repo.add_alert(
        parent.id, tier="today", kind="pain", text="Knee pain again.", evidence="my knee hurts"
    )
    text = format_alert(parent, a)
    assert text.startswith("Please check today") and "staying on the line" not in text


def test_simulated_call_end_to_end_into_pipeline(world):
    """A scripted call through the session, then the pipeline, then the family note."""
    repo, parent, msg = world
    settings = Settings(_env_file=None, SUNDAY_PROVIDER="fake")
    prog = lambda c=(), d=(), t=False: json.dumps(  # noqa: E731
        {
            "covered": list(c),
            "delivered_messages": list(d),
            "parent_sounds_tired_or_wants_to_stop": t,
        }
    )
    model = ScriptedChat(
        script=[
            prog(["wellbeing"]),
            "Lovely. Did you take your BP tablet?",
            prog(["medication"]),
            "Good. Hira says she'll call Thursday after work.",
            prog(["sleep", "meals", "follow_ups", "family_messages", "open_talk"], [msg.id]),
            _analysis(message_replies=[{"message_id": msg.id, "response": "After eight is best."}]),
        ]
    )
    session = CallSession(repo, model, settings, parent)
    sim = Simulator(session).run_scripted(["I'm fine beta", "Yes I took it", "After eight is best"])
    assert sim.ended
    messenger = ConsoleMessenger(echo=False)
    analysis = finish_call(
        repo,
        model,
        messenger,
        session.call.id,
        signals=session.safety.signals,
        delivered_message_ids=session.ctx.delivered_message_ids,
    )
    assert analysis is not None
    assert "After eight is best." in messenger.sent[0][1]
    assert repo.undelivered_messages(parent.id) == []


# --------------------------------------------------------------- digest


def test_trends_and_digest(world):
    repo, parent, _ = world
    for i, (mood, med, sleep) in enumerate(
        [
            ("ok", "taken", "ok"),
            ("low", "missed", "poor"),
            ("ok", "missed", "poor"),
            ("low", "taken", "poor"),
        ]
    ):
        c = repo.create_call(
            parent.id,
            status="completed",
            started_at=datetime.now(UTC),
            ended_at=datetime.now(UTC),
            mood=mood,
        )
        repo.add_observations(
            c.id,
            parent.id,
            [
                {"kind": "medication", "text": "BP tablet", "value": med},
                {"kind": "sleep", "text": "sleep", "value": sleep},
                {"kind": "pain", "text": "knee pain", "value": ""},
                {"kind": "household", "text": "plumber", "follow_up": i == 3},
            ],
        )
    repo.create_call(parent.id, status="no_answer", started_at=datetime.now(UTC))
    repo.create_call(parent.id, status="no_answer", started_at=datetime.now(UTC))

    t = compute_trends(repo, parent, days=7)
    assert (t.calls_completed, t.calls_missed) == (4, 2)
    assert (t.medication_taken, t.medication_missed) == (2, 2)
    assert t.sleep_poor_days == 3 and t.pain_mentions == 4
    assert t.mood_counts["low"] == 2
    assert t.open_follow_ups == ["plumber"]
    flags = " ".join(t.worth_a_look)
    assert "missed medication 2" in flags and "Poor sleep on 3" in flags
    assert "Pain came up 4" in flags and "2 calls went unanswered" in flags

    text = format_digest(parent, t)
    assert text.startswith("Sunday - Amma's week")
    assert "Calls: 4 answered, 2 missed." in text and "Worth a look:" in text
    assert "not a doctor" in text


def test_quiet_week_digest(world):
    repo, parent, _ = world
    c = repo.create_call(parent.id, status="completed", started_at=datetime.now(UTC), mood="bright")
    repo.add_observations(c.id, parent.id, [{"kind": "medication", "text": "x", "value": "taken"}])
    text = format_digest(parent, compute_trends(repo, parent))
    assert "Nothing worrying this week." in text and "1 bright" in text


def test_services_send_urgent_alert_while_the_call_is_still_going(world):
    from sunday.app import Services
    from sunday.calls.dialer import FakeTelephony

    repo, parent, settings = world
    messenger = ConsoleMessenger(echo=False)
    svc = Services(
        settings=settings,
        repo=repo,
        model=ScriptedChat(
            script=[json.dumps({"summary": "Fall reported.", "mood": "low", "alerts": []})]
        ),
        messenger=messenger,
        telephony=FakeTelephony(),
    )
    session = svc.new_session(parent)
    assert session.emergency_number == "911"  # fixture parent is a +1 number
    session.start()
    session.hear("I fell in the bathroom")
    # The call has not ended and finish_call has not run, yet both family members have it.
    assert sorted(n for n, _ in messenger.sent) == ["Daniyal", "Hira"]
    assert all(t.startswith("URGENT - Sunday about Amma") for _, t in messenger.sent)
    assert repo.unnotified_alerts(parent.id) == []
    session.hear("my hip")
    assert len(messenger.sent) == 2  # not re-sent on later turns
    session.hangup()
    svc.complete(session)
    urgent = [t for _, t in messenger.sent if t.startswith("URGENT")]
    assert len(urgent) == 2  # finish_call did not send the same alert again


def test_digest_leads_with_alerts(world):
    repo, parent, _ = world
    c = repo.create_call(
        parent.id, status="completed", started_at=datetime.now(UTC), ended_at=datetime.now(UTC)
    )
    repo.add_alert(parent.id, call_id=c.id, tier="now", kind="fall", text="Amma said they fell.")
    repo.add_alert(parent.id, call_id=c.id, tier="today", kind="pain", text="Pain.")
    repo.add_alert(parent.id, call_id=c.id, tier="today", kind="no_food", text="No food.")
    t = compute_trends(repo, parent, days=7)
    assert t.alerts_now == 1 and t.alerts_today == 2
    text = format_digest(parent, t)
    assert "Nothing worrying" not in text
    assert "Alerts: 1 urgent, 2 check-today." in text
    assert "URGENT on" in text and "Amma said they fell." in text
    assert "2 'check today' alerts: pain, no food." in text
