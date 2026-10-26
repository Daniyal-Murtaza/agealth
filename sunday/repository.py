"""Database access: one small repository with the queries the app needs.

Kept deliberately thin. The rest of the code never writes SQL; it calls these
methods, which makes the behaviour easy to test and the storage easy to swap.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select
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
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

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
        since = datetime.now(UTC) - timedelta(days=days)
        with self.session() as s:
            q = (
                select(Call)
                .where(Call.parent_id == parent_id)
                .where(Call.started_at.is_not(None))
                .where(Call.started_at >= since)
                .order_by(Call.started_at.desc())
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
        """How many of the most recent call attempts in a row went unanswered."""
        with self.session() as s:
            q = (
                select(Call)
                .where(Call.parent_id == parent_id)
                .where(Call.status.in_(["completed", "no_answer", "failed"]))
                .order_by(Call.scheduled_for.desc(), Call.started_at.desc())
                .limit(10)
            )
            n = 0
            for c in s.scalars(q):
                if c.status == "no_answer":
                    n += 1
                else:
                    break
            return n

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
        with self.session() as s:
            m = Memory(
                parent_id=parent_id, text=text, topic=topic, source=source, evidence=evidence
            )
            s.add(m)
            s.flush()
            return m

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
        self, parent_id: str, *, from_name: str, text: str, kind: str = "say"
    ) -> QueuedMessage:
        with self.session() as s:
            m = QueuedMessage(parent_id=parent_id, from_name=from_name, text=text, kind=kind)
            s.add(m)
            s.flush()
            return m

    def undelivered_messages(self, parent_id: str) -> list[QueuedMessage]:
        with self.session() as s:
            q = (
                select(QueuedMessage)
                .where(QueuedMessage.parent_id == parent_id)
                .where(QueuedMessage.delivered_call_id == "")
                .order_by(QueuedMessage.created_at)
            )
            return list(s.scalars(q))

    def mark_message_delivered(self, message_id: str, call_id: str, response: str = "") -> None:
        with self.session() as s:
            m = s.get(QueuedMessage, message_id)
            if m is not None:
                m.delivered_call_id = call_id
                m.response = response
