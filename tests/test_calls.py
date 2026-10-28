"""Tests for the call session, simulator, Twilio transport and dialer."""

import json

import pytest

from sunday.brain.model import ModelError, ScriptedChat
from sunday.calls.dialer import FakeTelephony, dial, record_outcome
from sunday.calls.session import CallSession, Phase
from sunday.calls.transport import (
    Simulator,
    conversation_relay_twiml,
    end_message,
    text_message,
)
from sunday.config import Settings
from sunday.repository import Repository


@pytest.fixture
def world():
    repo = Repository(":memory:")
    parent = repo.add_parent(
        name="Fatima Zaidi",
        preferred_name="Amma",
        phone="+16145550101",
        language="en",
        medications=[{"name": "BP tablet", "when": "morning"}],
        introduced_by="your son Daniyal",
    )
    repo.add_family_member(
        parent.id, name="Daniyal", relation="son", phone="+15550001", is_primary=True
    )
    repo.add_family_member(parent.id, name="Hira", relation="daughter", phone="+15550002")
    repo.add_memory(parent.id, "Neighbour Farida visits on Wednesdays.", topic="social")
    repo.queue_message(parent.id, from_name="Hira", text="I'll call Thursday after work.")
    settings = Settings(_env_file=None, SUNDAY_PROVIDER="fake", SUNDAY_MAX_TURNS=6)
    return repo, parent, settings


def progress(covered=(), delivered=(), tired=False):
    return json.dumps(
        {
            "covered": list(covered),
            "delivered_messages": list(delivered),
            "parent_sounds_tired_or_wants_to_stop": tired,
        }
    )


def test_normal_call_flows_through_plan_and_closes(world):
    repo, parent, settings = world
    msg_id = repo.undelivered_messages(parent.id)[0].id
    model = ScriptedChat(
        script=[
            progress(["wellbeing"]),
            "Glad to hear it. Did you sleep alright?",
            progress(["sleep"]),
            "Good. Did you take your BP tablet this morning?",
            progress(["medication", "meals"]),
            "Hira says she'll call Thursday after work.",
            progress(
                ["family_messages", "follow_ups", "open_talk"], [msg_id]
            ),  # -> only close left
        ]
    )
    session = CallSession(repo, model, settings, parent)
    sim = Simulator(session).run_scripted(
        [
            "I'm well beta, thank you.",
            "Slept fine.",
            "Yes, with my chai.",
            "Tell her after eight.",
        ]
    )
    assert sim.said[0].startswith(
        "Hello Amma, it's Sunday calling, the daily call from your son Daniyal"
    )
    assert "Did you sleep alright?" in sim.said[1]
    assert sim.ended and "lovely talking to you, Amma" in sim.said[-1]
    assert session.phase == Phase.ENDED and not sim.emergency
    call = repo.get_call(session.call.id)
    assert call.status == "completed" and call.end_reason == "completed"
    speakers = [t.speaker for t in call.turns]
    assert speakers[0] == "sunday" and speakers.count("parent") == 4
    system = model.calls[1][0].content
    assert "Farida visits on Wednesdays" in system and "From Hira" in system
    assert session.ctx.delivered_message_ids == {msg_id}


def test_fall_triggers_emergency_mode_immediately_without_model(world):
    repo, parent, settings = world
    model = ScriptedChat(script=[ModelError("should not be called", retryable=False)])
    session = CallSession(repo, model, settings, parent)
    sim = Simulator(session).run_scripted(
        ["I fell in the bathroom and I can't get up", "my hip hurts", "okay", "still here"]
    )
    assert sim.emergency and not sim.ended  # stays on the line
    assert "Stay where you are" in sim.said[1] and "Daniyal, Hira" in sim.said[2]
    assert "911" in sim.said[3] and "Take slow breaths" in sim.said[4]
    assert model.calls == []  # emergency path never consults the model
    alerts = repo.unnotified_alerts(parent.id)
    assert len(alerts) == 1 and alerts[0].tier == "now" and alerts[0].kind == "fall"
    assert "fell" in alerts[0].text and alerts[0].evidence.startswith("I fell")
    call = repo.get_call(session.call.id)
    # The line dropped while in emergency mode: transcript kept, reason recorded.
    assert session.phase == Phase.ENDED and call.status == "completed"
    assert call.end_reason == "script_finished" and len(call.turns) == 9


