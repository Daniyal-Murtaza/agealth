"""The web server: the public landing page, the family app, and Twilio's webhooks.

Run with ``sunday serve``. It binds to 127.0.0.1; put a tunnel (ngrok,
cloudflared) or a reverse proxy in front and set ``SUNDAY_PUBLIC_URL``.

Routes
  GET  /                           public landing page (no login)
  GET  /start, POST /start         self-serve onboarding: add a parent and yourself
  GET  /login, POST /login         family login: a one-time link to the phone on file
  GET  /auth/{token}               redeem the link, set the session cookie
  POST /logout
  GET  /app                        the family page for one parent (switcher if several)
  POST /app/{pid}/messages         queue something for tomorrow's call; DELETE-ish via /remove
  POST /app/{pid}/alerts/{id}/ack  "I've seen this"
  POST /app/{pid}/memories         tell Sunday something; /remove to retire it
  POST /app/{pid}/settings         call time, timezone, language
  POST /app/{pid}/members/{id}     a member's own alert level and channel
  POST /app/{pid}/members          invite a sibling: they get notes and can sign in
  POST /app/{pid}/medications      add or remove what the call asks about
  POST /app/{pid}/consent          the parent's recording consent, recorded by the family
  POST /app/{pid}/call             ring the parent now (the introductory call, or any time)
  GET  /app/{pid}/calls/{call_id}  one call: note, transcript, what was noticed, recording
  GET  /app/{pid}/calls/{call_id}/recording.mp3   the recording, family only
  POST /twilio/recording/{call_id} Twilio (signed): a recording finished
  GET  /app/{pid}/export.json      everything about this parent, for the family to keep
  POST /twilio/voice/{parent_id}   Twilio (signed): the parent answered our call
  POST /twilio/inbound             Twilio (signed): the parent rang us
  WS   /twilio/relay               Twilio: live conversation text in/out
  POST /twilio/status/{call_id}    Twilio (signed): call ended / no answer / busy
  GET  /health

Every /app route re-checks that the logged-in phone is on the parent's family
record. Nothing is served to a browser that is not a family member's, except
the landing page and the login form.

Note: no ``from __future__ import annotations`` here; FastAPI needs real types.
"""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import available_timezones

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from sunday.app import Services
from sunday.brain.conversation import LANGUAGE_NAMES
from sunday.calls.dialer import dial, record_outcome
from sunday.calls.session import CallSession
from sunday.calls.transport import twilio_router
from sunday.logging_setup import get_logger
from sunday.notify.digest import compute_trends, format_digest
from sunday.notify.pipeline import notify_no_answer
from sunday.web.auth import (
    SESSION_COOKIE,
    Auth,
    RateLimiter,
    Viewer,
    digits_only,
    login_message,
)
from sunday.web.twilio_auth import make_twilio_verifier

log = get_logger("web")

_HERE = Path(__file__).parent
_SESSIONS: dict[str, CallSession] = {}

MOOD_LABEL = {"bright": "sounded bright", "ok": "sounded okay", "low": "sounded a bit low"}
TIER_LABEL = {"now": "Urgent", "today": "Check today", "info": "FYI"}


