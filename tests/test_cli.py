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
        SUNDAY_SECRET_KEY="test-secret-not-for-production",
        SUNDAY_TWILIO_VERIFY_SIGNATURES=False,
    )
    messenger = ConsoleMessenger(echo=False)
    svc = Services.from_settings(settings, messenger=messenger)
    client = TestClient(create_app(svc))

    assert client.get("/health").json() == {"ok": True}
    assert "Family sign in" in client.get("/").text  # public landing page, no parent data

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
    assert svc.repo.recent_calls(pid, days=1)[0].status == "completed"

    # Two unanswered calls -> a 'today' alert goes to Daniyal.
    from sunday.calls.dialer import dial

    c1 = dial(svc.repo, svc.telephony, settings, svc.repo.get_parent(pid))
    client.post(f"/twilio/status/{c1}", data={"CallSid": "CA2", "CallStatus": "no-answer"})
    c2 = dial(svc.repo, svc.telephony, settings, svc.repo.get_parent(pid))
    client.post(f"/twilio/status/{c2}", data={"CallSid": "CA3", "CallStatus": "no-answer"})
    assert any("did not answer today or yesterday" in t for _, t in messenger.sent)
    assert svc.repo.recent_calls(pid, days=1)[0].status == "no_answer"


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
        SUNDAY_SECRET_KEY="test-secret-not-for-production",
        SUNDAY_TWILIO_VERIFY_SIGNATURES=False,
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


# --------------------------------------------------------------- the family web app


def _web(env, **overrides):
    kw = {
        "SUNDAY_PROVIDER": "fake",
        "SUNDAY_DB": Path(env["SUNDAY_DB"]),
        "SUNDAY_PUBLIC_URL": "https://x.test",
        "SUNDAY_SECRET_KEY": "test-secret-not-for-production",
        "SUNDAY_TWILIO_VERIFY_SIGNATURES": False,
    }
    kw.update(overrides)
    settings = Settings(_env_file=None, **kw)
    messenger = ConsoleMessenger(echo=False)
    svc = Services.from_settings(settings, messenger=messenger)
    # https base url: the session cookie is Secure, so the client must look like https.
    client = TestClient(create_app(svc), base_url="https://testserver", follow_redirects=False)
    return svc, messenger, client


def _sign_in(client, messenger, phone):
    r = client.post("/login", data={"phone": phone})
    assert r.status_code == 200 and "Check your phone" in r.text
    link = re.search(r"https://x\.test(/auth/\S+)", messenger.sent[-1][1]).group(1)
    r = client.get(link)
    assert r.status_code == 303 and r.headers["location"] == "/app"
    assert "sunday_session=" in r.headers["set-cookie"]
    assert "HttpOnly" in r.headers["set-cookie"] and "Secure" in r.headers["set-cookie"]
    return link


def test_server_refuses_to_start_without_a_secret(env):
    settings = Settings(_env_file=None, SUNDAY_PROVIDER="fake", SUNDAY_DB=Path(env["SUNDAY_DB"]))
    svc = Services.from_settings(settings, messenger=ConsoleMessenger(echo=False))
    with pytest.raises(Exception, match="SUNDAY_SECRET_KEY"):
        create_app(svc)
    r = run(env, ["serve", "--port", "1"])
    assert r.exit_code == 1 and "SUNDAY_SECRET_KEY" in r.output


def test_landing_is_public_and_the_family_page_is_not(env):
    _setup(env)
    _, _, client = _web(env)
    r = client.get("/")
    assert r.status_code == 200 and "Someone calls Amma every morning" in r.text
    assert "Family sign in" in r.text
    assert client.get("/health").json() == {"ok": True}
    for path in ("/app", "/app/par_x/export.json", "/app/par_x/calls/call_x"):
        r = client.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/login", path
    assert client.get("/docs").status_code == 404


