"""The nouns of Sunday, as SQLAlchemy models.

* **Parent** - the person we call. Name, phone, language, call time, how they
  like to be addressed, medications and routines the call should ask about.
* **FamilyMember** - who pays attention. Phone for WhatsApp/SMS, role, and
  which alert tiers they want.
* **Call** - one attempt: scheduled, ringing, answered, no-answer, completed.
  Holds the transcript turns, the summary, and the extracted observations.
* **Turn** - one utterance in a call, speaker + text + timestamp.
* **Observation** - a fact noted during a call ("took blood pressure tablets",
  "mentioned knee pain", "plumber still hasn't come"), typed and dated, so
  trends can be computed and tomorrow's call can follow up.
* **Alert** - something the family must know, with a tier.
* **QueuedMessage** - something a family member wants said or asked on the
  next call.
* **Memory** - durable facts about the parent the call should know
  (grandchildren's names, the knee, the neighbour), with provenance.

Everything is local SQLite by default. A parent's conversations are sensitive;
this never leaves the operator's machine unless they choose to deploy it.
"""

from __future__ import annotations

import secrets
from datetime import UTC, date, datetime
from typing import Literal

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

CallStatus = Literal["scheduled", "in_progress", "completed", "no_answer", "failed", "cancelled"]
AlertTier = Literal["info", "today", "now"]
ObservationKind = Literal[
    "medication",
    "sleep",
    "meal",
    "mood",
    "pain",
    "mobility",
    "memory",
    "social",
    "safety",
    "household",
    "plan",
    "story",
    "other",
]


def now() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


class Base(DeclarativeBase):
    pass


class Parent(Base):
    __tablename__ = "parents"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("par"))
    name: Mapped[str] = mapped_column(String, nullable=False)
    preferred_name: Mapped[str] = mapped_column(String, default="")  # "Amma", "Mr Khan"
    phone: Mapped[str] = mapped_column(String, nullable=False)
    language: Mapped[str] = mapped_column(String, default="en")  # BCP-47-ish: en, ur, hi, pa
    timezone: Mapped[str] = mapped_column(String, default="UTC")
    call_time: Mapped[str] = mapped_column(String, default="09:00")  # local HH:MM
    call_days: Mapped[list] = mapped_column(
        JSON, default=lambda: ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    )
    medications: Mapped[list] = mapped_column(JSON, default=list)  # [{"name","when"}]
    notes: Mapped[str] = mapped_column(Text, default="")  # free text from the family
    introduced_by: Mapped[str] = mapped_column(String, default="")  # "your daughter Hira"
    consent_recording: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_digest_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    family: Mapped[list[FamilyMember]] = relationship(
        back_populates="parent", cascade="all, delete-orphan"
    )
    calls: Mapped[list[Call]] = relationship(back_populates="parent", cascade="all, delete-orphan")
    memories: Mapped[list[Memory]] = relationship(
        back_populates="parent", cascade="all, delete-orphan"
    )
    queued: Mapped[list[QueuedMessage]] = relationship(
        back_populates="parent", cascade="all, delete-orphan"
    )

    def address(self) -> str:
        return self.preferred_name or self.name.split()[0]

    def local_now(self, at: datetime | None = None) -> datetime:
        from zoneinfo import ZoneInfo

        at = at or datetime.now(UTC)
        try:
            return at.astimezone(ZoneInfo(self.timezone or "UTC"))
        except Exception:  # noqa: BLE001 - a bad tz string must not break a call
            return at

    def local_today(self) -> date:
        return self.local_now().date()


