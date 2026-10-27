"""Transports: how a CallSession is connected to a real or simulated phone line.

* :class:`Simulator` - drives a session from text lines (terminal or tests).
  Lets you design and test the conversation with zero telephony.
* :func:`twilio_router` - FastAPI routes for Twilio Conversation Relay: a TwiML
  endpoint that answers the call and a WebSocket that streams the parent's
  words in and Sunday's words out. Twilio does speech-to-text and text-to-
  speech; we only exchange text.

Twilio message shapes (from the docs):
  in : {"type":"setup", callSid, from, to, customParameters:{parent_id}}
       {"type":"prompt", voicePrompt, lang, last}
       {"type":"interrupt", ...}   {"type":"dtmf", digit}   {"type":"error", description}
  out: {"type":"text", token, last}      {"type":"end", handoffData}

Note: no ``from __future__ import annotations`` in this module. FastAPI resolves
``WebSocket`` and ``Form`` parameters from real type objects.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field

from sunday.calls.session import CallSession, SessionEvent
from sunday.logging_setup import get_logger

log = get_logger("calls.transport")


# --------------------------------------------------------------------------
# Simulator
# --------------------------------------------------------------------------


@dataclass
class SimulatedCall:
    events: list[SessionEvent] = field(default_factory=list)

    @property
    def said(self) -> list[str]:
        return [e.say for e in self.events if e.say]

    @property
    def ended(self) -> bool:
        return any(e.end_call for e in self.events)

    @property
    def emergency(self) -> bool:
        return any(e.emergency for e in self.events)


class Simulator:
    """Run a session against scripted parent lines or an interactive prompt."""

    def __init__(self, session: CallSession) -> None:
        self.session = session
        self.result = SimulatedCall()

    def run_scripted(self, parent_lines: list[str]) -> SimulatedCall:
        self.result.events.append(self.session.start())
        for line in parent_lines:
            ev = self.session.hear(line)
            self.result.events.append(ev)
            if ev.end_call:
                break
        if not self.result.ended:
            self.session.hangup("script_finished")
        return self.result

    def run_interactive(
        self,
        ask: Callable[[str], str],
        show: Callable[[str], None],
        note: Callable[[str], None] | None = None,
    ) -> SimulatedCall:
        ev = self.session.start()
        self.result.events.append(ev)
        show(ev.say)
        while not ev.end_call:
            try:
                line = ask("you (as the parent) > ")
            except (KeyboardInterrupt, EOFError):
                line = "/hangup"
            if line.strip().lower() in ("/hangup", "/quit", "/exit"):
                self.session.hangup()
                break
            ev = self.session.hear(line)
            self.result.events.append(ev)
            if ev.say:
                show(ev.say)
            if ev.emergency and note is not None:
                sent = "alerts sent to the family" if ev.alerts_raised else "family already alerted"
                note(f"emergency mode: {sent}, staying on the line")
        return self.result


# --------------------------------------------------------------------------
# Twilio Conversation Relay
# --------------------------------------------------------------------------

# Default voices per language. Google voices via Twilio; override in settings later.
TTS = {
    "en": ("Google", "en-US", "en-US-Neural2-F"),
    "ur": ("Google", "ur-IN", "ur-IN-Standard-A"),
    "hi": ("Google", "hi-IN", "hi-IN-Neural2-A"),
    "pa": ("Google", "pa-IN", "pa-IN-Standard-A"),
}


def conversation_relay_twiml(ws_url: str, *, language: str, parent_id: str, greeting: str) -> str:
    """TwiML that connects the answered call to our WebSocket."""
    from xml.sax.saxutils import escape, quoteattr

    provider, lang_code, voice = TTS.get(language, TTS["en"])
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response><Connect>"
        f"<ConversationRelay url={quoteattr(ws_url)} welcomeGreeting={quoteattr(greeting)} "
        f'language="{lang_code}" ttsProvider="{provider}" voice="{voice}" '
        'transcriptionProvider="Deepgram" speechModel="nova-3-general" '
        'interruptible="speech" speechTimeout="1200" dtmfDetection="true">'
        f'<Parameter name="parent_id" value="{escape(parent_id)}"/>'
        "</ConversationRelay></Connect></Response>"
    )


def text_message(token: str, *, last: bool = True) -> str:
    return json.dumps({"type": "text", "token": token, "last": last})


def end_message(reason: str) -> str:
    return json.dumps({"type": "end", "handoffData": json.dumps({"reason": reason})})


def twilio_router(make_session: "Callable[[str, str], CallSession]", *, public_url: str):
    """Build the FastAPI router. ``make_session(parent_id, call_sid)`` creates a session."""
    from fastapi import APIRouter, Form, WebSocket, WebSocketDisconnect
    from fastapi.responses import Response

    router = APIRouter()
    ws_url = public_url.replace("https://", "wss://").replace("http://", "ws://") + "/twilio/relay"

    @router.post("/twilio/voice/{parent_id}")
    async def voice(parent_id: str, CallSid: str = Form("")) -> Response:  # noqa: N803
        # Twilio hits this when the parent answers. Greeting is spoken by Twilio while
        # the WebSocket connects, so the first second of the call is never silent.
        session = make_session(parent_id, CallSid)
        ev = session.start()
        twiml = conversation_relay_twiml(
            ws_url, language=session.parent.language, parent_id=parent_id, greeting=ev.say
        )
        _PENDING[CallSid] = session
        return Response(content=twiml, media_type="application/xml")

    @router.websocket("/twilio/relay")
    async def relay(ws: WebSocket) -> None:
        await ws.accept()
        session: CallSession | None = None
        try:
            while True:
                raw = await ws.receive_text()
                msg = json.loads(raw)
                kind = msg.get("type")
                if kind == "setup":
                    call_sid = msg.get("callSid", "")
                    session = _PENDING.pop(call_sid, None)
                    if session is None:
                        pid = (msg.get("customParameters") or {}).get("parent_id", "")
                        session = make_session(pid, call_sid)
                        session.start()
                    session.repo.update_call(session.call.id, provider_call_id=call_sid)
                    log.info("relay connected for call %s", call_sid)
                elif kind == "prompt" and session is not None:
                    if not msg.get("last", True):
                        continue  # wait for the finished utterance
                    ev = session.hear(msg.get("voicePrompt", ""))
                    if ev.say:
                        await ws.send_text(text_message(ev.say, last=True))
                    if ev.end_call:
                        await ws.send_text(end_message("completed"))
                        break
                elif kind == "interrupt":
                    log.debug("parent interrupted: %s", msg.get("utteranceUntilInterrupt", ""))
                elif kind == "error":
                    log.error("twilio relay error: %s", msg.get("description"))
        except WebSocketDisconnect:
            log.info("relay disconnected")
        finally:
            if session is not None:
                session.hangup("line_dropped" if session.phase.value != "ended" else "completed")

    return router


_PENDING: dict[str, CallSession] = {}
