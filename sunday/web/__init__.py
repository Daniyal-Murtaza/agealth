"""The web server: the public landing page, the family app, and Twilio's webhooks.

Run with ``sunday serve``. It binds to 127.0.0.1; put a tunnel (ngrok,
cloudflared) or a reverse proxy in front and set ``SUNDAY_PUBLIC_URL``.

Routes
  GET  /                           public landing page (no login)
  GET  /login, POST /login         family login: a one-time link to the phone on file
  GET  /auth/{token}               redeem the link, set the session cookie
  POST /logout
  GET  /app                        the family page for one parent (switcher if several)
  POST /app/{pid}/messages         queue something for tomorrow's call; DELETE-ish via /remove
  POST /app/{pid}/alerts/{id}/ack  "I've seen this"
  POST /app/{pid}/memories         tell Sunday something; /remove to retire it
  POST /app/{pid}/settings         call time, timezone, language
  POST /app/{pid}/members/{id}     a member's own alert level and channel
  GET  /app/{pid}/calls/{call_id}  one call: note, transcript, what was noticed
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
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from sunday.app import Services
from sunday.brain.conversation import LANGUAGE_NAMES
from sunday.calls.dialer import record_outcome
from sunday.calls.session import CallSession
from sunday.calls.transport import twilio_router
from sunday.logging_setup import get_logger
from sunday.notify.digest import compute_trends, format_digest
from sunday.notify.pipeline import notify_no_answer
from sunday.web.auth import SESSION_COOKIE, Auth, Viewer, digits_only, login_message
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

    @app.get("/auth/{token}")
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
        return render(request, "family.html", viewer=viewer, **_family_context(repo, p, viewer))

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
        session = services.new_session(parent)
        _SESSIONS[call_sid] = session
        return session

    def make_inbound_session(from_number: str, call_sid: str) -> CallSession | None:
        parent = repo.parent_by_phone(from_number)
        if parent is None:
            return None
        session = services.new_session(parent, direction="inbound")
        _SESSIONS[call_sid] = session
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

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True}

    return app


# -- view model -------------------------------------------------------------------------------


def _family_context(repo, p, viewer: Viewer) -> dict:
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
    }


def _next_call_text(p, now_local: datetime) -> str:
    hh, mm = (p.call_time or "09:00").split(":")
    target = now_local.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    if target <= now_local:
        target += timedelta(days=1)
    day = "today" if target.date() == now_local.date() else "tomorrow"
    return f"{day} at {target:%H:%M}"