class FamilyMember(Base):
    __tablename__ = "family_members"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("fam"))
    parent_id: Mapped[str] = mapped_column(ForeignKey("parents.id"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    relation: Mapped[str] = mapped_column(String, default="")  # daughter, son
    phone: Mapped[str] = mapped_column(String, nullable=False)
    channel: Mapped[str] = mapped_column(String, default="whatsapp")  # whatsapp | sms | console
    min_alert_tier: Mapped[str] = mapped_column(String, default="info")  # info | today | now
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)

    parent: Mapped[Parent] = relationship(back_populates="family")


class Call(Base):
    __tablename__ = "calls"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("call"))
    parent_id: Mapped[str] = mapped_column(ForeignKey("parents.id"), nullable=False)
    status: Mapped[str] = mapped_column(String, default="scheduled")
    direction: Mapped[str] = mapped_column(String, default="outbound")  # outbound | inbound
    provider_call_id: Mapped[str] = mapped_column(String, default="")  # Twilio CallSid etc.
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    summary: Mapped[str] = mapped_column(Text, default="")
    mood: Mapped[str] = mapped_column(String, default="")  # bright | ok | low | unclear
    end_reason: Mapped[str] = mapped_column(String, default="")
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    parent: Mapped[Parent] = relationship(back_populates="calls")
    turns: Mapped[list[Turn]] = relationship(
        back_populates="call", cascade="all, delete-orphan", order_by="Turn.at"
    )
    observations: Mapped[list[Observation]] = relationship(
        back_populates="call", cascade="all, delete-orphan"
    )
    alerts: Mapped[list[Alert]] = relationship(back_populates="call", cascade="all, delete-orphan")

    def transcript(self) -> str:
        return "\n".join(f"{t.speaker}: {t.text}" for t in self.turns)


class Turn(Base):
    __tablename__ = "turns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    call_id: Mapped[str] = mapped_column(ForeignKey("calls.id"), nullable=False)
    speaker: Mapped[str] = mapped_column(String, nullable=False)  # sunday | parent
    text: Mapped[str] = mapped_column(Text, nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    call: Mapped[Call] = relationship(back_populates="turns")


class Observation(Base):
    __tablename__ = "observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    call_id: Mapped[str] = mapped_column(ForeignKey("calls.id"), nullable=False)
    parent_id: Mapped[str] = mapped_column(ForeignKey("parents.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[str] = mapped_column(String, default="")  # e.g. "taken" | "missed" | "7h"
    evidence: Mapped[str] = mapped_column(Text, default="")  # what the parent actually said
    follow_up: Mapped[bool] = mapped_column(Boolean, default=False)  # ask about it tomorrow
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    call: Mapped[Call] = relationship(back_populates="observations")


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("alt"))
    call_id: Mapped[str | None] = mapped_column(ForeignKey("calls.id"))
    parent_id: Mapped[str] = mapped_column(ForeignKey("parents.id"), nullable=False)
    tier: Mapped[str] = mapped_column(String, nullable=False)  # info | today | now
    kind: Mapped[str] = mapped_column(String, nullable=False)  # fall | pain | no_answer | ...
    text: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[str] = mapped_column(Text, default="")
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    acknowledged_by: Mapped[str] = mapped_column(String, default="")
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    call: Mapped[Call | None] = relationship(back_populates="alerts")


class QueuedMessage(Base):
    __tablename__ = "queued_messages"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("msg"))
    parent_id: Mapped[str] = mapped_column(ForeignKey("parents.id"), nullable=False)
    from_name: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, default="say")  # say | ask | remind
    text: Mapped[str] = mapped_column(Text, nullable=False)
    # Say it on or after this local date (the appointment is Tuesday: remind on Monday and
    # Tuesday, not today). None means the next call.
    remind_on: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    delivered_call_id: Mapped[str] = mapped_column(String, default="")
    response: Mapped[str] = mapped_column(Text, default="")  # what the parent said back

    parent: Mapped[Parent] = relationship(back_populates="queued")


class Memory(Base):
    """A durable fact about the parent the call should know."""

    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    parent_id: Mapped[str] = mapped_column(ForeignKey("parents.id"), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    topic: Mapped[str] = mapped_column(String, default="")  # family | health | home | likes
    source: Mapped[str] = mapped_column(String, default="")  # "family" | call id
    evidence: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    parent: Mapped[Parent] = relationship(back_populates="memories")
