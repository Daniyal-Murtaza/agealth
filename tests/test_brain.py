"""Tests for the conversation brain: safety triggers, prompt, progress, analysis."""

import json

import pytest

from sunday.brain.analysis import analyse_call, no_answer_alert
from sunday.brain.conversation import (
    CallContext,
    build_system_prompt,
    next_line,
    opening_line,
    update_progress,
)
from sunday.brain.model import Message, ModelError, ScriptedChat, complete_json, extract_json
from sunday.brain.safety import SafetyState, Signal, keyword_signals
from sunday.models import Memory, Observation, Parent, QueuedMessage


def _parent(**kw) -> Parent:
    base = dict(
        id="par_1",
        name="Fatima Zaidi",
        preferred_name="Amma",
        phone="+1555",
        language="en",
        medications=[{"name": "blood pressure tablet", "when": "morning"}],
        introduced_by="your son Daniyal",
        notes="Knee replaced in 2023.",
    )
    base.update(kw)
    return Parent(**base)


def _ctx(parent=None, **kw) -> CallContext:
    parent = parent or _parent()
    return CallContext(
        parent=parent,
        memories=[
            Memory(parent_id="par_1", text="Grandchildren are Ayaan and Zara.", topic="family")
        ],
        follow_ups=[
            Observation(
                call_id="c0",
                parent_id="par_1",
                kind="household",
                text="plumber has not come",
                follow_up=True,
            )
        ],
        queued=[
            QueuedMessage(
                id="msg_1",
                parent_id="par_1",
                from_name="Hira",
                kind="say",
                text="I'll call Thursday after work.",
            )
        ],
        family_names=["Daniyal", "Hira"],
        **kw,
    )


# --------------------------------------------------------------- safety


@pytest.mark.parametrize(
    ("text", "kind", "tier"),
    [
        ("I fell in the kitchen this morning and I can't get up", "fall", "now"),
        ("beta main gir gayi thi", "fall", "now"),
        ("there's a pain in my chest since last night", "chest_pain", "now"),
        ("someone called from the bank and I gave him my card number", "scam_live", "now"),
        ("my knee hurts so much today", "pain", "today"),
        ("I have a bit of fever", "unwell", "today"),
        ("nobody calls me anymore, I'm so lonely", "low_mood", "today"),
        ("I ran out of my tablets yesterday", "medication_concern", "today"),
        ("a man said I won a lottery", "scam", "today"),
        ("mujhe bukhar hai", "unwell", "today"),
    ],
)
def test_keyword_signals_fire(text, kind, tier):
    sig = keyword_signals(text)
    assert any(s.kind == kind and s.tier == tier for s in sig), sig


def test_keyword_signals_quiet_on_ordinary_talk():
    assert keyword_signals("I slept well and had paratha with chai. Farida came by.") == []


def test_safety_state_dedupes_and_tracks_highest():
    st = SafetyState()
    assert st.highest is None
    fresh = st.add(keyword_signals("my knee hurts so much"))
    assert [s.kind for s in fresh] == ["pain"] and st.highest == "today"
    assert st.add(keyword_signals("my knee hurts so much again")) == []
    st.add(keyword_signals("I fell"))
    assert st.has_now() and st.highest == "now"


# --------------------------------------------------------------- prompt


def test_opening_line_is_honest_and_localised():
    en = opening_line(_ctx())
    assert "Sunday" in en and "your son Daniyal" in en and "Amma" in en
    ur = opening_line(_ctx(_parent(language="ur")))
    assert "سنڈے" in ur and "Amma" in ur


def test_system_prompt_contains_context_and_rules():
    ctx = _ctx()
    ctx.done.add("greeting")
    p = build_system_prompt(ctx, max_minutes=12, emergency_number="911")
    for needle in (
        "Ayaan and Zara",
        "plumber has not come",
        "From Hira: Tell them: I'll call Thursday",
        "blood pressure tablet (morning)",
        "Knee replaced in 2023",
        "NEVER give medical advice",
        "NEVER ask for money",
        "911",
        "Already covered (do NOT ask about these again): greeting",
        "ONE question at a time",
    ):
        assert needle in p, needle
    ctx.delivered_message_ids.add("msg_1")
    assert "From Hira" not in build_system_prompt(ctx, max_minutes=12, emergency_number="911")


def test_next_line_is_trimmed_to_short_turns():
    model = ScriptedChat(
        script=[
            'Sunday: "That sounds lovely. Did you sleep well? And did you eat? '
            'And how is the knee? Also the weather is nice."'
        ]
    )
    out = next_line(
        model,
        _ctx(),
        [("sunday", "Hello"), ("parent", "I'm fine beta")],
        max_minutes=12,
        emergency_number="911",
    )
    assert out == "That sounds lovely. Did you sleep well? And did you eat?"
    roles = [m.role for m in model.calls[0]]
    assert roles == ["system", "assistant", "user"]


