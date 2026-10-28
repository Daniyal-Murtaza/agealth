"""Tests for the domain model and repository."""

from datetime import UTC, datetime

import pytest

from sunday.repository import Repository


@pytest.fixture
def repo():
    return Repository(":memory:")


@pytest.fixture
def parent(repo):
    return repo.add_parent(
        name="Fatima Zaidi",
        preferred_name="Amma",
        phone="+16145550101",
        language="ur",
        call_time="09:00",
        medications=[{"name": "blood pressure tablet", "when": "morning"}],
        introduced_by="your son Daniyal",
    )


def test_parent_roundtrip_and_address(repo, parent):
    got = repo.get_parent(parent.id)
    assert got.name == "Fatima Zaidi" and got.address() == "Amma"
    assert got.medications[0]["name"] == "blood pressure tablet"
    assert got.call_days == ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    assert repo.list_parents() == [got] or repo.list_parents()[0].id == got.id
    repo.update_parent(parent.id, active=False)
    assert repo.list_parents() == []
    assert repo.get_parent("nope") is None


def test_family_members(repo, parent):
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
        parent.id, name="Hira", relation="daughter", phone="+15550002", min_alert_tier="now"
    )
    fam = repo.family_for(parent.id)
    assert [m.name for m in fam] == ["Daniyal", "Hira"]
    assert fam[0].is_primary and fam[1].min_alert_tier == "now"


def test_call_turns_observations_alerts(repo, parent):
    call = repo.create_call(parent.id, status="in_progress", started_at=datetime.now(UTC))
    repo.add_turn(call.id, "sunday", "Good morning Amma, it's Sunday.")
    repo.add_turn(call.id, "parent", "Good morning beta. I slept badly.")
    repo.add_observations(
        call.id,
        parent.id,
        [
            {"kind": "sleep", "text": "slept badly", "value": "poor", "evidence": "I slept badly"},
            {"kind": "household", "text": "plumber has not come", "follow_up": True},
        ],
    )
    repo.add_alert(
        parent.id, call_id=call.id, tier="today", kind="sleep", text="Poor sleep two nights running"
    )
    repo.update_call(
        call.id,
        status="completed",
        ended_at=datetime.now(UTC),
        summary="Slept badly; plumber still pending.",
        mood="ok",
    )

    got = repo.get_call(call.id)
    assert got.transcript().splitlines()[1] == "parent: Good morning beta. I slept badly."
    assert {o.kind for o in got.observations} == {"sleep", "household"}
    assert got.alerts[0].tier == "today"
    assert repo.last_completed_call(parent.id).id == call.id
    assert [o.text for o in repo.pending_follow_ups(parent.id)] == ["plumber has not come"]
    assert len(repo.observations_since(parent.id, days=1)) == 2
    assert len(repo.recent_calls(parent.id, days=1)) == 1

    assert [a.id for a in repo.unnotified_alerts(parent.id)] == [got.alerts[0].id]
    repo.mark_alert_notified(got.alerts[0].id)
    assert repo.unnotified_alerts(parent.id) == []
    assert len(repo.alerts_since(parent.id, days=1)) == 1


def test_consecutive_no_answers(repo, parent):
    t = datetime.now(UTC)
    repo.create_call(parent.id, status="completed", scheduled_for=t.replace(day=1))
    repo.create_call(parent.id, status="no_answer", scheduled_for=t.replace(day=2))
    repo.create_call(parent.id, status="no_answer", scheduled_for=t.replace(day=3))
    assert repo.consecutive_no_answers(parent.id) == 2
    repo.create_call(parent.id, status="completed", scheduled_for=t.replace(day=4))
    assert repo.consecutive_no_answers(parent.id) == 0


def test_queued_messages_and_memories(repo, parent):
    m1 = repo.queue_message(parent.id, from_name="Hira", text="I'll call Thursday after work.")
    m2 = repo.queue_message(
        parent.id,
        from_name="Daniyal",
        kind="ask",
        text="Does she want a lift to the clinic on Tuesday?",
    )
    assert [m.id for m in repo.undelivered_messages(parent.id)] == [m1.id, m2.id]
    repo.mark_message_delivered(m1.id, "call_x")
    repo.mark_message_delivered(m2.id, "call_x", response="Yes please, at 9.")
    assert repo.undelivered_messages(parent.id) == []

    repo.add_memory(
        parent.id, "Grandchildren are Ayaan (7) and Zara (4).", topic="family", source="family"
    )
    repo.add_memory(
        parent.id,
        "Neighbour Farida visits on Wednesdays.",
        topic="social",
        source="call_x",
        evidence="Farida came by, she comes every Wednesday",
    )
    mems = repo.memories_for(parent.id)
    assert len(mems) == 2 and mems[1].evidence.startswith("Farida")


def test_parent_by_phone_matches_on_digits(repo, parent):
    assert repo.parent_by_phone("+1 (614) 555-0101").id == parent.id
    assert repo.parent_by_phone("16145550101").id == parent.id
    assert repo.parent_by_phone("+15550000000") is None
    assert repo.parent_by_phone("") is None


def test_dated_reminders_are_held_until_their_day(repo, parent):
    from datetime import date, timedelta

    today = date(2026, 10, 27)
    repo.queue_message(parent.id, from_name="Hira", text="I'll call Thursday.")
    appt = repo.queue_message(
        parent.id,
        from_name="Daniyal",
        text="Your appointment is Tuesday at 10 with Dr Khan.",
        kind="remind",
        remind_on=today + timedelta(days=1),
    )
    assert [m.from_name for m in repo.undelivered_messages(parent.id, on=today)] == ["Hira"]
    assert len(repo.undelivered_messages(parent.id, on=today + timedelta(days=1))) == 2
    assert len(repo.undelivered_messages(parent.id, on=today + timedelta(days=5))) == 2
    assert len(repo.undelivered_messages(parent.id, include_future=True)) == 2
    assert repo.undelivered_messages(parent.id, include_future=True)[1].id == appt.id


def test_new_columns_are_added_to_an_old_database(tmp_path):
    from sqlalchemy import inspect, text

    path = tmp_path / "old.sqlite"
    first = Repository(path)
    with first.engine.begin() as conn:
        conn.execute(text("ALTER TABLE queued_messages DROP COLUMN remind_on"))
        conn.execute(text("ALTER TABLE parents DROP COLUMN last_digest_at"))
    assert "remind_on" not in {
        c["name"] for c in inspect(first.engine).get_columns("queued_messages")
    }
    first.engine.dispose()

    second = Repository(path)  # opening again migrates in place
    cols = {c["name"] for c in inspect(second.engine).get_columns("queued_messages")}
    assert "remind_on" in cols
    assert "last_digest_at" in {c["name"] for c in inspect(second.engine).get_columns("parents")}
    p = second.add_parent(name="A", phone="+15550001")
    second.queue_message(p.id, from_name="B", text="hi")
    assert len(second.undelivered_messages(p.id)) == 1
