"""Database access: one small repository with the queries the app needs.

Kept deliberately thin. The rest of the code never writes SQL; it calls these
methods, which makes the behaviour easy to test and the storage easy to swap.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from sunday.models import (
    Alert,
    Base,
    Call,
    FamilyMember,
    Memory,
    Observation,
    Parent,
    QueuedMessage,
    Turn,
)


def _digits(phone: str) -> str:
    return "".join(ch for ch in (phone or "") if ch.isdigit())


class Repository:
    def __init__(self, db_path: Path | str) -> None:
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        if str(db_path) == ":memory:":
            # One shared in-memory database across threads (tests, TestClient).
            from sqlalchemy.pool import StaticPool

            self.engine = create_engine(
                "sqlite://",
                future=True,
                poolclass=StaticPool,
                connect_args={"check_same_thread": False},
            )
        else:
            self.engine = create_engine(
                f"sqlite:///{db_path}", future=True, connect_args={"check_same_thread": False}
            )
        Base.metadata.create_all(self.engine)
        self._add_missing_columns()
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    def _add_missing_columns(self) -> None:
        """Tiny forward-only migration: columns added to a model appear in old databases.
        SQLite can add a nullable column in place; that is all we ever need."""
        insp = inspect(self.engine)
        with self.engine.begin() as conn:
            for table in Base.metadata.sorted_tables:
                have = {c["name"] for c in insp.get_columns(table.name)}
                for col in table.columns:
                    if col.name not in have:
                        ddl = col.type.compile(self.engine.dialect)
                        conn.execute(
                            text(f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {ddl}')
                        )

    @contextmanager
    def session(self):
        s: Session = self._sessions()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    # --- parents ------------------------------------------------------------

    def add_parent(self, **fields: Any) -> Parent:
        with self.session() as s:
            p = Parent(**fields)
            s.add(p)
            s.flush()
            return p

    def get_parent(self, parent_id: str) -> Parent | None:
        with self.session() as s:
            p = s.get(Parent, parent_id)
            if p is not None:
                _ = p.family, p.memories  # load relationships before the session closes
            return p

    def list_parents(self, *, active_only: bool = True) -> list[Parent]:
        with self.session() as s:
            q = select(Parent)
            if active_only:
                q = q.where(Parent.active.is_(True))
            return list(s.scalars(q.order_by(Parent.created_at)))

    def update_parent(self, parent_id: str, **fields: Any) -> Parent:
        with self.session() as s:
            p = s.get(Parent, parent_id)
            if p is None:
                raise KeyError(parent_id)
            for k, v in fields.items():
                setattr(p, k, v)
            return p

    # --- family -------------------------------------------------------------

    def add_family_member(self, parent_id: str, **fields: Any) -> FamilyMember:
        with self.session() as s:
            m = FamilyMember(parent_id=parent_id, **fields)
            s.add(m)
            s.flush()
            return m

    def family_for(self, parent_id: str) -> list[FamilyMember]:
        with self.session() as s:
            return list(s.scalars(select(FamilyMember).where(FamilyMember.parent_id == parent_id)))

    def get_family_member(self, member_id: str) -> FamilyMember | None:
        with self.session() as s:
            return s.get(FamilyMember, member_id)

    def update_family_member(self, member_id: str, **fields: Any) -> FamilyMember:
        with self.session() as s:
            m = s.get(FamilyMember, member_id)
            if m is None:
                raise KeyError(member_id)
            for k, v in fields.items():
                setattr(m, k, v)
            return m

    def members_by_phone(self, phone: str) -> list[FamilyMember]:
        """Every family row for this phone, one per parent they look after. This is how a
        family member logs in: the phone Sunday already sends their notes to."""
        digits = _digits(phone)
        if not digits:
            return []
        with self.session() as s:
            rows = list(s.scalars(select(FamilyMember)))
        return [m for m in rows if _digits(m.phone) == digits]

    # --- calls --------------------------------------------------------------

    def create_call(self, parent_id: str, **fields: Any) -> Call:
        with self.session() as s:
            c = Call(parent_id=parent_id, **fields)
            s.add(c)
            s.flush()
            return c

    def get_call(self, call_id: str) -> Call | None:
        with self.session() as s:
            c = s.get(Call, call_id)
            if c is not None:
                _ = c.turns, c.observations, c.alerts
            return c

    def update_call(self, call_id: str, **fields: Any) -> Call:
        with self.session() as s:
            c = s.get(Call, call_id)
            if c is None:
                raise KeyError(call_id)
            for k, v in fields.items():
                setattr(c, k, v)
            return c

    def add_turn(self, call_id: str, speaker: str, text: str) -> Turn:
        with self.session() as s:
            t = Turn(call_id=call_id, speaker=speaker, text=text)
            s.add(t)
            s.flush()
            return t

    def recent_calls(self, parent_id: str, *, days: int = 14, limit: int = 30) -> list[Call]:
        """Calls in the window, answered or not. Unanswered calls have no started_at,
        so they are placed by when they were scheduled."""
        since = datetime.now(UTC) - timedelta(days=days)
        with self.session() as s:
            when = func.coalesce(Call.started_at, Call.scheduled_for)
            q = (
                select(Call)
                .where(Call.parent_id == parent_id)
                .where(when.is_not(None))
                .where(when >= since)
                .order_by(when.desc())
                .limit(limit)
            )
            calls = list(s.scalars(q))
            for c in calls:
                _ = c.turns, c.observations
            return calls

    def last_completed_call(self, parent_id: str) -> Call | None:
        with self.session() as s:
            q = (
                select(Call)
                .where(Call.parent_id == parent_id, Call.status == "completed")
                .order_by(Call.ended_at.desc())
                .limit(1)
            )
            c = s.scalars(q).first()
            if c is not None:
                _ = c.observations
            return c

    def consecutive_no_answers(self, parent_id: str) -> int:
        """How many *days* in a row, counting back from the latest attempt, had at least one
        unanswered call and no answered one. Two rings on the same morning are one day."""
        parent = self.get_parent(parent_id)
        with self.session() as s:
            q = (
                select(Call)
                .where(Call.parent_id == parent_id)
                .where(Call.status.in_(["completed", "no_answer", "failed"]))
                .order_by(
                    func.coalesce(Call.started_at, Call.scheduled_for).desc(),
                )
                .limit(40)
            )
            calls = list(s.scalars(q))
        days: dict = {}
        order: list = []
        for c in calls:
            when = c.started_at or c.scheduled_for
            if when is None:
                continue
            d = (parent.local_now(when) if parent else when).date()
            if d not in days:
                days[d] = set()
                order.append(d)
            days[d].add(c.status)
        n = 0
        for d in order:
            if "completed" in days[d] or "no_answer" not in days[d]:
                break
            n += 1
        return n

    def call_by_recording_sid(self, recording_sid: str) -> Call | None:
        if not recording_sid:
            return None
        with self.session() as s:
            q = select(Call).where(Call.recording_sid == recording_sid).limit(1)
            return s.scalars(q).first()

    def call_by_provider_id(self, provider_call_id: str) -> Call | None:
        if not provider_call_id:
            return None
        with self.session() as s:
            q = select(Call).where(Call.provider_call_id == provider_call_id).limit(1)
            c = s.scalars(q).first()
            if c is not None:
                _ = c.turns, c.observations, c.alerts
            return c

    # --- observations, alerts, memories -------------------------------------

    def add_observations(
        self, call_id: str, parent_id: str, items: list[dict]
    ) -> list[Observation]:
        with self.session() as s:
            out = [Observation(call_id=call_id, parent_id=parent_id, **i) for i in items]
            s.add_all(out)
            s.flush()
            return out

    def observations_since(self, parent_id: str, *, days: int) -> list[Observation]:
        since = datetime.now(UTC) - timedelta(days=days)
        with self.session() as s:
            q = (
                select(Observation)
                .where(Observation.parent_id == parent_id)
                .where(Observation.at >= since)
                .order_by(Observation.at)
            )
            return list(s.scalars(q))

    def pending_follow_ups(self, parent_id: str, *, days: int = 7) -> list[Observation]:
        return [o for o in self.observations_since(parent_id, days=days) if o.follow_up]

    def add_alert(
        self,
        parent_id: str,
        *,
        tier: str,
        kind: str,
        text: str,
        call_id: str | None = None,
        evidence: str = "",
    ) -> Alert:
        with self.session() as s:
            a = Alert(
                parent_id=parent_id,
                call_id=call_id,
                tier=tier,
                kind=kind,
                text=text,
                evidence=evidence,
            )
            s.add(a)
            s.flush()
            return a

    def unnotified_alerts(self, parent_id: str) -> list[Alert]:
        with self.session() as s:
            q = (
                select(Alert)
                .where(Alert.parent_id == parent_id)
                .where(Alert.notified_at.is_(None))
                .order_by(Alert.at)
            )
            return list(s.scalars(q))

    def mark_alert_notified(self, alert_id: str) -> None:
        with self.session() as s:
            a = s.get(Alert, alert_id)
            if a is not None:
                a.notified_at = datetime.now(UTC)

    def get_alert(self, alert_id: str) -> Alert | None:
        with self.session() as s:
            return s.get(Alert, alert_id)

    def acknowledge_alert(self, alert_id: str, parent_id: str, by: str) -> bool:
        with self.session() as s:
            a = s.get(Alert, alert_id)
            if a is None or a.parent_id != parent_id:
                return False
            a.acknowledged_by = by
            return True

    def alerts_since(self, parent_id: str, *, days: int) -> list[Alert]:
        since = datetime.now(UTC) - timedelta(days=days)
        with self.session() as s:
            q = (
                select(Alert)
                .where(Alert.parent_id == parent_id, Alert.at >= since)
                .order_by(Alert.at)
            )
            return list(s.scalars(q))

    def add_memory(
        self, parent_id: str, text: str, *, topic: str = "", source: str = "", evidence: str = ""
    ) -> Memory:
        """Remember something. The same fact heard again (ignoring case, spacing and a
        trailing full stop) is not stored twice; the model restates "the grandchildren are
        Ayaan and Zara" most weeks."""
        key = " ".join(text.lower().split()).rstrip(".")
        with self.session() as s:
            for m in s.scalars(
                select(Memory).where(Memory.parent_id == parent_id, Memory.active.is_(True))
            ):
                if " ".join(m.text.lower().split()).rstrip(".") == key:
                    return m
            m = Memory(
                parent_id=parent_id, text=text, topic=topic, source=source, evidence=evidence
            )
            s.add(m)
            s.flush()
            return m

    def deactivate_memory(self, memory_id: int, parent_id: str) -> bool:
        with self.session() as s:
            m = s.get(Memory, memory_id)
            if m is None or m.parent_id != parent_id:
                return False
            m.active = False
            return True

    def memories_for(self, parent_id: str) -> list[Memory]:
        with self.session() as s:
            q = (
                select(Memory)
                .where(Memory.parent_id == parent_id, Memory.active.is_(True))
                .order_by(Memory.at)
            )
            return list(s.scalars(q))

    # --- queued messages ------------------------------------------------------

    def queue_message(
        self,
        parent_id: str,
        *,
        from_name: str,
        text: str,
        kind: str = "say",
        remind_on: date | None = None,
    ) -> QueuedMessage:
        with self.session() as s:
            m = QueuedMessage(
                parent_id=parent_id, from_name=from_name, text=text, kind=kind, remind_on=remind_on
            )
            s.add(m)
            s.flush()
            return m

    def undelivered_messages(
        self, parent_id: str, *, on: date | None = None, include_future: bool = False
    ) -> list[QueuedMessage]:
        """Messages not yet passed on. Dated reminders are held back until their day unless
        ``include_future`` (for `sunday show`). ``on`` is the parent's local date."""
        with self.session() as s:
            q = (
                select(QueuedMessage)
                .where(QueuedMessage.parent_id == parent_id)
                .where(QueuedMessage.delivered_call_id == "")
                .order_by(QueuedMessage.created_at)
            )
            items = list(s.scalars(q))
        if include_future:
            return items
        today = on or datetime.now(UTC).date()
        return [m for m in items if m.remind_on is None or m.remind_on <= today]

    def parent_by_phone(self, phone: str) -> Parent | None:
        """Who is calling us? Matched on digits only, so "+92 300..." and "+92300..." agree."""
        digits = _digits(phone)
        if not digits:
            return None
        for p in self.list_parents():
            if _digits(p.phone) == digits:
                return p
        return None

    def delete_message(self, message_id: str, parent_id: str) -> bool:
        """Remove an undelivered queued message. Scoped to the parent so a family member
        can only remove items on their own parent's queue."""
        with self.session() as s:
            m = s.get(QueuedMessage, message_id)
            if m is None or m.parent_id != parent_id or m.delivered_call_id:
                return False
            s.delete(m)
            return True

    def mark_message_delivered(self, message_id: str, call_id: str, response: str = "") -> None:
        with self.session() as s:
            m = s.get(QueuedMessage, message_id)
            if m is not None:
                m.delivered_call_id = call_id
                m.response = response