def test_update_progress_marks_items_and_messages():
    ctx = _ctx()
    model = ScriptedChat(
        script=[
            json.dumps(
                {
                    "covered": ["greeting", "wellbeing", "bogus"],
                    "delivered_messages": ["msg_1", "msg_999"],
                }
            )
        ]
    )
    update_progress(
        model,
        ctx,
        [
            ("sunday", "How are you? Hira says she'll call Thursday after work."),
            ("parent", "Fine. Hira called? Good."),
        ],
    )
    assert ctx.done == {"greeting", "wellbeing"} and ctx.delivered_message_ids == {"msg_1"}
    assert "greeting" not in ctx.remaining()


def test_message_not_delivered_unless_sunday_actually_said_it():
    from sunday.brain.conversation import message_was_said

    msg = "I'll call Thursday after work."
    assert not message_was_said(msg, ["I'll let Hira know that after eight is better for you."])
    assert message_was_said(msg, ["Hira asked me to tell you: I'll call Thursday after work."])
    assert message_was_said(msg, ["Hira says she will call you on Thursday after her work."])
    ask = "Do you want a lift to the clinic on Tuesday?"
    assert not message_was_said(ask, ["How is the clinic going?"])
    assert message_was_said(ask, ["Amma, do you want a lift to the clinic on Tuesday?"])

    # The model says msg_1 was delivered; Sunday only promised to pass something on.
    ctx = _ctx()
    model = ScriptedChat(
        script=[json.dumps({"covered": ["wellbeing"], "delivered_messages": ["msg_1"]})]
    )
    update_progress(
        model,
        ctx,
        [("sunday", "I'll let Hira know after eight is better."), ("parent", "Yes, tell her.")],
    )
    assert ctx.delivered_message_ids == set()


def test_update_progress_tired_parent_skips_to_close():
    ctx = _ctx()
    model = ScriptedChat(
        script=[
            json.dumps(
                {
                    "covered": [],
                    "delivered_messages": [],
                    "parent_sounds_tired_or_wants_to_stop": True,
                }
            )
        ]
    )
    update_progress(model, ctx, [("parent", "I'm tired beta, let's talk tomorrow")])
    assert ctx.remaining() == ["close"]


def test_update_progress_survives_model_failure():
    ctx = _ctx()
    update_progress(ScriptedChat(script=["garbage", "garbage"]), ctx, [("parent", "hi")])
    assert ctx.done == set()


# --------------------------------------------------------------- analysis

TRANSCRIPT = """sunday: Hello Amma, it's Sunday calling. How are you today?
parent: I'm alright beta. I slept badly, my knee was hurting.
sunday: I'm sorry about the knee. Did you take your blood pressure tablet this morning?
parent: Yes, I took it with my chai.
sunday: Good. Hira says she'll call you Thursday after work.
parent: Tell her to call after eight, I go to Farida's in the evening.
sunday: And did the plumber come in the end?
parent: No, still nothing. Third day now."""


def test_analysis_grounds_everything_and_merges_signals():
    reply = json.dumps(
        {
            "summary": "Amma was alright but slept badly because of her knee. Took her BP tablet. "
            "Plumber still hasn't come (day 3).",
            "mood": "ok",
            "observations": [
                {
                    "kind": "medication",
                    "text": "took blood pressure tablet",
                    "value": "taken",
                    "evidence": "Yes, I took it with my chai.",
                },
                {
                    "kind": "sleep",
                    "text": "slept badly",
                    "value": "poor",
                    "evidence": "I slept badly",
                },
                {
                    "kind": "household",
                    "text": "plumber still has not come",
                    "follow_up": True,
                    "evidence": "No, still nothing. Third day now.",
                },
                {
                    "kind": "meal",
                    "text": "had biryani for dinner",
                    "evidence": "I had biryani",
                },  # invented
            ],
            "alerts": [
                {
                    "tier": "today",
                    "kind": "pain",
                    "text": "Knee pain disturbed sleep.",
                    "evidence": "my knee was hurting",
                }
            ],
            "memories": [
                {
                    "text": "Visits Farida in the evenings.",
                    "topic": "social",
                    "evidence": "I go to Farida's in the evening",
                },
                {
                    "text": "Loves gardening.",
                    "topic": "likes",
                    "evidence": "I love my garden",
                },  # invented
            ],
            "message_replies": [{"message_id": "msg_1", "response": "Call after eight."}],
        }
    )
    parent = _parent()
    signals = [Signal(kind="pain", tier="today", evidence="my knee was hurting")]
    a = analyse_call(
        ScriptedChat(script=[reply]),
        parent,
        TRANSCRIPT,
        delivered_messages=[("msg_1", "I'll call Thursday after work.")],
        signals=signals,
    )
    assert a.mood == "ok" and "Plumber" in a.summary
    assert [o.kind for o in a.observations] == ["medication", "sleep", "household"]  # meal dropped
    assert [m.text for m in a.memories] == ["Visits Farida in the evenings."]  # garden dropped
    assert len(a.alerts) == 1 and a.alerts[0].tier == "today"
    assert a.message_replies[0].response == "Call after eight."


