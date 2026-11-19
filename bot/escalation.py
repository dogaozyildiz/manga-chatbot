"""When to stop guessing and involve a human.

The trigger is not a heuristic invented here. `refused` is computed by the
backend and means one of three specific things: nothing was retrieved, the
model declined, or *every* claim it made failed verbatim verification against
the sources. That last case is the valuable one — it catches an answer that
looked fine and wasn't.

So the handoff is grounded in the same machinery as the answers: the bot
escalates when the system it trusts says it has nothing it can stand behind.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import httpx

from bot.config import Settings
from bot.schema import RagAnswer
from bot.store import Store

log = logging.getLogger(__name__)

Notifier = Callable[[str], Awaitable[None]]


def telegram_notifier(settings: Settings) -> Notifier | None:
    """Push escalations to the operator's Telegram from any process.

    Whichever channel a user is on, the human who has to answer is the same
    person, and they already have a Telegram chat with the bot. Posting
    straight to the Bot API avoids standing up a second client just to send one
    message. Returns None when no operator is configured, which is a supported
    setup — escalations are still recorded.
    """
    if not (settings.telegram_bot_token and settings.admin_chat_id):
        return None

    async def notify(text: str) -> None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"https://api.telegram.org/bot{settings.telegram_bot_token}"
                "/sendMessage",
                json={"chat_id": settings.admin_chat_id, "text": text},
            )

    return notify


def reason_for(answer: RagAnswer) -> str | None:
    """Why this answer needs a human, or None if it doesn't."""
    if answer.unavailable:
        return "unavailable"
    if answer.refused:
        return "refused"
    if answer.degraded:
        return "degraded"
    return None


class Escalator:
    def __init__(self, store: Store, notifier: Notifier | None = None) -> None:
        self._store = store
        self._notifier = notifier

    def set_notifier(self, notifier: Notifier | None) -> None:
        """Channels install this once they have a client able to send.

        Kept settable because the conversation core is built before any channel
        exists, and because an operator chat is optional — with none configured
        the escalation is still recorded, just not pushed.
        """
        self._notifier = notifier

    async def escalate(
        self, channel: str, user_id: str, question: str, reason: str
    ) -> None:
        await self._store.record_escalation(channel, user_id, question, reason)
        if self._notifier is None:
            return
        try:
            await self._notifier(
                f"Escalation ({reason}) from {channel}:{user_id}\n\n{question}"
            )
        except Exception:
            # A failed operator notification must never break the user's reply.
            # The row is already written, so nothing is actually lost -- but the
            # operator must be able to see that pushes are failing.
            log.warning("escalation notification failed", exc_info=True)
