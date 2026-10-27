"""End-to-end tests through the real CLI and web app with the offline brain.

No API key, no Twilio. These exercise the journeys an operator takes: set up
a parent and family, queue a message, have the simulated call, read the
family note, check the dashboard, and the Twilio status callback paths.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import sunday.cli as cli
from sunday.app import Services
from sunday.config import Settings
from sunday.notify.messenger import ConsoleMessenger
from sunday.repository import Repository
from sunday.web import create_app

runner = CliRunner()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
    return {"SUNDAY_PROVIDER": "fake", "SUNDAY_DB": str(tmp_path / "s.sqlite")}


def run(env, args, input=None):
    return runner.invoke(cli.app, args, input=input, env=env, catch_exceptions=False)


def _setup(env) -> str:
    r = run(
        env,
        [
            "parent",
            "add",
            "--name",
            "Fatima Zaidi",
            "--phone",
            "+923001234567",
            "--language",
            "en",
            "--time",
            "09:00",
            "--timezone",
            "Asia/Karachi",
            "--call-me",
            "Amma",
            "--introduced-by",
            "your son Daniyal",
        ],
    )
    assert r.exit_code == 0
    pid = re.search(r"par_[0-9a-f]+", r.output).group(0)
    assert (
        run(
            env,
            [
                "family",
                "add",
                pid,
                "--name",
                "Daniyal",
                "--phone",
                "+15550001",
                "--relation",
                "son",
                "--channel",
                "console",
                "--alerts",
                "info",
                "--primary",
            ],
        ).exit_code
        == 0
    )
    assert (
        run(
            env,
            [
                "family",
                "add",
                pid,
                "--name",
                "Hira",
                "--phone",
                "+15550002",
                "--relation",
                "daughter",
                "--channel",
                "console",
                "--alerts",
                "now",
            ],
        ).exit_code
        == 0
    )
    assert (
        run(env, ["med", "add", pid, "blood pressure tablet", "--when", "morning"]).exit_code == 0
    )
    assert (
        run(
            env, ["memory", "add", pid, "Grandchildren are Ayaan and Zara.", "--topic", "family"]
        ).exit_code
        == 0
    )
    assert (
        run(env, ["message", pid, "I'll call Thursday after work.", "--from", "Hira"]).exit_code
        == 0
    )
    return pid


def test_setup_and_status(env):
    pid = _setup(env)
    r = run(env, ["status"])
    assert "Fatima Zaidi" in r.output and "Asia/Karachi" in r.output
    r = run(env, ["show", pid])
    assert "blood pressure tablet" in r.output and "Ayaan and Zara" in r.output
    assert "from Hira: I'll call Thursday" in r.output
    assert run(env, ["show", "par_nope"]).exit_code == 1


def test_simulated_call_delivers_message_and_notifies_family(env):
    pid = _setup(env)
    r = run(
        env,
        ["simulate", pid],
        input=(
            "I'm alright beta\nslept okay\nyes I took it\nhad paratha\nyes read it\n"
            "tell her after eight\ngoing to the market\nokay let's talk tomorrow\n"
        ),
    )
    assert r.exit_code == 0, r.output
    out = r.output
    assert "Hello Amma, it's Sunday calling, the daily call from your son Daniyal" in out
    assert "Did you take your tablets" in out
    assert "Hira asked me to tell you: I'll call Thursday after work." in out
    assert "lovely talking to you, Amma" in out
    assert "--- to Daniyal" in out and "Sunday called Amma" in out
    assert "--- to Hira" not in out  # Hira only wants emergencies
    assert "Summary stored" in out
    repo = Repository(env["SUNDAY_DB"])
    assert repo.undelivered_messages(pid) == []  # Hira's message was delivered
    call = repo.recent_calls(pid, days=1)[0]
    assert call.status == "completed" and call.notified_at is not None
    assert sum(1 for t in call.turns if t.speaker == "parent") == 8


def test_simulated_fall_alerts_everyone_immediately(env):
    pid = _setup(env)
    r = run(
        env, ["simulate", pid], input="I fell in the bathroom and I can't get up\nmy hip\n/hangup\n"
    )
    assert r.exit_code == 0
    out = r.output
    assert "Stay where you are" in out and "emergency mode" in out
    assert out.count("URGENT - Sunday about Amma") == 2  # Daniyal and Hira
    assert out.index("--- to Daniyal") < out.index("Sunday called Amma")  # alert before summary
    assert "sounded okay" not in out
    repo = Repository(env["SUNDAY_DB"])
    alerts = repo.alerts_since(pid, days=1)
    assert len(alerts) == 1 and alerts[0].tier == "now" and alerts[0].notified_at is not None
    r = run(env, ["show", pid])
    assert "NOW: Amma said they fell" in r.output


def test_digest_and_due(env):
    pid = _setup(env)
    run(env, ["simulate", pid], input="fine\nslept badly\nmissed them today\nnot hungry\n/hangup\n")
    r = run(env, ["digest", pid])
    assert r.exit_code == 0 and "Amma's week" in r.output and "Calls: 1 answered" in r.output
    r = run(env, ["due", "--dry-run"])
    assert r.exit_code == 0  # either "Nobody is due" or "would call", depending on the clock
    r = run(env, ["call", pid])
    assert r.exit_code == 1 and "Twilio is not configured" in r.output


def test_web_dashboard_and_status_callbacks(env, tmp_path):
    pid = _setup(env)
    settings = Settings(
        _env_file=None,
        SUNDAY_PROVIDER="fake",
        SUNDAY_DB=Path(env["SUNDAY_DB"]),
        SUNDAY_PUBLIC_URL="https://x.test",
    )
    messenger = ConsoleMessenger(echo=False)
    svc = Services.from_settings(settings, messenger=messenger)
    client = TestClient(create_app(svc))

    assert client.get("/health").json() == {"ok": True, "parents": 1}
    page = client.get("/").text
    assert "Fatima Zaidi" in page and f"/parents/{pid}" in page
    assert "No calls yet" in client.get(f"/parents/{pid}").text

    # A call that was answered: voice webhook -> relay -> status callback runs the pipeline.
    r = client.post(f"/twilio/voice/{pid}", data={"CallSid": "CA1"})
    assert r.status_code == 200 and "Sunday calling" in r.text
    with client.websocket_connect("/twilio/relay") as ws:
        ws.send_text(
            json.dumps({"type": "setup", "callSid": "CA1", "customParameters": {"parent_id": pid}})
        )
        ws.send_text('{"type":"prompt","voicePrompt":"I am fine beta","last":true}')
        assert "feeling today" in ws.receive_text()
        ws.send_text('{"type":"prompt","voicePrompt":"okay let\'s talk tomorrow","last":true}')
        ws.receive_text()  # closing line
        ws.receive_text()  # end
    r = client.post("/twilio/status/x", data={"CallSid": "CA1", "CallStatus": "completed"})
    assert r.status_code == 200
    assert any("Sunday called Amma" in t for _, t in messenger.sent)
    assert "completed" in client.get(f"/parents/{pid}").text

    # Two unanswered calls -> a 'today' alert goes to Daniyal.
    from sunday.calls.dialer import dial

    c1 = dial(svc.repo, svc.telephony, settings, svc.repo.get_parent(pid))
    client.post(f"/twilio/status/{c1}", data={"CallSid": "CA2", "CallStatus": "no-answer"})
    c2 = dial(svc.repo, svc.telephony, settings, svc.repo.get_parent(pid))
    client.post(f"/twilio/status/{c2}", data={"CallSid": "CA3", "CallStatus": "no-answer"})
    assert any("did not answer today or yesterday" in t for _, t in messenger.sent)
    assert "no_answer" in client.get(f"/parents/{pid}").text


def test_commands_fail_cleanly_without_parent(env):
    for args in (
        ["show", "par_x"],
        ["simulate", "par_x"],
        ["digest", "par_x"],
        ["message", "par_x", "hi", "--from", "a"],
    ):
        r = run(env, args)
        assert r.exit_code == 1 and "no parent" in r.output


def test_parent_can_call_in(env):
    pid = _setup(env)
    settings = Settings(
        _env_file=None,
        SUNDAY_PROVIDER="fake",
        SUNDAY_DB=Path(env["SUNDAY_DB"]),
        SUNDAY_PUBLIC_URL="https://x.test",
    )
    messenger = ConsoleMessenger(echo=False)
    svc = Services.from_settings(settings, messenger=messenger)
    client = TestClient(create_app(svc))

    # An unknown number gets a kind refusal and a hang-up.
    r = client.post("/twilio/inbound", data={"CallSid": "CA9", "From": "+15559999999"})
    assert r.status_code == 200 and "<Hangup/>" in r.text and "not set up" in r.text
    assert "ConversationRelay" not in r.text

    # Amma rings from her own phone at 2am: greeted by name, no survey, pipeline runs when
    # the relay closes even though no status callback is configured for inbound calls.
    r = client.post("/twilio/inbound", data={"CallSid": "CA10", "From": "+92 300 1234567"})
    assert r.status_code == 200 and "ConversationRelay" in r.text
    assert "How lovely that you called" in r.text
    with client.websocket_connect("/twilio/relay") as ws:
        ws.send_text(json.dumps({"type": "setup", "callSid": "CA10"}))
        ws.send_text('{"type":"prompt","voicePrompt":"I could not sleep beta","last":true}')
        first = json.loads(ws.receive_text())["token"]
        assert "tablets" not in first  # no medication survey on an inbound call
        ws.send_text('{"type":"prompt","voicePrompt":"okay bye now","last":true}')
        ws.receive_text()
        ws.receive_text()
    assert any(t.startswith("Amma called Sunday") for _, t in messenger.sent)
    repo = Repository(env["SUNDAY_DB"])
    calls = repo.recent_calls(pid, days=1)
    assert calls[0].direction == "inbound" and calls[0].notified_at is not None
    # A late status callback is harmless: the pipeline already ran.
    before = len(messenger.sent)
    client.post("/twilio/status/x", data={"CallSid": "CA10", "CallStatus": "completed"})
    assert len(messenger.sent) == before


def test_message_can_be_held_until_a_date(env):
    pid = _setup(env)
    r = run(
        env,
        [
            "message",
            pid,
            "--from",
            "Daniyal",
            "--kind",
            "remind",
            "--on",
            "2030-01-02",
            "Appointment with Dr Khan at 10.",
        ],
    )
    assert r.exit_code == 0 and "Queued from Wed 02 Jan" in r.output
    r = run(env, ["message", pid, "--from", "Daniyal", "--on", "tomorrow", "x"])
    assert r.exit_code == 1 and "YYYY-MM-DD" in r.output
    r = run(env, ["show", pid])
    assert "Dr Khan" in r.output and "(from Wed 02 Jan)" in r.output
