"""Prove a webhook really came from Twilio.

Twilio signs every request with the account's auth token (HMAC-SHA1 over the
exact URL it called plus the POSTed form fields) and sends the result as
``X-Twilio-Signature``. We verify with Twilio's own ``RequestValidator``.

The check fails closed: if verification is on and there is no auth token, or
the header is missing or wrong, the request is rejected. Tests disable it with
``SUNDAY_TWILIO_VERIFY_SIGNATURES=false``; nothing else does.

The URL Twilio signed is the public one it dialled, so we rebuild it from
``SUNDAY_PUBLIC_URL`` plus the request path and query rather than trusting the
Host header a tunnel forwarded.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import HTTPException, Request

from sunday.config import Settings
from sunday.logging_setup import get_logger

log = get_logger("web.twilio_auth")


def make_twilio_verifier(settings: Settings) -> Callable[[Request], Awaitable[None]]:
    """A FastAPI dependency that raises 403 unless the request is signed by Twilio."""
    verify = settings.twilio_verify_signatures
    token = settings.twilio_auth_token.get_secret_value() if settings.twilio_auth_token else ""
    base = (settings.public_url or "").rstrip("/")

    async def dependency(request: Request) -> None:
        if not verify:
            return
        if not token or not base:
            log.error("twilio webhook rejected: verification on but no auth token / public url")
            raise HTTPException(status_code=403, detail="webhook verification not configured")
        from twilio.request_validator import RequestValidator

        signature = request.headers.get("X-Twilio-Signature", "")
        url = base + request.url.path
        if request.url.query:
            url += "?" + request.url.query
        form = await request.form()
        params = {k: str(v) for k, v in form.items()}
        if not RequestValidator(token).validate(url, params, signature):
            log.warning("twilio webhook rejected: bad signature for %s", request.url.path)
            raise HTTPException(status_code=403, detail="bad twilio signature")

    return dependency