def test_today_signal_raises_alert_but_call_continues(world):
    repo, parent, settings = world
    model = ScriptedChat(
        script=[
            progress(),
            "I'm sorry the knee hurts. Have you told the doctor?",
            progress(
                [
                    "wellbeing",
                    "sleep",
                    "medication",
                    "meals",
                    "follow_ups",
                    "family_messages",
                    "open_talk",
                ]
            ),
        ]
    )
    sim = Simulator(CallSession(repo, model, settings, parent)).run_scripted(
        ["my knee hurts so much today", "yes I will"]
    )
    assert not sim.emergency and sim.ended
    assert "Have you told the doctor" in sim.said[1]
    alerts = repo.unnotified_alerts(parent.id)
    assert [a.tier for a in alerts] == ["today"] and alerts[0].kind == "pain"


def test_tired_parent_gets_a_warm_close(world):
    repo, parent, settings = world
    model = ScriptedChat(script=[progress(tired=True)])
    sim = Simulator(CallSession(repo, model, settings, parent)).run_scripted(
        ["I'm tired beta, let's talk tomorrow"]
    )
    assert sim.ended and "lovely talking" in sim.said[-1] and len(sim.said) == 2


def test_turn_budget_ends_call(world):
    repo, parent, settings = world
    model = ScriptedChat(script=[progress(), "Tell me more."] * 20)
    sim = Simulator(CallSession(repo, model, settings, parent)).run_scripted(["and then"] * 10)
    assert sim.ended
    call = repo.get_call(
        sim.events[0].say and [c for c in repo.recent_calls(parent.id, days=1)][0].id
    )
    assert sum(1 for t in call.turns if t.speaker == "parent") == settings.max_turns


def test_model_failure_ends_politely_and_keeps_transcript(world):
    repo, parent, settings = world
    model = ScriptedChat(script=[ModelError("down", retryable=True)])
    session = CallSession(repo, model, settings, parent)
    sim = Simulator(session).run_scripted(["I'm fine"])
    assert sim.ended and "trouble hearing today" in sim.said[-1]
    call = repo.get_call(session.call.id)
    assert call.end_reason == "model_error" and len(call.turns) == 3


def test_interactive_simulator_and_hangup(world):
    repo, parent, settings = world
    model = ScriptedChat(script=[progress(), "How is Farida?"])
    session = CallSession(repo, model, settings, parent)
    answers = iter(["fine", "/hangup"])
    shown = []
    Simulator(session).run_interactive(lambda _p: next(answers), shown.append)
    assert "How is Farida?" in shown[1]
    assert repo.get_call(session.call.id).end_reason == "parent_hung_up"


# --------------------------------------------------------------- twilio pieces


def test_twiml_and_messages():
    xml = conversation_relay_twiml(
        "wss://x.test/twilio/relay", language="ur", parent_id="par_1", greeting='Hello "Amma"'
    )
    assert 'url="wss://x.test/twilio/relay"' in xml and 'language="ur-IN"' in xml
    assert "welcomeGreeting='Hello \"Amma\"'" in xml  # quoted safely
    assert '<Parameter name="parent_id" value="par_1"/>' in xml
    assert json.loads(text_message("hi"))["last"] is True
    assert json.loads(json.loads(end_message("done"))["handoffData"])["reason"] == "done"