def test_magic_link_login_is_scoped_single_use_and_quiet_about_unknown_numbers(env):
    _setup(env)
    svc, messenger, client = _web(env)

    # Unknown number: same page, nothing sent.
    r = client.post("/login", data={"phone": "+1 555 999 9999"})
    assert r.status_code == 200 and "Check your phone" in r.text and messenger.sent == []

    # Daniyal (info tier, console channel) gets a link on his channel.
    link = _sign_in(client, messenger, "+1 555 0001")
    assert messenger.sent[-1][0] == "Daniyal" and "once only" in messenger.sent[-1][1]
    # The link is single use.
    r = client.get(link)
    assert r.status_code == 303 and "expired=1" in r.headers["location"]
    # A forged token is refused.
    assert "expired=1" in client.get("/auth/not-a-real-token").headers["location"]

    page = client.get("/app")
    assert page.status_code == 200
    assert "Amma" in page.text and "Signed in as Daniyal" in page.text
    assert "For the next call" in page.text and "call Thursday after work" in page.text
    assert "Export everything" in page.text

    # Another family's parent is not visible, even by id.
    other = svc.repo.add_parent(name="Someone Else", phone="+1 555 777 0000")
    svc.repo.add_family_member(other.id, name="Stranger", phone="+1 555 777 1111")
    assert client.get(f"/app?parent={other.id}").status_code == 404
    assert client.get(f"/app/{other.id}/export.json").status_code == 404
    r = client.post(f"/app/{other.id}/messages", data={"text": "hack", "kind": "say"})
    assert r.status_code == 404
    assert svc.repo.undelivered_messages(other.id) == []

    # Sign out clears the cookie.
    r = client.post("/logout")
    assert r.status_code == 303
    client.cookies.clear()
    assert client.get("/app").status_code == 303


def test_family_actions_and_call_page(env):
    pid = _setup(env)
    svc, messenger, client = _web(env)
    _sign_in(client, messenger, "+1 555 0001")

    # Queue an ask for tomorrow, with a date; then remove it.
    r = client.post(
        f"/app/{pid}/messages",
        data={"text": "Does she want us on Sunday?", "kind": "ask", "on": "2030-01-05"},
    )
    assert r.status_code == 303
    q = svc.repo.undelivered_messages(pid, include_future=True)
    mine = [m for m in q if m.from_name == "Daniyal"]
    assert len(mine) == 1 and mine[0].kind == "ask" and str(mine[0].remind_on) == "2030-01-05"
    client.post(f"/app/{pid}/messages/{mine[0].id}/remove")
    assert [m.from_name for m in svc.repo.undelivered_messages(pid, include_future=True)] == [
        "Hira"
    ]

    # Tell Sunday something, then forget it.
    client.post(
        f"/app/{pid}/memories", data={"text": "Her sister calls on Fridays.", "topic": "family"}
    )
    mems = svc.repo.memories_for(pid)
    assert any("sister" in m.text for m in mems)
    sister = next(m for m in mems if "sister" in m.text)
    client.post(f"/app/{pid}/memories/{sister.id}/remove")
    assert not any("sister" in m.text for m in svc.repo.memories_for(pid))

    # Settings: valid values stick, junk is ignored.
    client.post(
        f"/app/{pid}/settings",
        data={
            "call_time": "08:30",
            "timezone": "Europe/London",
            "language": "ur",
            "preferred_name": "Ammi",
        },
    )
    p = svc.repo.get_parent(pid)
    assert (p.call_time, p.timezone, p.language, p.preferred_name) == (
        "08:30",
        "Europe/London",
        "ur",
        "Ammi",
    )
    client.post(
        f"/app/{pid}/settings",
        data={"call_time": "25:99", "timezone": "Mars/Base", "language": "xx"},
    )
    p = svc.repo.get_parent(pid)
    assert (p.call_time, p.timezone, p.language) == ("08:30", "Europe/London", "ur")

    # A member may change only their own alert level.
    me = next(m for m in svc.repo.family_for(pid) if m.name == "Daniyal")
    hira = next(m for m in svc.repo.family_for(pid) if m.name == "Hira")
    assert (
        client.post(
            f"/app/{pid}/members/{me.id}", data={"min_alert_tier": "today", "channel": "sms"}
        ).status_code
        == 303
    )
    assert (
        client.post(f"/app/{pid}/members/{hira.id}", data={"min_alert_tier": "info"}).status_code
        == 403
    )
    assert svc.repo.get_family_member(me.id).min_alert_tier == "today"
    assert svc.repo.get_family_member(hira.id).min_alert_tier == "now"

    # Have a call (with a fall) through the simulator, then read it on the web.
    run(env, ["simulate", pid], input="I fell in the bathroom\nmy hip\n/hangup\n")
    page = client.get(f"/app?parent={pid}")
    assert "Urgent" in page.text and "I've seen this" in page.text
    alert = [a for a in svc.repo.alerts_since(pid, days=1) if a.tier == "now"][0]
    client.post(f"/app/{pid}/alerts/{alert.id}/ack")
    assert svc.repo.get_alert(alert.id).acknowledged_by == "Daniyal"
    assert "I've seen this" not in client.get(f"/app?parent={pid}").text

    call = svc.repo.recent_calls(pid, days=1)[0]
    page = client.get(f"/app/{pid}/calls/{call.id}")
    assert page.status_code == 200 and "I fell in the bathroom" in page.text
    # Settings above switched Ammi to Urdu, so the emergency script is in Urdu.
    assert "میں آپ کے ساتھ ہوں" in page.text and "What Sunday noticed" in page.text

    data = client.get(f"/app/{pid}/export.json").json()
    assert data["parent"]["preferred_name"] == "Ammi" and len(data["calls"]) == 1
    assert data["calls"][0]["transcript"][1]["text"] == "I fell in the bathroom"
    assert data["alerts"][0]["tier"] == "now"


