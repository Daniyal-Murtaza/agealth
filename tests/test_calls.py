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
        phone="+15551234567",
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

    assert record_outcome(repo, c1, "no-answer") is None  # first miss: no alert
    c2 = dial(repo, tel, settings, parent)
    a2 = record_outcome(repo, c2, "busy")
    assert a2 is not None and repo.get_call(c2).status == "no_answer"
    alerts = repo.unnotified_alerts(parent.id)
    assert alerts[-1].tier == "today" and alerts[-1].kind == "no_answer"
    c3 = dial(repo, tel, settings, parent)
    record_outcome(repo, c3, "no-answer")
    assert repo.unnotified_alerts(parent.id)[-1].tier == "now"
    # A completed call is never overwritten by a late status callback.
    repo.update_call(c3, status="completed")
    assert record_outcome(repo, c3, "no-answer") is None