def create_app(services: Services) -> FastAPI:
    settings = services.settings
    repo = services.repo
    secret = settings.require_secret_key()
    auth = Auth(
        repo,
        secret,
        login_link_minutes=settings.login_link_minutes,
        session_days=settings.session_days,
    )
    public_url = (settings.public_url or "http://127.0.0.1:8000").rstrip("/")
    cookie_secure = public_url.startswith("https://")

    app = FastAPI(title="Sunday", version="0.1.0", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")
    templates = Jinja2Templates(directory=_HERE / "templates")
    templates.env.globals.update(
        mood_label=lambda m: MOOD_LABEL.get(m, ""),
        tier_label=lambda t: TIER_LABEL.get(t, t),
        languages=LANGUAGE_NAMES,
        demo_login=settings.demo_login,
    )

    def render(request: Request, name: str, **ctx) -> HTMLResponse:
        ctx.setdefault("viewer", None)
        return templates.TemplateResponse(request, name, ctx)

    # -- who is asking ---------------------------------------------------------------

    def current_viewer(request: Request) -> Viewer:
        v = auth.viewer_from_request(request)
        if v is None:
            raise HTTPException(status_code=303, headers={"Location": "/login"})
        return v

    def parent_for(viewer: Viewer, parent_id: str):
        p = viewer.parent(parent_id)
        if p is None:
            raise HTTPException(status_code=404, detail="no such parent on your account")
        return p

    # -- public -----------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def landing(request: Request):
        return render(request, "landing.html", viewer=auth.viewer_from_request(request))

    start_limit = RateLimiter(limit=5, window_s=3600)

    @app.get("/start", response_class=HTMLResponse)
    async def start_form(request: Request):
        return render(
            request,
            "start.html",
            viewer=auth.viewer_from_request(request),
            timezones=_TIMEZONES,
            errors=[],
            form={},
        )

    @app.post("/start", response_class=HTMLResponse)
    async def start_submit(request: Request):
        """Create the parent and the first family member, then send that member their
        sign-in link. Daily calls begin only after the introductory call, which the member
        places from their page, so a stranger's number typed here is never rung on its own."""
        form = {k: str(v).strip() for k, v in (await request.form()).items()}
        errors = _validate_start(form)
        addr = request.client.host if request.client else "?"
        if not errors and not start_limit.allow(addr):
            errors.append("Too many sign-ups from this connection. Please try again later.")
        if errors:
            return render(request, "start.html", timezones=_TIMEZONES, errors=errors, form=form)
        your_digits = digits_only(form["your_phone"])
        if repo.parent_by_phone(form["parent_phone"]) or auth.members_for_phone(your_digits):
            # Already set up: do not create a second household; send the sign-in link.
            members = auth.members_for_phone(your_digits)
            if members:
                _send_login(members[0], your_digits)
            return render(request, "start_done.html", form=form, existing=True)
        parent = repo.add_parent(
            name=form["parent_name"],
            preferred_name=form.get("call_me") or form["parent_name"].split()[0],
            phone=_e164(form["parent_phone"]),
            language=form.get("language", "en"),
            timezone=form.get("timezone", "UTC"),
            call_time=form.get("call_time", "09:00"),
            introduced_by=f"your {form['relation']} {form['your_name']}".strip(),
            medications=[
                {"name": m.strip(), "when": "morning"}
                for m in form.get("medications", "").split(",")
                if m.strip()
            ],
            intro_done=False,
        )
        member = repo.add_family_member(
            parent.id,
            name=form["your_name"],
            relation=form["relation"],
            phone=_e164(form["your_phone"]),
            channel="whatsapp" if settings.twilio_ready() else "console",
            min_alert_tier="info",
            is_primary=True,
        )
        for line in form.get("about", "").splitlines():
            if line.strip():
                repo.add_memory(parent.id, line.strip()[:300], topic="family", source="family")
        _send_login(member, your_digits)
        log.info("onboarded %s for %s (%s)", member.name, parent.name, parent.id)
        return render(request, "start_done.html", form=form, existing=False)

    def _send_login(member, digits: str) -> None:
        link = f"{public_url}/auth/{auth.make_login_token(digits)}"
        try:
            services.messenger.send(
                member, login_message(member.name, link, settings.login_link_minutes)
            )
        except Exception as exc:  # noqa: BLE001
            log.error("login link to %s failed: %s", member.name, exc)

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request):
        if auth.viewer_from_request(request):
            return RedirectResponse("/app", status_code=303)
        return render(request, "login.html")

    @app.post("/login", response_class=HTMLResponse)
    async def login_submit(request: Request, phone: str = Form("")):
        addr = request.client.host if request.client else "?"
        digits = digits_only(phone)
        # Same answer whether or not the number is known, and whether or not we sent.
        if len(digits) >= 7 and auth.limit_addr.allow(addr) and auth.limit_phone.allow(digits):
            members = auth.members_for_phone(digits)
            if members:
                link = f"{public_url}/auth/{auth.make_login_token(digits)}"
                text = login_message(members[0].name, link, settings.login_link_minutes)
                try:
                    services.messenger.send(members[0], text)
                except Exception as exc:  # noqa: BLE001 - never leak delivery state to the form
                    log.error("login link to %s failed: %s", members[0].name, exc)
        return render(request, "login_sent.html", phone=phone)

    @app.get("/auth/{token}", response_class=HTMLResponse)
    async def confirm(request: Request, token: str):
        # WhatsApp and iMessage fetch links to build previews. A GET must not consume the
        # one-time token, so this page just checks it and posts back when the person taps.
        if not auth.peek_login_token(token):
            return RedirectResponse("/login?expired=1", status_code=303)
        return render(request, "auth_confirm.html", token=token)

    @app.post("/auth/{token}")
    async def redeem(token: str):
        phone = auth.redeem_login_token(token)
        if phone is None:
            return RedirectResponse("/login?expired=1", status_code=303)
        resp = RedirectResponse("/app", status_code=303)
        resp.set_cookie(
            SESSION_COOKIE,
            auth.make_session_cookie(phone),
            max_age=auth.session_s,
            httponly=True,
            secure=cookie_secure,
            samesite="lax",
            path="/",
        )
        return resp

    @app.get("/demo", response_class=HTMLResponse)
    async def demo_page(request: Request):
        """Hosted demo only (SUNDAY_DEMO_LOGIN). Signs the visitor in as the demo parent's
        son so they can see a family page. The demo parent is invented; the session is for
        the demo phone only, so it can never reach a real family."""
        if not settings.demo_login:
            raise HTTPException(status_code=404)
        return render(request, "demo.html", viewer=auth.viewer_from_request(request))

    @app.post("/demo")
    async def demo_login():
        from sunday.demo import FAMILY, seed_demo

        if not settings.demo_login:
            raise HTTPException(status_code=404)
        seed_demo(repo)
        resp = RedirectResponse("/app", status_code=303)
        resp.set_cookie(
            SESSION_COOKIE,
            auth.make_session_cookie(digits_only(FAMILY[0][2])),
            max_age=auth.session_s,
            httponly=True,
            secure=cookie_secure,
            samesite="lax",
            path="/",
        )
        return resp

    @app.post("/logout")
    async def logout():
        resp = RedirectResponse("/", status_code=303)
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    # -- the family page -----------------------------------------------------------------

    @app.get("/app", response_class=HTMLResponse)
    async def family_home(
        request: Request, parent: str = "", viewer: Viewer = Depends(current_viewer)
    ):
        p = parent_for(viewer, parent) if parent else viewer.parent(None)
        if p is None:
            raise HTTPException(status_code=404)
        return render(
            request,
            "family.html",
            viewer=viewer,
            **_family_context(repo, p, viewer, twilio_ready=settings.twilio_ready()),
        )

    @app.post("/app/{pid}/messages")
    async def add_message(
        pid: str,
        text: str = Form(""),
        kind: str = Form("say"),
        on: str = Form(""),
        viewer: Viewer = Depends(current_viewer),
    ):
        p = parent_for(viewer, pid)
        text = text.strip()[:400]
        if text:
            remind_on = None
            if on:
                try:
                    remind_on = date.fromisoformat(on)
                except ValueError:
                    remind_on = None
            kind = kind if kind in ("say", "ask", "remind") else "say"
            repo.queue_message(
                p.id,
                from_name=viewer.membership_for(p.id).name,
                text=text,
                kind=kind,
                remind_on=remind_on,
            )
        return RedirectResponse(f"/app?parent={p.id}#tomorrow", status_code=303)

    @app.post("/app/{pid}/messages/{mid}/remove")
    async def remove_message(pid: str, mid: str, viewer: Viewer = Depends(current_viewer)):
        p = parent_for(viewer, pid)
        repo.delete_message(mid, p.id)
        return RedirectResponse(f"/app?parent={p.id}#tomorrow", status_code=303)

    @app.post("/app/{pid}/alerts/{aid}/ack")
    async def ack_alert(pid: str, aid: str, viewer: Viewer = Depends(current_viewer)):
        p = parent_for(viewer, pid)
        repo.acknowledge_alert(aid, p.id, by=viewer.membership_for(p.id).name)
        return RedirectResponse(f"/app?parent={p.id}#alerts", status_code=303)

    @app.post("/app/{pid}/memories")
    async def add_memory(
        pid: str,
        text: str = Form(""),
        topic: str = Form(""),
        viewer: Viewer = Depends(current_viewer),
    ):
        p = parent_for(viewer, pid)
        text = text.strip()[:300]
        if text:
            repo.add_memory(p.id, text, topic=topic.strip()[:30], source="family")
        return RedirectResponse(f"/app?parent={p.id}#knows", status_code=303)

    @app.post("/app/{pid}/memories/{mem_id}/remove")
    async def remove_memory(pid: str, mem_id: int, viewer: Viewer = Depends(current_viewer)):
        p = parent_for(viewer, pid)
        repo.deactivate_memory(mem_id, p.id)
        return RedirectResponse(f"/app?parent={p.id}#knows", status_code=303)

    @app.post("/app/{pid}/settings")
    async def update_settings(
        pid: str,
        call_time: str = Form("09:00"),
        timezone: str = Form("UTC"),
        language: str = Form("en"),
        preferred_name: str = Form(""),
        viewer: Viewer = Depends(current_viewer),
    ):
        p = parent_for(viewer, pid)
        fields: dict = {}
        try:
            hh, mm = call_time.split(":")
            if 0 <= int(hh) < 24 and 0 <= int(mm) < 60:
                fields["call_time"] = f"{int(hh):02d}:{int(mm):02d}"
        except ValueError:
            pass
        if timezone in available_timezones():
            fields["timezone"] = timezone
        if language in LANGUAGE_NAMES:
            fields["language"] = language
        if preferred_name.strip():
            fields["preferred_name"] = preferred_name.strip()[:40]
        if fields:
            repo.update_parent(p.id, **fields)
        return RedirectResponse(f"/app?parent={p.id}#settings", status_code=303)

    @app.post("/app/{pid}/members/{member_id}")
    async def update_member(
        pid: str,
        member_id: str,
        min_alert_tier: str = Form("info"),
        channel: str = Form("whatsapp"),
        viewer: Viewer = Depends(current_viewer),
    ):
        """A member edits their own alert level and channel only."""
        p = parent_for(viewer, pid)
        mine = viewer.membership_for(p.id)
        if mine is None or mine.id != member_id:
            raise HTTPException(status_code=403, detail="you can only change your own settings")
        fields: dict = {}
        if min_alert_tier in ("info", "today", "now"):
            fields["min_alert_tier"] = min_alert_tier
        if channel in ("whatsapp", "sms", "console"):
            fields["channel"] = channel
        if fields:
            repo.update_family_member(member_id, **fields)
        return RedirectResponse(f"/app?parent={p.id}#settings", status_code=303)

    @app.post("/app/{pid}/members")
    async def invite_member(
        pid: str,
        name: str = Form(""),
        relation: str = Form(""),
        phone: str = Form(""),
        min_alert_tier: str = Form("info"),
        viewer: Viewer = Depends(current_viewer),
    ):
        """Add a sibling. They get the notes at their level and can sign in with that number."""
        p = parent_for(viewer, pid)
        name, phone = name.strip()[:60], phone.strip()
        if name and len(digits_only(phone)) >= 7:
            already = any(digits_only(m.phone) == digits_only(phone) for m in repo.family_for(p.id))
            if not already:
                m = repo.add_family_member(
                    p.id,
                    name=name,
                    relation=relation.strip()[:30],
                    phone=_e164(phone),
                    channel="whatsapp" if settings.twilio_ready() else "console",
                    min_alert_tier=min_alert_tier
                    if min_alert_tier in ("info", "today", "now")
                    else "info",
                )
                _send_login(m, digits_only(phone))
        return RedirectResponse(f"/app?parent={p.id}#settings", status_code=303)

    @app.post("/app/{pid}/medications")
    async def medications(
        pid: str,
        add: str = Form(""),
        when: str = Form("morning"),
        remove: str = Form(""),
        viewer: Viewer = Depends(current_viewer),
    ):
        p = parent_for(viewer, pid)
        meds = [dict(m) for m in (p.medications or [])]
        if remove:
            meds = [m for m in meds if m.get("name") != remove]
        if add.strip():
            meds.append({"name": add.strip()[:60], "when": when.strip()[:20] or "morning"})
        repo.update_parent(p.id, medications=meds)
        return RedirectResponse(f"/app?parent={p.id}#settings", status_code=303)

    @app.post("/app/{pid}/consent")
    async def consent(
        pid: str, recording: str = Form(""), viewer: Viewer = Depends(current_viewer)
    ):
        """The family records the parent's consent to recording. Sunday says it is recording at
        the start of every recorded call, so the parent hears it each time."""
        p = parent_for(viewer, pid)
        repo.update_parent(p.id, consent_recording=recording == "yes")
        return RedirectResponse(f"/app?parent={p.id}#settings", status_code=303)

    @app.post("/app/{pid}/call", response_class=HTMLResponse)
    async def call_now(request: Request, pid: str, viewer: Viewer = Depends(current_viewer)):
        """Ring the parent now. This is how the introductory call is placed: the family member
        is next to the parent (or on the phone with them) and introduces Sunday."""
        p = parent_for(viewer, pid)
        if not settings.twilio_ready() or not settings.public_url:
            return render(
                request, "call_now.html", viewer=viewer, parent=p, placed=False, call_id=""
            )
        if any(c.status in ("scheduled", "in_progress") for c in repo.recent_calls(p.id, days=1)):
            return render(
                request,
                "call_now.html",
                viewer=viewer,
                parent=p,
                placed=True,
                call_id="",
                busy=True,
            )
        call_id = dial(repo, services.telephony, settings, p)
        return render(
            request, "call_now.html", viewer=viewer, parent=p, placed=True, call_id=call_id
        )

    @app.get("/app/{pid}/calls/{call_id}/recording.mp3")
    async def recording(pid: str, call_id: str, viewer: Viewer = Depends(current_viewer)):
        p = parent_for(viewer, pid)
        call = repo.get_call(call_id)
        if call is None or call.parent_id != p.id or not call.recording_url:
            raise HTTPException(status_code=404)
        try:
            data = services.telephony.recording_bytes(call.recording_url)
        except Exception as exc:  # noqa: BLE001
            log.error("recording fetch failed for %s: %s", call_id, exc)
            raise HTTPException(status_code=502, detail="recording unavailable") from exc
        return Response(
            content=data,
            media_type="audio/mpeg",
            headers={"Cache-Control": "private, no-store", "Content-Disposition": "inline"},
        )

    @app.get("/app/{pid}/calls/{call_id}", response_class=HTMLResponse)
    async def call_page(
        request: Request, pid: str, call_id: str, viewer: Viewer = Depends(current_viewer)
    ):
        p = parent_for(viewer, pid)
        call = repo.get_call(call_id)
        if call is None or call.parent_id != p.id:
            raise HTTPException(status_code=404)
        return render(
            request,
            "call.html",
            viewer=viewer,
            parent=p,
            call=call,
            local=lambda dt: p.local_now(dt) if dt else None,
        )

    @app.get("/app/{pid}/export.json")
    async def export(pid: str, viewer: Viewer = Depends(current_viewer)):
        """The family owns this. Everything we hold about the parent, as plain JSON."""
        p = parent_for(viewer, pid)
        calls = []
        for c in repo.recent_calls(p.id, days=3650, limit=10000):
            full = repo.get_call(c.id)
            calls.append(
                {
                    "id": c.id,
                    "direction": c.direction,
                    "status": c.status,
                    "started_at": c.started_at.isoformat() if c.started_at else None,
                    "ended_at": c.ended_at.isoformat() if c.ended_at else None,
                    "mood": c.mood,
                    "summary": c.summary,
                    "transcript": [
                        {"speaker": t.speaker, "text": t.text, "at": t.at.isoformat()}
                        for t in full.turns
                    ],
                    "observations": [
                        {"kind": o.kind, "text": o.text, "value": o.value, "evidence": o.evidence}
                        for o in full.observations
                    ],
                }
            )
        data = {
            "exported_at": datetime.now(UTC).isoformat(),
            "parent": {
                "name": p.name,
                "preferred_name": p.preferred_name,
                "language": p.language,
                "timezone": p.timezone,
                "call_time": p.call_time,
                "medications": p.medications,
            },
            "family": [
                {"name": m.name, "relation": m.relation, "alerts": m.min_alert_tier}
                for m in repo.family_for(p.id)
            ],
            "memories": [
                {"text": m.text, "topic": m.topic, "at": m.at.isoformat()}
                for m in repo.memories_for(p.id)
            ],
            "alerts": [
                {
                    "tier": a.tier,
                    "kind": a.kind,
                    "text": a.text,
                    "evidence": a.evidence,
                    "at": a.at.isoformat(),
                }
                for a in repo.alerts_since(p.id, days=3650)
            ],
            "calls": calls,
        }
        return JSONResponse(
            data,
            headers={
                "Content-Disposition": f'attachment; filename="sunday-{p.address().lower()}.json"'
            },
        )

    # -- Twilio ----------------------------------------------------------------------------

    verify = make_twilio_verifier(settings)

    def make_session(parent_id: str, call_sid: str) -> CallSession:
        parent = repo.get_parent(parent_id)
        if parent is None:
            raise HTTPException(status_code=404)
        session = services.new_session(parent, provider_call_id=call_sid)
        _SESSIONS[call_sid] = session
        services.begin_recording(session, call_sid)
        return session

    def make_inbound_session(from_number: str, call_sid: str) -> CallSession | None:
        parent = repo.parent_by_phone(from_number)
        if parent is None:
            return None
        session = services.new_session(parent, direction="inbound")
        _SESSIONS[call_sid] = session
        services.begin_recording(session, call_sid)
        return session

    def on_end(session: CallSession) -> None:
        for sid, s in list(_SESSIONS.items()):
            if s is session:
                _SESSIONS.pop(sid, None)
        services.complete(session)

    app.include_router(
        twilio_router(
            make_session,
            public_url=public_url,
            make_inbound_session=make_inbound_session,
            on_end=on_end,
            verify=verify,
        )
    )

    @app.post("/twilio/status/{call_id}", dependencies=[Depends(verify)])
    async def status(
        call_id: str,
        CallSid: str = Form(""),  # noqa: N803 - Twilio's field names
        CallStatus: str = Form(""),  # noqa: N803
    ) -> PlainTextResponse:
        session = _SESSIONS.pop(CallSid, None)
        if session is not None:
            session.hangup("completed" if session.phase.value == "ended" else "line_dropped")
            services.complete(session)
            return PlainTextResponse("ok")
        if record_outcome(repo, call_id, CallStatus) is not None:
            call = repo.get_call(call_id)
            if call is not None:
                notify_no_answer(repo, services.messenger, call.parent_id)
        return PlainTextResponse("ok")

    @app.post("/twilio/recording/{call_id}", dependencies=[Depends(verify)])
    async def recording_done(
        call_id: str,
        RecordingSid: str = Form(""),  # noqa: N803
        RecordingUrl: str = Form(""),  # noqa: N803
        RecordingDuration: str = Form("0"),  # noqa: N803
        RecordingStatus: str = Form("completed"),  # noqa: N803
    ) -> PlainTextResponse:
        call = repo.get_call(call_id)
        if call is None or RecordingStatus != "completed":
            return PlainTextResponse("ok")
        parent = repo.get_parent(call.parent_id)
        if parent is None or not parent.consent_recording:
            log.warning("recording callback for %s without consent; ignoring", call_id)
            return PlainTextResponse("ok")
        try:
            seconds = int(float(RecordingDuration or 0))
        except ValueError:
            seconds = 0
        repo.update_call(
            call_id,
            recording_sid=RecordingSid or call.recording_sid,
            recording_url=RecordingUrl,
            recording_seconds=seconds,
        )
        return PlainTextResponse("ok")

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True}

    return app