@pytest.mark.asyncio
async def test_twilio_relay_websocket_roundtrip(world):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from sunday.calls.transport import twilio_router

    repo, parent, settings = world
    model = ScriptedChat(script=[progress(), "Did you sleep well?", progress(tired=True)])
    app = FastAPI()
    app.include_router(
        twilio_router(
            lambda pid, sid: CallSession(repo, model, settings, repo.get_parent(pid)),
            public_url="https://x.test",
        )
    )
    client = TestClient(app)

    r = client.post(f"/twilio/voice/{parent.id}", data={"CallSid": "CA123"})
    assert r.status_code == 200 and "ConversationRelay" in r.text and "Sunday calling" in r.text

    with client.websocket_connect("/twilio/relay") as ws:
        ws.send_text(
            json.dumps(
                {"type": "setup", "callSid": "CA123", "customParameters": {"parent_id": parent.id}}
            )
        )
        ws.send_text(
            json.dumps({"type": "prompt", "voicePrompt": "I'm", "lang": "en-US", "last": False})
        )  # partial: ignored
        ws.send_text(
            json.dumps(
                {"type": "prompt", "voicePrompt": "I'm fine beta", "lang": "en-US", "last": True}
            )
        )
        out = json.loads(ws.receive_text())
        assert out["type"] == "text" and out["token"] == "Did you sleep well?"
        ws.send_text(json.dumps({"type": "prompt", "voicePrompt": "tired now", "last": True}))
        closing = json.loads(ws.receive_text())
        assert "lovely talking" in closing["token"]
        assert json.loads(ws.receive_text())["type"] == "end"
    calls = repo.recent_calls(parent.id, days=1)
    assert calls[0].provider_call_id == "CA123" and calls[0].status == "completed"


# --------------------------------------------------------------- dialer


def test_dial_and_no_answer_escalation(world):
    repo, parent, settings = world
    tel = FakeTelephony()
    settings = Settings(_env_file=None, SUNDAY_PROVIDER="fake", SUNDAY_PUBLIC_URL="https://x.test")
    c1 = dial(repo, tel, settings, parent)
    assert tel.placed[0]["to"] == parent.phone
    assert tel.placed[0]["voice_url"] == f"https://x.test/twilio/voice/{parent.id}"
    assert repo.get_call(c1).provider_call_id == "CAfake0001"

    from datetime import UTC, datetime, timedelta

    assert record_outcome(repo, c1, "no-answer") is None  # first miss: no alert
    # The same morning's retry is still one day of silence, not two.
    retry = dial(repo, tel, settings, parent)
    assert record_outcome(repo, retry, "no-answer") is None
    assert repo.consecutive_no_answers(parent.id) == 1
    # Push today's attempts back a day; a second missed day raises 'today'.
    for cid in (c1, retry):
        repo.update_call(cid, scheduled_for=datetime.now(UTC) - timedelta(days=1))
    c2 = dial(repo, tel, settings, parent)
    a2 = record_outcome(repo, c2, "busy")
    assert a2 is not None and repo.get_call(c2).status == "no_answer"
    alerts = repo.unnotified_alerts(parent.id)
    assert alerts[-1].tier == "today" and alerts[-1].kind == "no_answer"
    # Third missed day -> 'now'.
    for cid in (c1, retry):
        repo.update_call(cid, scheduled_for=datetime.now(UTC) - timedelta(days=2))
    repo.update_call(c2, scheduled_for=datetime.now(UTC) - timedelta(days=1))
    c3 = dial(repo, tel, settings, parent)
    record_outcome(repo, c3, "no-answer")
    assert repo.unnotified_alerts(parent.id)[-1].tier == "now"
    # A completed call is never overwritten by a late status callback.
    repo.update_call(c3, status="completed")
    assert record_outcome(repo, c3, "no-answer") is None


def test_now_alert_is_handed_out_during_the_call(world):
    repo, parent, settings = world
    seen: list[str] = []
    model = ScriptedChat(script=[ModelError("should not be called", retryable=False)])
    session = CallSession(repo, model, settings, parent, on_alert=lambda a: seen.append(a.id))
    session.start()
    ev = session.hear("I fell and I can't get up")
    assert ev.emergency and seen == ev.alerts_raised and len(seen) == 1
    # Fired once, at the moment the trigger hit, before any further turn.
    session.hear("my hip hurts")
    ev = session.hear("still here")
    assert len(seen) == 1 and "911" in ev.say
    assert session.emergency_number == "911"


def test_a_session_for_an_overseas_parent_refuses_to_start(world):
    """Defence in depth: even if an overseas number reaches the database, Sunday will not
    speak to it. The emergency-number table still knows the right number for that country,
    so a future service area is a one-line change."""
    from sunday.brain.safety import emergency_number_for
    from sunday.geo import OutsideServiceAreaError

    repo, _, settings = world
    parent = repo.add_parent(name="Khalida", preferred_name="Ammi", phone="+923001234567")
    session = CallSession(repo, ScriptedChat(script=[]), settings, parent)
    with pytest.raises(OutsideServiceAreaError):
        session.start()
    assert emergency_number_for("+923001234567") == "1122"