def test_analysis_never_downgrades_code_now_signal():
    reply = json.dumps(
        {
            "summary": "Fine day.",
            "mood": "ok",
            "alerts": [
                {
                    "tier": "info",
                    "kind": "fall",
                    "text": "Mentioned a small slip.",
                    "evidence": "I fell",
                }
            ],
        }
    )
    a = analyse_call(
        ScriptedChat(script=[reply]),
        _parent(),
        "parent: I fell in the bathroom",
        delivered_messages=[],
        signals=[Signal("fall", "now", "I fell in the bathroom")],
    )
    assert a.alerts[0].tier == "now" and a.alerts[0].kind == "fall"


def test_analysis_fallback_when_model_fails_keeps_signals():
    a = analyse_call(
        ScriptedChat(script=["nope", "nope"]),
        _parent(),
        "parent: chest pain",
        delivered_messages=[],
        signals=[Signal("chest_pain", "now", "chest pain")],
    )
    assert "could not be written" in a.summary
    assert a.alerts[0].tier == "now" and "chest pain" in a.alerts[0].text


def test_no_answer_alert_tiers():
    p = _parent()
    assert no_answer_alert(p, 1) is None
    assert no_answer_alert(p, 2)[0] == "today"
    assert no_answer_alert(p, 3)[0] == "now"


# --------------------------------------------------------------- model helpers


def test_complete_json_repairs_once():
    from pydantic import BaseModel

    class A(BaseModel):
        n: int

    m = ScriptedChat(script=['{"n": "x"}', '{"n": 2}'])
    assert complete_json(m, [Message("user", "q")], A).n == 2
    assert len(m.calls) == 2 and m.calls[0][0].role == "system"
    with pytest.raises(ModelError):
        complete_json(ScriptedChat(script=["bad", "bad"]), [], A)
    assert extract_json('```json\n{"n": 1}\n```') == '{"n": 1}'


def test_household_alerts_become_follow_ups_not_family_alerts():
    reply = json.dumps(
        {
            "summary": "Fine. Plumber still not here.",
            "mood": "ok",
            "alerts": [
                {
                    "tier": "today",
                    "kind": "household",
                    "text": "Plumber has not come for three days.",
                    "evidence": "the plumber still hasn't come, third day",
                },
                {
                    "tier": "today",
                    "kind": "pain",
                    "text": "Severe knee pain.",
                    "evidence": "my knee hurts so much",
                },
                {
                    "tier": "today",
                    "kind": "safety",
                    "text": "No water in the house.",
                    "evidence": "there is no water since the plumber",
                },
            ],
        }
    )
    transcript = (
        "parent: the plumber still hasn't come, third day\nparent: my knee hurts so much\n"
        "parent: there is no water since the plumber"
    )
    a = analyse_call(
        ScriptedChat(script=[reply]), _parent(), transcript, delivered_messages=[], signals=[]
    )
    assert [x.kind for x in a.alerts] == ["pain", "safety"]  # plumber demoted, no-water kept
    follow = [o for o in a.observations if o.follow_up]
    assert follow and follow[0].kind == "household" and "Plumber" in follow[0].text


def test_address_is_not_used_every_sentence():
    from sunday.brain.conversation import _trim_address

    text = "I'm sorry to hear that, Amma. Did you take your tablet, Amma? Rest well, Amma."
    once = _trim_address(text, "Amma", recent_sunday_turns=[])
    assert once.count("Amma") == 1 and once.startswith("I'm sorry to hear that, Amma.")
    none = _trim_address(text, "Amma", recent_sunday_turns=["Hello Amma", "Good, Amma."])
    assert none == "I'm sorry to hear that. Did you take your tablet? Rest well."
    assert _trim_address("Did you sleep well?", "Amma", []) == "Did you sleep well?"


def test_ask_messages_are_marked_and_ids_shown():
    ctx = _ctx()
    ctx.queued.append(
        QueuedMessage(
            id="msg_2",
            parent_id="par_1",
            from_name="Daniyal",
            kind="ask",
            text="Does she want a lift on Tuesday?",
        )
    )
    p = build_system_prompt(ctx, max_minutes=12, emergency_number="911")
    assert "[msg_2] From Daniyal: ASK them and wait for their answer: Does she want a lift" in p
    assert "[msg_1] From Hira: Tell them:" in p


