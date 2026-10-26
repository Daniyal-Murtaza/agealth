"""The web server: Twilio webhooks plus a small read-only family dashboard.

Run with ``sunday serve``. Twilio must be able to reach it over HTTPS, so in
development you put a tunnel (ngrok, cloudflared) in front and set
SUNDAY_PUBLIC_URL to the tunnel address.

Routes
  POST /twilio/voice/{parent_id}   Twilio: the parent answered -> TwiML
  WS   /twilio/relay               Twilio: live conversation text in/out
  POST /twilio/status/{call_id}    Twilio: call ended / no answer / busy
  GET  /                           dashboard: parents, last call, open alerts
  GET  /parents/{parent_id}        one parent: recent calls, alerts, trends
  GET  /health

Note: no ``from __future__ import annotations`` here; FastAPI needs real types.
"""

import html

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from sunday.app import Services
from sunday.calls.dialer import record_outcome
from sunday.calls.session import CallSession
from sunday.calls.transport import twilio_router
from sunday.logging_setup import get_logger
from sunday.notify.digest import compute_trends, format_digest
from sunday.notify.pipeline import notify_no_answer

log = get_logger("web")

_SESSIONS: dict[str, CallSession] = {}


def create_app(services: Services) -> FastAPI:
    app = FastAPI(title="Sunday", version="0.1.0")
    public_url = services.settings.public_url or "http://localhost:8000"

    def make_session(parent_id: str, call_sid: str) -> CallSession:
        parent = services.repo.get_parent(parent_id)
        if parent is None:
            raise KeyError(parent_id)
        session = services.new_session(parent)
        _SESSIONS[call_sid] = session
        return session

    app.include_router(twilio_router(make_session, public_url=public_url))

    @app.post("/twilio/status/{call_id}")
    async def status(
        call_id: str,
        CallSid: str = Form(""),  # noqa: N803 - Twilio's field names
        CallStatus: str = Form(""),  # noqa: N803
    ) -> PlainTextResponse:
        """Call ended. Either run the after-call pipeline or record a no-answer."""
        session = _SESSIONS.pop(CallSid, None)
        if session is not None:
            session.hangup("completed" if session.phase.value == "ended" else "line_dropped")
            services.complete(session)
            return PlainTextResponse("ok")
        if record_outcome(services.repo, call_id, CallStatus) is not None:
            call = services.repo.get_call(call_id)
            if call is not None:
                notify_no_answer(services.repo, services.messenger, call.parent_id)
        return PlainTextResponse("ok")

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True, "parents": len(services.repo.list_parents())}

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> str:
        rows = []
        for p in services.repo.list_parents():
            last = services.repo.last_completed_call(p.id)
            alerts = [a for a in services.repo.alerts_since(p.id, days=7) if not a.acknowledged_by]
            urgent = sum(1 for a in alerts if a.tier == "now")
            rows.append(
                f"<tr><td><a href='/parents/{p.id}'>{html.escape(p.name)}</a></td>"
                f"<td>{html.escape(p.language)} at {html.escape(p.call_time)}</td>"
                f"<td>{html.escape(last.summary[:120]) if last and last.summary else '-'}</td>"
                f"<td>{len(alerts)}{' (' + str(urgent) + ' urgent)' if urgent else ''}</td></tr>"
            )
        body = (
            (
                "<table><tr><th>Parent</th><th>Calls</th><th>Last call</th>"
                "<th>Alerts (7d)</th></tr>" + "".join(rows) + "</table>"
            )
            if rows
            else "<p>No parents yet. Add one with the CLI.</p>"
        )
        return _page("Sunday", body)

    @app.get("/parents/{parent_id}", response_class=HTMLResponse)
    async def parent_page(parent_id: str) -> str:
        p = services.repo.get_parent(parent_id)
        if p is None:
            return _page("Not found", "<p>No such parent.</p>")
        calls = services.repo.recent_calls(p.id, days=14)
        alerts = services.repo.alerts_since(p.id, days=14)
        trends = compute_trends(services.repo, p, days=7)
        call_rows = (
            "".join(
                f"<li><b>{(c.started_at or c.scheduled_for):%a %d %b %H:%M}</b> - "
                f"{html.escape(c.status)}"
                + (f" - {html.escape(c.mood)}" if c.mood else "")
                + (f"<br>{html.escape(c.summary)}" if c.summary else "")
                + "</li>"
                for c in calls
            )
            or "<li>No calls yet.</li>"
        )
        alert_rows = (
            "".join(
                f"<li><b>{html.escape(a.tier.upper())}</b> {html.escape(a.text)}"
                + (f' <i>"{html.escape(a.evidence[:100])}"</i>' if a.evidence else "")
                + "</li>"
                for a in alerts
            )
            or "<li>None.</li>"
        )
        body = (
            f"<p><a href='/'>&larr; all parents</a></p>"
            f"<h2>Recent calls</h2><ul>{call_rows}</ul>"
            f"<h2>Alerts</h2><ul>{alert_rows}</ul>"
            f"<h2>This week</h2><pre>{html.escape(format_digest(p, trends))}</pre>"
        )
        return _page(p.name, body)

    return app


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)} - Sunday</title>"
        "<style>body{font:16px/1.5 -apple-system,system-ui,sans-serif;max-width:900px;"
        "margin:2rem auto;padding:0 1rem;color:#222}table{border-collapse:collapse;width:100%}"
        "td,th{border-bottom:1px solid #ddd;padding:.5rem;text-align:left;vertical-align:top}"
        "pre{background:#f6f6f6;padding:1rem;white-space:pre-wrap}</style></head>"
        f"<body><h1>{html.escape(title)}</h1>{body}"
        "<p style='color:#777;margin-top:3rem'>Sunday is an assistant, not a doctor.</p>"
        "</body></html>"
    )