# -- onboarding helpers ------------------------------------------------------------------------

_COMMON_TZ = [
    "Asia/Karachi",
    "Asia/Kolkata",
    "Asia/Dhaka",
    "Asia/Dubai",
    "Europe/London",
    "Europe/Paris",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Toronto",
    "Australia/Sydney",
    "UTC",
]
_TIMEZONES = _COMMON_TZ + sorted(
    t for t in available_timezones() if "/" in t and not t.startswith("Etc") and t not in _COMMON_TZ
)


def _e164(phone: str) -> str:
    """Normalise to +digits. Accepts spaces, dashes and brackets."""
    digits = digits_only(phone)
    return f"+{digits}" if digits else phone.strip()


def _validate_start(form: dict) -> list[str]:
    errors = []
    if len(form.get("your_name", "")) < 2:
        errors.append("Please tell us your name.")
    if not form.get("relation"):
        errors.append("Please say how you are related (son, daughter, niece...).")
    if len(digits_only(form.get("your_phone", ""))) < 7 or not form.get(
        "your_phone", ""
    ).startswith("+"):
        errors.append("Your mobile number needs the country code, like +1 555 010 0001.")
    if len(form.get("parent_name", "")) < 2:
        errors.append("Please tell us your parent's name.")
    if len(digits_only(form.get("parent_phone", ""))) < 7 or not form.get(
        "parent_phone", ""
    ).startswith("+"):
        errors.append("Your parent's number needs the country code, like +92 300 1234567.")
    if digits_only(form.get("your_phone", "")) == digits_only(form.get("parent_phone", "")):
        errors.append("Your number and your parent's number are the same.")
    if form.get("language", "en") not in LANGUAGE_NAMES:
        errors.append("Please pick a language.")
    if form.get("timezone", "UTC") not in available_timezones():
        errors.append("Please pick a timezone.")
    try:
        hh, mm = form.get("call_time", "09:00").split(":")
        assert 0 <= int(hh) < 24 and 0 <= int(mm) < 60
    except (ValueError, AssertionError):
        errors.append("The call time should look like 09:00.")
    if form.get("confirm") != "yes":
        errors.append(
            "Please confirm you are a family member and your parent knows you are setting this up."
        )
    return errors


