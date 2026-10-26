"""Sending notes to the family: WhatsApp or SMS via Twilio, or the console.

Every message states where it came from and never includes medical advice.
The console messenger makes the whole system usable with no Twilio account,
which is how you test the product before paying for anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from sunday.logging_setup import get_logger
from sunday.models import FamilyMember

log = get_logger("notify.messenger")


class Messenger(Protocol):
    def send(self, member: FamilyMember, text: str) -> str:
        """Deliver ``text`` to ``member``. Returns a provider message id."""
        ...


class TwilioMessenger:
    def __init__(
        self, account_sid: str, auth_token: str, *, sms_from: str, whatsapp_from: str | None
    ) -> None:
        from twilio.rest import Client

        self._client = Client(account_sid, auth_token)
        self._sms_from = sms_from
        self._wa_from = whatsapp_from

    def send(self, member: FamilyMember, text: str) -> str:
        if member.channel == "whatsapp" and self._wa_from:
            to = (
                member.phone if member.phone.startswith("whatsapp:") else f"whatsapp:{member.phone}"
            )
            msg = self._client.messages.create(to=to, from_=self._wa_from, body=text)
        else:
            msg = self._client.messages.create(to=member.phone, from_=self._sms_from, body=text)
        return msg.sid


@dataclass
class ConsoleMessenger:
    """Prints instead of sending. Records everything for tests."""

    sent: list[tuple[str, str]] = field(default_factory=list)
    echo: bool = True

    def send(self, member: FamilyMember, text: str) -> str:
        self.sent.append((member.name, text))
        if self.echo:
            print(f"\n--- to {member.name} ({member.channel} {member.phone}) ---\n{text}\n")
        return f"console-{len(self.sent)}"


TIER_ORDER = {"info": 0, "today": 1, "now": 2}


def wants(member: FamilyMember, tier: str) -> bool:
    return TIER_ORDER[tier] >= TIER_ORDER.get(member.min_alert_tier, 0)
