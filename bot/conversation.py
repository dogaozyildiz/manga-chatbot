"""The bot's whole behaviour, with no knowledge of Telegram or WhatsApp.

Everything a channel does is: take `(channel, user_id, text)`, call `handle`,
render the `Reply`. Keeping transport out of here is what makes the WhatsApp
adapter small, and it is what lets the tests exercise the real conversation
without a network or a bot token.
"""

from __future__ import annotations

from bot.escalation import Escalator, reason_for
from bot.ratelimit import RateLimiter
from bot.rag import RagClient
from bot.schema import Channel, RagAnswer, Reply
from bot.store import Store

AWAITING_TITLES = "awaiting_titles"
AWAITING_CONTACT = "awaiting_contact"

WELCOME = (
    "Manga catalogue assistant.\n\n"
    "I answer from a catalogue of 500 series and 43 industry articles, and I "
    "show the source for everything I say. If it isn't in there, I'll tell you "
    "so instead of inventing an answer.\n\n"
    "Try:\n"
    "• completed romance manhwa under 100 chapters\n"
    "• recommend something like Solo Leveling\n"
    "• what is scanlation?\n\n"
    "/subscribe — get update alerts\n"
    "/help — what else I can do"
)

HELP = (
    "Ask me about manga and manhwa — specific series, or how the industry "
    "works.\n\n"
    "I handle real constraints, not just keywords: \"completed romance manhwa "
    "under 100 chapters\" becomes an actual filter, so nothing over 100 "
    "chapters comes back.\n\n"
    "/subscribe — tell me what you read, get update alerts\n"
    "/stop — delete everything I've stored about you\n"
    "/status — what I've got on file\n"
    "/cancel — back out of the current question"
)


class Conversation:
    def __init__(
        self,
        rag: RagClient,
        store: Store,
        escalator: Escalator,
        limiter: RateLimiter | None = None,
    ) -> None:
        self._rag = rag
        self._store = store
        self._escalator = escalator
        self._limiter = limiter or RateLimiter()

    async def handle(
        self, channel: Channel, user_id: str, text: str, *, display_name: str = ""
    ) -> Reply:
        text = (text or "").strip()
        if not text:
            return Reply(body="Send me a question about manga or manhwa.")

        if text.startswith("/"):
            return await self._command(channel, user_id, text, display_name)

        # A capture flow in progress owns the next message, so "solo leveling"
        # is stored as an answer rather than fired at the backend as a question.
        pending = await self._store.pending_state(channel.value, user_id)
        if pending:
            return await self._capture(channel, user_id, text, pending)

        return await self._question(channel, user_id, text)

    # -- commands ----------------------------------------------------------

    async def _command(
        self, channel: Channel, user_id: str, text: str, display_name: str
    ) -> Reply:
        # "/start@SomeBot extra words" -> "start"
        command = text.split()[0].lstrip("/").split("@")[0].lower()

        if command == "start":
            return Reply(body=WELCOME)
        if command == "help":
            return Reply(body=HELP)

        if command == "subscribe":
            await self._store.upsert(
                channel.value,
                user_id,
                pending_state=AWAITING_TITLES,
                display_name=display_name or None,
            )
            return Reply(
                body=(
                    "I'll flag new chapters for the series you follow.\n\n"
                    "Which ones do you read? Just list them in one message."
                )
            )

        if command == "cancel":
            await self._store.upsert(channel.value, user_id, pending_state=None)
            return Reply(body="Dropped it. Ask me anything.")

        if command == "stop":
            existed = await self._store.forget(channel.value, user_id)
            return Reply(
                body=(
                    "Deleted everything I had on you."
                    if existed
                    else "I wasn't storing anything about you."
                )
            )

        if command == "status":
            return await self._status(channel, user_id)

        return Reply(body=f"I don't know /{command}. Try /help.")

    async def _status(self, channel: Channel, user_id: str) -> Reply:
        subscriber = await self._store.get(channel.value, user_id)
        if subscriber is None:
            return Reply(body="Nothing on file. /subscribe to change that.")
        lines = ["On file for you:"]
        lines.append(f"• series: {subscriber.titles or '(none yet)'}")
        lines.append(f"• contact: {subscriber.contact or '(none)'}")
        lines.append("\n/stop deletes all of it.")
        return Reply(body="\n".join(lines))

    # -- preference capture ------------------------------------------------

    async def _capture(
        self, channel: Channel, user_id: str, text: str, pending: str
    ) -> Reply:
        if pending == AWAITING_TITLES:
            await self._store.upsert(
                channel.value, user_id, titles=text, pending_state=AWAITING_CONTACT
            )
            return Reply(
                body=(
                    "Got it.\n\n"
                    "Want alerts anywhere other than here? Send an email address, "
                    "or say skip."
                )
            )

        if pending == AWAITING_CONTACT:
            skipped = text.strip().lower() in {"skip", "no", "nope", "none", "nah"}
            await self._store.upsert(
                channel.value,
                user_id,
                contact=None if skipped else text,
                pending_state=None,
            )
            subscriber = await self._store.get(channel.value, user_id)
            titles = (subscriber.titles if subscriber else "") or "your list"
            return Reply(
                body=(
                    f"You're set — I'll watch {titles}.\n\n"
                    + ("" if skipped else "I'll use that contact for alerts.\n\n")
                    + "/status to see it, /stop to delete it."
                )
            )

        # An unknown state means a version of this bot wrote something we no
        # longer understand. Clear it rather than trapping the user in it.
        await self._store.upsert(channel.value, user_id, pending_state=None)
        return Reply(body="Lost my place there — ask me again?")

    # -- questions ---------------------------------------------------------

    async def _question(self, channel: Channel, user_id: str, text: str) -> Reply:
        denial = self._limiter.check(f"{channel.value}:{user_id}")
        if denial:
            return Reply(body=denial)

        answer = await self._rag.ask(text)

        reason = reason_for(answer)
        if reason:
            await self._escalator.escalate(channel.value, user_id, text, reason)

        return self._to_reply(answer, reason)

    def _to_reply(self, answer: RagAnswer, reason: str | None) -> Reply:
        if answer.unavailable:
            return Reply(
                body=(
                    "I can't reach the catalogue right now. I've flagged it — "
                    "try again shortly."
                ),
                escalated=True,
            )

        if answer.degraded:
            # Retrieval worked, generation didn't. Showing the matched sources
            # is far more useful than an apology, and it is honest about which
            # half of the system is down.
            return Reply(
                body=(
                    "I can't write an answer at the moment, but these are the "
                    "catalogue entries that match:"
                ),
                filters=answer.applied_filters,
                citations=answer.citations,
                note="retrieval only — answer generation is unavailable",
                escalated=True,
            )

        if answer.refused:
            return Reply(
                body=(
                    answer.text
                    or "I don't have anything on that in the catalogue."
                ),
                filters=answer.applied_filters,
                citations=answer.citations,
                note="flagged for a human — I'd rather say nothing than guess",
                escalated=True,
            )

        return Reply(
            body=answer.text,
            filters=answer.applied_filters,
            citations=answer.citations,
            rejected_claims=answer.rejected_claims,
        )