# -- view model -------------------------------------------------------------------------------


def _family_context(repo, p, viewer: Viewer, *, twilio_ready: bool = False) -> dict:
    now_local = p.local_now()
    today = now_local.date()
    calls = repo.recent_calls(p.id, days=30, limit=60)
    completed = [c for c in calls if c.status == "completed" and c.summary]
    latest = completed[0] if completed else None
    latest_full = repo.get_call(latest.id) if latest else None

    # Fourteen-day strip: one cell per local day.
    by_day: dict[date, list] = {}
    for c in calls:
        when = c.started_at or c.scheduled_for
        if when:
            by_day.setdefault(p.local_now(when).date(), []).append(c)
    strip = []
    for i in range(13, -1, -1):
        d = today - timedelta(days=i)
        day_calls = by_day.get(d, [])
        done = [c for c in day_calls if c.status == "completed"]
        missed = [c for c in day_calls if c.status == "no_answer"]
        if done:
            state, mood = "answered", (done[0].mood or "unclear")
        elif missed:
            state, mood = "missed", ""
        else:
            state, mood = "none", ""
        strip.append({"date": d, "state": state, "mood": mood, "call": done[0] if done else None})

    obs = repo.observations_since(p.id, days=14)
    med_days_taken = {
        p.local_now(o.at).date() for o in obs if o.kind == "medication" and o.value == "taken"
    }
    med_days_missed = {
        p.local_now(o.at).date() for o in obs if o.kind == "medication" and o.value == "missed"
    }
    answered_days = sum(1 for s in strip if s["state"] == "answered")

    alerts = repo.alerts_since(p.id, days=30)
    open_alerts = [a for a in alerts if not a.acknowledged_by]
    seen_alerts = [a for a in alerts if a.acknowledged_by][:5]

    queued = repo.undelivered_messages(p.id, include_future=True)
    trends = compute_trends(repo, p, days=7)
    follow_ups = [o.text for o in latest_full.observations if o.follow_up] if latest_full else []

    return {
        "parent": p,
        "parents": viewer.parents,
        "me": viewer.membership_for(p.id),
        "members": repo.family_for(p.id),
        "today": today,
        "now_local": now_local,
        "latest": latest,
        "latest_local": p.local_now(latest.started_at or latest.ended_at) if latest else None,
        "follow_ups": follow_ups,
        "strip": strip,
        "answered_days": answered_days,
        "med_taken": len(med_days_taken),
        "med_missed": len(med_days_missed - med_days_taken),
        "open_alerts": open_alerts,
        "seen_alerts": seen_alerts,
        "queued": queued,
        "memories": list(reversed(repo.memories_for(p.id)))[:12],
        "calls": completed[:10],
        "digest": format_digest(p, trends),
        "trends": trends,
        "local": lambda dt: p.local_now(dt) if dt else None,
        "timezones": sorted(
            t for t in available_timezones() if "/" in t and not t.startswith("Etc")
        ),
        "min_date": today.isoformat(),
        "next_call": _next_call_text(p, now_local),
        "twilio_ready": twilio_ready,
        "has_calls": bool(completed),
    }


def _next_call_text(p, now_local: datetime) -> str:
    hh, mm = (p.call_time or "09:00").split(":")
    target = now_local.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    if target <= now_local:
        target += timedelta(days=1)
    day = "today" if target.date() == now_local.date() else "tomorrow"
    return f"{day} at {target:%H:%M}"
