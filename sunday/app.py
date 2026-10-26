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
from sunday.notify.pipeline import finish_call
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

    def new_session(self, parent: Parent, *, country: str = "US") -> CallSession:
        return CallSession(self.repo, self.model, self.settings, parent, country=country)

    def complete(self, session: CallSession) -> None:
        """Run the after-call pipeline for a finished session."""
        finish_call(
            self.repo,
            self.model,
            self.messenger,
            session.call.id,
            signals=list(session.safety.signals),
            delivered_message_ids=set(session.ctx.delivered_message_ids),
        )
