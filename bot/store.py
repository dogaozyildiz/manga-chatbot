"""Preference capture and escalation records.

SQLite by default, because a portfolio bot that needs a database server before
it will start is a bot nobody clones and runs. `DATABASE_URL` is a SQLAlchemy
URL, so pointing it at Postgres is a config change, not a code change.

`pending_state` is a column rather than an in-process dict on purpose: a
capture flow that forgets which question it asked whenever the process restarts
is exactly the bug that makes chatbots feel broken.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, String, Text, UniqueConstraint, func, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import StaticPool


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Subscriber(Base):
    __tablename__ = "subscribers"
    __table_args__ = (UniqueConstraint("channel", "user_id", name="uq_channel_user"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(16))
    user_id: Mapped[str] = mapped_column(String(64))
    display_name: Mapped[str | None] = mapped_column(String(128), default=None)
    # Free text, as typed. Normalising "solo leveling, ORV" into a series table
    # would need the catalogue, and the point of the flow is capture, not
    # resolution.
    titles: Mapped[str | None] = mapped_column(Text, default=None)
    contact: Mapped[str | None] = mapped_column(String(255), default=None)
    pending_state: Mapped[str | None] = mapped_column(String(32), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class Escalation(Base):
    __tablename__ = "escalations"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(16))
    user_id: Mapped[str] = mapped_column(String(64))
    question: Mapped[str] = mapped_column(Text)
    # Why the bot handed off: "refused", "degraded", "unavailable".
    reason: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Store:
    def __init__(self, url: str) -> None:
        kwargs: dict = {}
        if ":memory:" in url:
            # Every connection to an in-memory SQLite database gets its own
            # empty database, so a pooled engine would see the tables vanish
            # between sessions. StaticPool keeps one connection.
            kwargs = {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}
        self._engine = create_async_engine(url, **kwargs)
        self._session = async_sessionmaker(self._engine, expire_on_commit=False)

    async def init(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self._engine.dispose()

    # -- subscribers -------------------------------------------------------

    async def get(self, channel: str, user_id: str) -> Subscriber | None:
        async with self._session() as session:
            return await self._get(session, channel, user_id)

    @staticmethod
    async def _get(
        session: AsyncSession, channel: str, user_id: str
    ) -> Subscriber | None:
        result = await session.execute(
            select(Subscriber).where(
                Subscriber.channel == channel, Subscriber.user_id == user_id
            )
        )
        return result.scalar_one_or_none()

    async def upsert(self, channel: str, user_id: str, **fields) -> Subscriber:
        """Create or update one subscriber. Only the given fields are touched."""
        async with self._session() as session:
            subscriber = await self._get(session, channel, user_id)
            if subscriber is None:
                subscriber = Subscriber(channel=channel, user_id=user_id)
                session.add(subscriber)
            for key, value in fields.items():
                setattr(subscriber, key, value)
            subscriber.updated_at = _now()
            await session.commit()
            return subscriber

    async def pending_state(self, channel: str, user_id: str) -> str | None:
        subscriber = await self.get(channel, user_id)
        return subscriber.pending_state if subscriber else None

    async def forget(self, channel: str, user_id: str) -> bool:
        """Delete a subscriber. Returns whether there was one.

        `/stop` has to actually delete. A bot that captures contact details and
        then only pretends to forget them is a liability, not a feature.
        """
        async with self._session() as session:
            subscriber = await self._get(session, channel, user_id)
            if subscriber is None:
                return False
            await session.delete(subscriber)
            await session.commit()
            return True

    # -- escalations -------------------------------------------------------

    async def record_escalation(
        self, channel: str, user_id: str, question: str, reason: str
    ) -> Escalation:
        async with self._session() as session:
            escalation = Escalation(
                channel=channel, user_id=user_id, question=question, reason=reason
            )
            session.add(escalation)
            await session.commit()
            return escalation

    async def counts(self) -> dict[str, int]:
        async with self._session() as session:
            subscribers = await session.scalar(
                select(func.count()).select_from(Subscriber)
            )
            escalations = await session.scalar(
                select(func.count()).select_from(Escalation)
            )
        return {"subscribers": subscribers or 0, "escalations": escalations or 0}