def test_only_us_numbers_can_be_dialled(world):
    from sunday.geo import OutsideServiceAreaError, is_callable, timezone_for

    repo, parent, settings = world
    tel = FakeTelephony()
    settings = Settings(_env_file=None, SUNDAY_PROVIDER="fake", SUNDAY_PUBLIC_URL="https://x.test")
    assert is_callable(parent.phone)
    dial(repo, tel, settings, parent)
    assert len(tel.placed) == 1
    for phone in ("+92 300 1234567", "+44 7700 900003", "+1 416 555 0100", "+1 555 010 0001"):
        p = repo.add_parent(name="X", phone=phone)
        with pytest.raises(OutsideServiceAreaError):
            dial(repo, tel, settings, p)
    assert len(tel.placed) == 1  # nothing else was rung
    assert repo.recent_calls(parent.id, days=1)  # and no Call rows were created for the others
    assert all(c.parent_id == parent.id for c in repo.recent_calls(parent.id, days=1))
    assert timezone_for("+1 614 555 0101") == "America/New_York"
    assert timezone_for("+1 808 555 0100") == "Pacific/Honolulu"


def test_on_alert_failure_never_breaks_the_call(world):
    repo, parent, settings = world

    def boom(_alert):
        raise RuntimeError("twilio down")

    session = CallSession(repo, ScriptedChat(script=[]), settings, parent, on_alert=boom)
    session.start()
    ev = session.hear("I fell")
    assert ev.emergency and "Stay where you are" in ev.say
    assert len(repo.unnotified_alerts(parent.id)) == 1  # still persisted for finish_call


def test_today_alert_changes_the_closing_line(world):
    repo, parent, settings = world
    model = ScriptedChat(script=[progress(tired=True)])
    sim = Simulator(CallSession(repo, model, settings, parent)).run_scripted(
        ["my knee hurts so much, I'm tired, let's talk tomorrow"]
    )
    assert sim.ended
    assert "you're well" not in sim.said[-1] and "how you're doing today" in sim.said[-1]


def test_inbound_call_skips_the_survey_and_still_passes_messages(world):
    repo, parent, settings = world
    msg_id = repo.undelivered_messages(parent.id)[0].id
    model = ScriptedChat(
        script=[
            progress(["wellbeing"]),
            "Oh, Hira says she'll call Thursday after work. Is everything alright?",
            progress(["family_messages", "open_talk"], [msg_id]),
        ]
    )
    session = CallSession(repo, model, settings, parent, direction="inbound")
    sim = Simulator(session).run_scripted(["I couldn't sleep, just wanted to talk", "yes fine"])
    assert sim.said[0] == "Hello Amma, it's Sunday. How lovely that you called. How are you?"
    assert list(session.ctx.plan) == [
        "greeting",
        "wellbeing",
        "family_messages",
        "open_talk",
        "close",
    ]
    assert sim.ended and "lovely talking" in sim.said[-1]
    call = repo.get_call(session.call.id)
    assert call.direction == "inbound" and call.status == "completed"
    assert session.ctx.delivered_message_ids == {msg_id}


def test_dated_reminder_is_only_in_the_prompt_on_its_day(world):
    from datetime import timedelta

    repo, parent, settings = world
    repo.queue_message(
        parent.id,
        from_name="Daniyal",
        text="Appointment with Dr Khan at 10.",
        kind="remind",
        remind_on=parent.local_today() + timedelta(days=2),
    )
    today = CallSession(repo, ScriptedChat(script=[]), settings, parent)
    assert [m.from_name for m in today.ctx.queued] == ["Hira"]  # the appointment waits
    repo.queue_message(
        parent.id,
        from_name="Daniyal",
        text="Take your umbrella, rain today.",
        kind="remind",
        remind_on=parent.local_today(),
    )
    now = CallSession(repo, ScriptedChat(script=[]), settings, parent)
    assert sorted(m.text for m in now.ctx.queued) == [
        "I'll call Thursday after work.",
        "Take your umbrella, rain today.",
    ]
