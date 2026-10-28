"""Placing outbound calls and deciding what to do when nobody answers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from sunday.brain.analysis import no_answer_alert
from sunday.config import Settings
from sunday.logging_setup import get_logger
from sunday.models import Parent
from sunday.repository import Repository

log = get_logger("calls.dialer")


class Telephony(Protocol):
    def place_call(self, to: str, *, voice_url: str, status_url: str) -> str:
        """Start ringing. Returns the provider call id."""
        ...

    def start_recording(self, provider_call_id: str, *, status_url: str) -> str:
        """Begin recording a live call (only called when the parent has consented).
        Returns the provider recording id."""
        ...

    def recording_bytes(self, recording_url: str) -> bytes:
        """Fetch a finished recording as MP3 bytes, using the account credentials."""
        ...


class TwilioTelephony:
    def __init__(self, account_sid: str, auth_token: str, from_number: str) -> None:
        from twilio.rest import Client

        self._client = Client(account_sid, auth_token)
        self._from = from_number

    def place_call(self, to: str, *, voice_url: str, status_url: str) -> str:
        call = self._client.calls.create(
            to=to,
            from_=self._from,
            url=voice_url,
            method="POST",
            status_callback=status_url,
            status_callback_method="POST",
            status_callback_event=["completed", "no-answer", "busy", "failed"],
            timeout=30,  # ring for 30s; an older person may need a moment
        )
        return call.sid

    def start_recording(self, provider_call_id: str, *, status_url: str) -> str:
        rec = self._client.calls(provider_call_id).recordings.create(
            recording_status_callback=status_url,
            recording_status_callback_event=["completed"],
            recording_channels="dual",
        )
        return rec.sid

    def recording_bytes(self, recording_url: str) -> bytes:
        import httpx

        url = recording_url if recording_url.endswith(".mp3") else recording_url + ".mp3"
        r = httpx.get(url, auth=(self._client.username, self._client.password), timeout=30)
        r.raise_for_status()
        return r.content


class FakeTelephony:
    """Records what would have been dialled. ``recordings`` lists started recordings."""

    def __init__(self, *, answer: bool = True) -> None:
        self.answer = answer
        self.placed: list[dict] = []
        self.recordings: list[dict] = []

    def place_call(self, to: str, *, voice_url: str, status_url: str) -> str:
        self.placed.append({"to": to, "voice_url": voice_url, "status_url": status_url})
        return f"CAfake{len(self.placed):04d}"

    def start_recording(self, provider_call_id: str, *, status_url: str) -> str:
        self.recordings.append({"call": provider_call_id, "status_url": status_url})
        return f"REfake{len(self.recordings):04d}"

    def recording_bytes(self, recording_url: str) -> bytes:
        return b"ID3fake-mp3-bytes:" + recording_url.encode()


def dial(repo: Repository, telephony: Telephony, settings: Settings, parent: Parent) -> str:
    """Create the Call record and ask the provider to ring. Returns the call id.
    Refuses, before anything is created, to ring a number outside the service area."""
    from sunday.geo import check_callable

    check_callable(parent.phone)
    call = repo.create_call(parent.id, status="scheduled", scheduled_for=datetime.now(UTC))
    base = (settings.public_url or "http://localhost:8000").rstrip("/")
    sid = telephony.place_call(
        parent.phone,
        voice_url=f"{base}/twilio/voice/{parent.id}",
        status_url=f"{base}/twilio/status/{call.id}",
    )
    repo.update_call(call.id, provider_call_id=sid)
    log.info("dialing %s for %s (call %s)", parent.phone, parent.name, call.id)
    return call.id


def record_outcome(repo: Repository, call_id: str, provider_status: str) -> str | None:
    """Map a provider status callback to our call status. Returns an alert id if raised."""
    call = repo.get_call(call_id)
    if call is None:
        return None
    if call.status in ("completed", "in_progress"):
        return None  # the session already handled it
    status = {
        "no-answer": "no_answer",
        "busy": "no_answer",
        "failed": "failed",
        "canceled": "cancelled",
    }.get(provider_status, "no_answer")
    repo.update_call(call_id, status=status, ended_at=datetime.now(UTC), end_reason=provider_status)
    parent = repo.get_parent(call.parent_id)
    if parent is None or status != "no_answer":
        return None
    streak = repo.consecutive_no_answers(parent.id)
    decision = no_answer_alert(parent, streak)
    if decision is None:
        return None
    tier, text = decision
    alert = repo.add_alert(parent.id, call_id=call_id, tier=tier, kind="no_answer", text=text)
    log.warning("no answer x%d for %s -> %s alert", streak, parent.name, tier)
    return alert.id
