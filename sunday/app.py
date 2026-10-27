"""Wire the pieces together once, from settings.

Everything that needs a repository, a model, a messenger or telephony gets
them from here, so the CLI, the web app and the tests build the system the
same way. With ``SUNDAY_PROVIDER=fake`` and no Twilio keys you get a fully
working local system: scripted model, console messenger, fake telephony.
"""

from __future__ import annotations

from dataclasses import dataclass

from sunday.brain.model import ChatModel, build_model
from sunday.calls.dialer import FakeTelephony, Telephony, TwilioTelephony
from sunday.calls.session import CallSession
from sunday.config import Settings, load_settings
from sunday.logging_setup import configure_logging, get_logger
from sunday.models import Parent
from sunday.notify.messenger import ConsoleMessenger, Messenger, TwilioMessenger
from sunday.notify.pipeline import finish_call, notify_urgent
from sunday.repository import Repository

log = get_logger("app")


@dataclass
class Services:
    settings: Settings
    repo: Repository
    model: ChatModel
    messenger: Messenger
    telephony: Telephony

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        *,
        model: ChatModel | None = None,
        messenger: Messenger | None = None,
        telephony: Telephony | None = None,
        repo: Repository | None = None,
    ) -> Services:
        settings = settings or load_settings()
        configure_logging(settings.log_level)
        repo = repo or Repository(settings.db_path)
        model = model or build_model(settings)
        if messenger is None:
            if settings.twilio_ready():
                messenger = TwilioMessenger(
                    settings.twilio_account_sid or "",
                    settings.twilio_auth_token.get_secret_value()
                    if settings.twilio_auth_token
                    else "",
                    sms_from=settings.twilio_from_number or "",
                    whatsapp_from=settings.twilio_whatsapp_from,
                )
            else:
                messenger = ConsoleMessenger()
                log.debug("no Twilio credentials: family notes will print to the console")
        if telephony is None:
            if settings.twilio_ready():
                telephony = TwilioTelephony(
                    settings.twilio_account_sid or "",
                    settings.twilio_auth_token.get_secret_value()
                    if settings.twilio_auth_token
                    else "",
                    settings.twilio_from_number or "",
                )
            else:
                telephony = FakeTelephony()
        return cls(
            settings=settings, repo=repo, model=model, messenger=messenger, telephony=telephony
        )

    def new_session(
        self,
        parent: Parent,
        *,
        country: str | None = None,
        direction: str = "outbound",
        provider_call_id: str = "",
    ) -> CallSession:
        """A live call. ``now`` alerts are sent to the family the moment they fire, from inside
        the call; everything else waits for :meth:`complete`. For a call we placed, the Call
        row ``dial`` created (found by the provider's id) is reused rather than duplicated."""

        def urgent(_alert) -> None:
            notify_urgent(self.repo, self.messenger, parent.id)

        call = self.repo.call_by_provider_id(provider_call_id) if provider_call_id else None
        if call is not None and call.parent_id != parent.id:
            call = None
        return CallSession(
            self.repo,
            self.model,
            self.settings,
            parent,
            call=call,
            country=country,
            on_alert=urgent,
            direction=direction,
        )

    def complete(self, session: CallSession) -> None:
        """Run the after-call pipeline for a finished session."""
        finish_call(
            self.repo,
            self.model,
            self.messenger,
            session.call.id,
            signals=list(session.safety.signals),
            delivered_message_ids=set(session.ctx.delivered_message_ids),
            public_url=self.settings.public_url or "",
        )

    def begin_recording(self, session: CallSession, provider_call_id: str) -> None:
        """Start recording a live call, only if the parent has consented. Failure is logged
        and the call goes on unrecorded; a recording is never worth a dropped call."""
        if not session.parent.consent_recording or not provider_call_id:
            return
        base = (self.settings.public_url or "").rstrip("/")
        try:
            sid = self.telephony.start_recording(
                provider_call_id, status_url=f"{base}/twilio/recording/{session.call.id}"
            )
            self.repo.update_call(session.call.id, recording_sid=sid)
        except Exception as exc:  # noqa: BLE001
            log.error("could not start recording for call %s: %s", session.call.id, exc)
