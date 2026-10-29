"""Where Sunday operates. For now: the United States only.

Every path that can ring a phone (the scheduler, "Have Sunday call now", the CLI,
and a parent calling in) goes through :func:`check_callable` first. A number
outside the service area is refused with a plain message and nothing is dialled.
Family members who only receive notes may be anywhere; this is about calls.
"""

from __future__ import annotations

import phonenumbers
from phonenumbers import timezone as pn_timezone

ALLOWED_REGIONS: tuple[str, ...] = ("US",)
SERVICE_AREA_NAME = "the United States"

# US timezones, most populous first, for pickers.
US_TIMEZONES = [
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Phoenix",
    "America/Los_Angeles",
    "America/Anchorage",
    "Pacific/Honolulu",
    "America/Puerto_Rico",
]

# What a family member reads instead of the IANA identifier.
TIMEZONE_LABELS = {
    "America/New_York": "Eastern Time",
    "America/Chicago": "Central Time",
    "America/Denver": "Mountain Time",
    "America/Phoenix": "Arizona (no daylight saving)",
    "America/Los_Angeles": "Pacific Time",
    "America/Anchorage": "Alaska Time",
    "Pacific/Honolulu": "Hawaii Time",
    "America/Puerto_Rico": "Puerto Rico (Atlantic Time)",
}


def tz_label(tz: str | None) -> str:
    """'Eastern Time' for America/New_York; an unknown zone falls back to its own name."""
    return TIMEZONE_LABELS.get(tz or "", (tz or "").replace("_", " "))


class OutsideServiceAreaError(ValueError):
    def __init__(self, phone: str, region: str | None) -> None:
        where = f" ({region})" if region else ""
        super().__init__(
            f"Sunday can only call numbers in {SERVICE_AREA_NAME} for now; "
            f"{phone}{where} is outside that."
        )
        self.phone = phone
        self.region = region


def parse(phone: str):
    """Parse a number the way an American types it: no + means a US number."""
    try:
        return phonenumbers.parse(phone, "US")
    except phonenumbers.NumberParseException:
        return None


def region_of(phone: str) -> str | None:
    """ISO region for a +E.164 number. Canada and the Caribbean share +1 with the US, so
    this uses the full number, not the country code."""
    n = parse(phone)
    if n is None:
        return None
    return phonenumbers.region_code_for_number(n) or None


def is_callable(phone: str) -> bool:
    return region_of(phone) in ALLOWED_REGIONS


def check_callable(phone: str) -> None:
    """Raise :class:`OutsideServiceAreaError` unless the number may be rung."""
    region = region_of(phone)
    if region not in ALLOWED_REGIONS:
        raise OutsideServiceAreaError(phone, region)


def timezone_for(phone: str) -> str | None:
    """Best-guess IANA timezone from a US number's area code (Etc/Unknown and multi-zone
    area codes fall back to None so the family picks)."""
    n = parse(phone)
    if n is None:
        return None
    zones = [z for z in pn_timezone.time_zones_for_number(n) if z != "Etc/Unknown"]
    if len(zones) == 1:
        return zones[0]
    if zones and all(z in US_TIMEZONES for z in zones):
        return zones[0]
    return None


def e164(phone: str) -> str:
    n = parse(phone)
    if n is None:
        digits = "".join(ch for ch in phone if ch.isdigit())
        return f"+{digits}" if digits else phone.strip()
    return phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.E164)


def pretty(phone: str) -> str:
    """(614) 555-0101 for US numbers; international format otherwise."""
    n = parse(phone)
    if n is None:
        return phone
    fmt = (
        phonenumbers.PhoneNumberFormat.NATIONAL
        if phonenumbers.region_code_for_number(n) == "US"
        else phonenumbers.PhoneNumberFormat.INTERNATIONAL
    )
    return phonenumbers.format_number(n, fmt)