def test_demo_seeds_a_fortnight_and_the_page_renders_it(env):
    r = run(env, ["demo"])
    assert r.exit_code == 0 and "Demo parent ready" in r.output
    assert run(env, ["demo"]).exit_code == 0  # idempotent
    svc, messenger, client = _web(env)
    _sign_in(client, messenger, "+1 555 010 0001")
    page = client.get("/app").text
    assert "Amma" in page and "days answered" in page and "Dr Khan" in page
    assert "Alerts already seen" in page and "called in" not in page
    assert page.count('class="answered') == 13 and 'class="missed' in page


def test_twilio_webhooks_require_a_valid_signature(env):
    pid = _setup(env)
    svc, _, client = _web(
        env,
        SUNDAY_TWILIO_VERIFY_SIGNATURES=True,
        TWILIO_ACCOUNT_SID="ACtest",
        TWILIO_AUTH_TOKEN="shhh-test-token",
        TWILIO_FROM_NUMBER="+15550000000",
    )
    # No signature: rejected. Wrong signature: rejected. Correct signature: accepted.
    r = client.post(f"/twilio/voice/{pid}", data={"CallSid": "CA1"})
    assert r.status_code == 403
    r = client.post(
        f"/twilio/voice/{pid}", data={"CallSid": "CA1"}, headers={"X-Twilio-Signature": "nope"}
    )
    assert r.status_code == 403
    from twilio.request_validator import RequestValidator

    sig = RequestValidator("shhh-test-token").compute_signature(
        f"https://x.test/twilio/voice/{pid}", {"CallSid": "CA1"}
    )
    r = client.post(
        f"/twilio/voice/{pid}", data={"CallSid": "CA1"}, headers={"X-Twilio-Signature": sig}
    )
    assert r.status_code == 200 and "ConversationRelay" in r.text
    # Inbound and status are gated the same way.
    assert client.post("/twilio/inbound", data={"CallSid": "CA2", "From": "+1"}).status_code == 403
    assert client.post("/twilio/status/x", data={"CallSid": "CA2"}).status_code == 403
    # The relay refuses a callSid no verified webhook registered.
    with client.websocket_connect("/twilio/relay") as ws:
        ws.send_text(
            json.dumps(
                {"type": "setup", "callSid": "CA-unknown", "customParameters": {"parent_id": pid}}
            )
        )
        assert json.loads(ws.receive_text())["type"] == "end"


def test_twilio_verification_fails_closed_without_a_token(env):
    pid = _setup(env)
    _, _, client = _web(env, SUNDAY_TWILIO_VERIFY_SIGNATURES=True)  # on, but no token configured
    assert client.post(f"/twilio/voice/{pid}", data={"CallSid": "CA1"}).status_code == 403