def test_open_ask_keeps_family_messages_uncovered_even_when_tired():
    ctx = _ctx()
    ctx.queued.append(
        QueuedMessage(
            id="msg_2",
            parent_id="par_1",
            from_name="Daniyal",
            kind="ask",
            text="Does she want a lift on Tuesday?",
        )
    )
    model = ScriptedChat(
        script=[
            json.dumps(
                {
                    "covered": ["family_messages"],
                    "delivered_messages": ["msg_1"],
                    "parent_sounds_tired_or_wants_to_stop": True,
                }
            )
        ]
    )
    update_progress(model, ctx, [("parent", "I'm tired")])
    assert ctx.remaining() == ["family_messages", "close"]
    assert [m.id for m in ctx.pending_asks()] == ["msg_2"]
    prompt = build_system_prompt(ctx, max_minutes=12, emergency_number="911")
    assert "the family is waiting for an answer to: Does she want a lift" in prompt
    ctx.delivered_message_ids.add("msg_2")
    assert ctx.pending_asks() == []


def test_today_triggers_soften_when_food_is_coming_or_pain_is_denied():
    from sunday.brain.safety import country_from_phone, emergency_number_for

    # Breakfast is on the way: not a no-food alert.
    assert keyword_signals("Haven't eaten yet, I'll make something later") == []
    assert keyword_signals("khana nahi khaya abhi, thodi der mein") == []
    # Still an alert when nothing is coming.
    assert [s.kind for s in keyword_signals("I haven't eaten since yesterday")] == ["no_food"]
    # Denials stay quiet; a now-tier phrase is never softened.
    assert keyword_signals("No pain today, I didn't have any fever either") == []
    assert [s.tier for s in keyword_signals("no pain but I fell")] == ["now"]

    assert country_from_phone("+923001234567") == "PK"
    assert country_from_phone("+1 614 555 0101") == "US"
    assert country_from_phone("+44 20 7946 0958") == "GB"
    assert country_from_phone("03001234567") is None  # not E.164: unknown
    assert emergency_number_for("+923001234567") == "1122"
    assert emergency_number_for("+919876543210") == "112"
    assert emergency_number_for("+16145550101") == "911"
    assert emergency_number_for(None) == "911"


def test_household_is_always_a_follow_up_and_replies_never_are():
    reply = json.dumps(
        {
            "summary": "Fine.",
            "mood": "ok",
            "observations": [
                {
                    "kind": "household",
                    "text": "plumber has not come, tap dripping",
                    "evidence": "the plumber still hasn't come, the tap is dripping",
                    "follow_up": False,
                },
                {
                    "kind": "plan",
                    "text": "will take the lift on Tuesday after eleven",
                    "evidence": "Yes, I'll take the lift, after eleven though",
                    "follow_up": True,
                },
            ],
            "message_replies": [
                {"message_id": "msg_1", "response": "Yes, I'll take the lift, after eleven though"}
            ],
        }
    )
    transcript = (
        "parent: the plumber still hasn't come, the tap is dripping\n"
        "parent: Yes, I'll take the lift, after eleven though"
    )
    a = analyse_call(
        ScriptedChat(script=[reply]),
        _parent(),
        transcript,
        delivered_messages=[("msg_1", "Lift on Tuesday?")],
        signals=[],
    )
    flags = {o.kind: o.follow_up for o in a.observations}
    assert flags == {"household": True, "plan": False}


def test_forgotten_plumber_is_recorded_from_the_parents_words():
    # The model wrote a summary but no observation about the plumber: tomorrow must still ask.
    reply = json.dumps(
        {
            "summary": "Fine. Plumber still not here.",
            "mood": "ok",
            "observations": [
                {"kind": "medication", "text": "took it", "value": "taken", "evidence": "took it"}
            ],
        }
    )
    transcript = (
        "sunday: How are you?\nparent: took it with chai\n"
        "parent: The plumber still hasn't come, third day now, the tap is dripping\n"
        "parent: the delivery came this morning, lovely"
    )
    a = analyse_call(
        ScriptedChat(script=[reply]), _parent(), transcript, delivered_messages=[], signals=[]
    )
    household = [o for o in a.observations if o.kind == "household"]
    assert len(household) == 1 and household[0].follow_up
    assert household[0].evidence.startswith("The plumber still hasn't come")
    # Nothing is added when the model already recorded it.
    reply2 = json.loads(reply)
    reply2["observations"].append(
        {
            "kind": "household",
            "text": "plumber not come",
            "evidence": "The plumber still hasn't come, third day now, the tap is dripping",
        }
    )
    a2 = analyse_call(
        ScriptedChat(script=[json.dumps(reply2)]),
        _parent(),
        transcript,
        delivered_messages=[],
        signals=[],
    )
    assert len([o for o in a2.observations if o.kind == "household"]) == 1
