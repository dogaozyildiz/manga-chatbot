"""The bot's behaviour, end to end, with the backend and transport replaced."""

from __future__ import annotations

import httpx
import pytest

from bot.communities import CommunityLookupError
from bot.ratelimit import RateLimiter
from bot.schema import Channel

from tests.conftest import (
    FakeCommunities,
    ask_payload,
    community,
    json_handler,
    retrieve_payload,
    route_handler,
)

USER = "42"


class TestCommands:
    async def test_start_introduces_the_bot(self, make_conversation):
        conversation = make_conversation(json_handler(ask_payload()))
        reply = await conversation.handle(Channel.TELEGRAM, USER, "/start")
        assert "catalogue" in reply.body.lower()
        assert "/subscribe" in reply.body

    async def test_help_lists_commands(self, make_conversation):
        conversation = make_conversation(json_handler(ask_payload()))
        reply = await conversation.handle(Channel.TELEGRAM, USER, "/help")
        assert "/stop" in reply.body

    async def test_command_with_bot_suffix(self, make_conversation):
        """Group chats deliver `/start@MyBot`."""
        conversation = make_conversation(json_handler(ask_payload()))
        reply = await conversation.handle(Channel.TELEGRAM, USER, "/start@MangaBot")
        assert "/subscribe" in reply.body

    async def test_unknown_command(self, make_conversation):
        conversation = make_conversation(json_handler(ask_payload()))
        reply = await conversation.handle(Channel.TELEGRAM, USER, "/wat")
        assert "/help" in reply.body

    async def test_empty_message(self, make_conversation):
        conversation = make_conversation(json_handler(ask_payload()))
        reply = await conversation.handle(Channel.TELEGRAM, USER, "   ")
        assert reply.body


class TestGroundedAnswers:
    async def test_answer_carries_filters_and_citations(self, make_conversation):
        conversation = make_conversation(json_handler(ask_payload()))
        reply = await conversation.handle(
            Channel.TELEGRAM, USER, "completed romance manhwa under 100 chapters"
        )

        assert reply.body.startswith("Two completed romance manhwa")
        assert reply.filters == ["Korean", "finished", "<100 chapters", "Romance"]
        assert reply.citations[0].title == "Rosen Blood"
        assert reply.rejected_claims == 1
        assert reply.escalated is False

    async def test_recommendation_goes_through_the_same_path(
        self, make_conversation
    ):
        """Recommendation is a query intent the backend already routes; it
        needs no separate service on this side."""
        payload = ask_payload(
            question="recommend something like Solo Leveling under 100 chapters",
            answer="Try these.",
            route="catalogue",
            applied_filters=["<100 chapters"],
        )
        conversation = make_conversation(json_handler(payload))
        reply = await conversation.handle(
            Channel.TELEGRAM, USER, "recommend something like Solo Leveling"
        )

        assert reply.filters == ["<100 chapters"]
        assert reply.citations
        assert reply.escalated is False

    async def test_question_is_not_swallowed_by_a_stale_state(
        self, make_conversation, store
    ):
        """An unrecognised pending state must release the user, not trap them."""
        await store.upsert(Channel.TELEGRAM.value, USER, pending_state="from_v0")
        conversation = make_conversation(json_handler(ask_payload()))
        reply = await conversation.handle(Channel.TELEGRAM, USER, "hello?")

        assert reply.body
        assert await store.pending_state(Channel.TELEGRAM.value, USER) is None


class TestRefusalAndEscalation:
    async def test_refusal_escalates_and_says_so(self, make_conversation, store):
        payload = ask_payload(
            answer="I couldn't find anything that covers that.",
            refused=True,
            claims=[],
        )
        conversation = make_conversation(json_handler(payload))
        reply = await conversation.handle(
            Channel.TELEGRAM, USER, "who won the 2019 Cricket World Cup?"
        )

        assert reply.escalated is True
        assert "couldn't find" in reply.body
        assert reply.note and "human" in reply.note
        assert (await store.counts())["escalations"] == 1

    async def test_escalation_records_the_question(self, make_conversation, store):
        conversation = make_conversation(
            json_handler(ask_payload(refused=True, claims=[]))
        )
        await conversation.handle(Channel.TELEGRAM, USER, "off-corpus question")

        from sqlalchemy import select

        from bot.store import Escalation

        async with store._session() as session:
            row = (await session.execute(select(Escalation))).scalar_one()
        assert row.question == "off-corpus question"
        assert row.reason == "refused"
        assert row.channel == "telegram"

    async def test_backend_down_escalates(self, make_conversation, store):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        conversation = make_conversation(handler)
        reply = await conversation.handle(Channel.TELEGRAM, USER, "anything")

        assert reply.escalated is True
        assert "can't reach" in reply.body
        assert (await store.counts())["escalations"] == 1

    async def test_no_quota_shows_sources_instead_of_apologising(
        self, make_conversation
    ):
        conversation = make_conversation(
            route_handler(
                ask=lambda r: httpx.Response(503, json={"detail": "no key"}),
                retrieve=retrieve_payload(),
            )
        )
        reply = await conversation.handle(Channel.TELEGRAM, USER, "anything")

        assert reply.citations
        assert reply.note and "retrieval only" in reply.note
        assert reply.escalated is True

    async def test_good_answer_does_not_escalate(self, make_conversation, store):
        conversation = make_conversation(json_handler(ask_payload()))
        await conversation.handle(Channel.TELEGRAM, USER, "a good question")
        assert (await store.counts())["escalations"] == 0


class TestPreferenceCapture:
    async def test_full_flow(self, make_conversation, store):
        conversation = make_conversation(json_handler(ask_payload()))

        first = await conversation.handle(Channel.TELEGRAM, USER, "/subscribe")
        assert "which ones" in first.body.lower()

        second = await conversation.handle(
            Channel.TELEGRAM, USER, "Solo Leveling, Omniscient Reader"
        )
        assert "email" in second.body.lower()

        third = await conversation.handle(Channel.TELEGRAM, USER, "me@example.com")
        assert "set" in third.body.lower()

        subscriber = await store.get(Channel.TELEGRAM.value, USER)
        assert subscriber.titles == "Solo Leveling, Omniscient Reader"
        assert subscriber.contact == "me@example.com"
        assert subscriber.pending_state is None

    async def test_contact_can_be_skipped(self, make_conversation, store):
        conversation = make_conversation(json_handler(ask_payload()))
        await conversation.handle(Channel.TELEGRAM, USER, "/subscribe")
        await conversation.handle(Channel.TELEGRAM, USER, "Solo Leveling")
        await conversation.handle(Channel.TELEGRAM, USER, "skip")

        subscriber = await store.get(Channel.TELEGRAM.value, USER)
        assert subscriber.contact is None
        assert subscriber.titles == "Solo Leveling"
        assert subscriber.pending_state is None

    async def test_capture_does_not_reach_the_backend(self, store, make_rag,
                                                      open_limiter):
        """While capturing, a message is an answer, not a question."""
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            return httpx.Response(200, json=ask_payload())

        from bot.conversation import Conversation
        from bot.escalation import Escalator

        conversation = Conversation(
            make_rag(handler), store, Escalator(store), open_limiter
        )
        await conversation.handle(Channel.TELEGRAM, USER, "/subscribe")
        await conversation.handle(Channel.TELEGRAM, USER, "Solo Leveling")
        assert calls == []

    async def test_cancel_releases_the_flow(self, make_conversation, store):
        conversation = make_conversation(json_handler(ask_payload()))
        await conversation.handle(Channel.TELEGRAM, USER, "/subscribe")
        await conversation.handle(Channel.TELEGRAM, USER, "/cancel")

        assert await store.pending_state(Channel.TELEGRAM.value, USER) is None
        reply = await conversation.handle(Channel.TELEGRAM, USER, "a question")
        assert reply.citations

    async def test_status_reports_what_is_stored(self, make_conversation):
        conversation = make_conversation(json_handler(ask_payload()))
        await conversation.handle(Channel.TELEGRAM, USER, "/subscribe")
        await conversation.handle(Channel.TELEGRAM, USER, "Solo Leveling")
        await conversation.handle(Channel.TELEGRAM, USER, "skip")

        reply = await conversation.handle(Channel.TELEGRAM, USER, "/status")
        assert "Solo Leveling" in reply.body

    async def test_status_with_nothing_stored(self, make_conversation):
        conversation = make_conversation(json_handler(ask_payload()))
        reply = await conversation.handle(Channel.TELEGRAM, USER, "/status")
        assert "/subscribe" in reply.body

    async def test_stop_actually_deletes(self, make_conversation, store):
        conversation = make_conversation(json_handler(ask_payload()))
        await conversation.handle(Channel.TELEGRAM, USER, "/subscribe")
        await conversation.handle(Channel.TELEGRAM, USER, "Solo Leveling")

        reply = await conversation.handle(Channel.TELEGRAM, USER, "/stop")
        assert "deleted" in reply.body.lower()
        assert await store.get(Channel.TELEGRAM.value, USER) is None

    async def test_stop_when_nothing_stored(self, make_conversation):
        conversation = make_conversation(json_handler(ask_payload()))
        reply = await conversation.handle(Channel.TELEGRAM, USER, "/stop")
        assert "wasn't storing" in reply.body

    async def test_channels_do_not_share_a_user_id(self, make_conversation, store):
        """`42` on Telegram and `42` on WhatsApp are different people."""
        conversation = make_conversation(json_handler(ask_payload()))
        await conversation.handle(Channel.TELEGRAM, USER, "/subscribe")
        await conversation.handle(Channel.TELEGRAM, USER, "Solo Leveling")

        assert await store.get(Channel.WHATSAPP.value, USER) is None
        reply = await conversation.handle(Channel.WHATSAPP, USER, "/status")
        assert "Nothing on file" in reply.body


class TestCommunities:
    async def test_returns_real_subreddits(self, make_conversation):
        communities = FakeCommunities(
            [community("manga", 3_000_000), community("manhwa", 400_000)]
        )
        conversation = make_conversation(
            json_handler(ask_payload()), communities=communities
        )
        reply = await conversation.handle(
            Channel.TELEGRAM, USER, "/communities Solo Leveling"
        )

        assert communities.queries == ["Solo Leveling"]
        assert [c.title.split(" ·")[0] for c in reply.citations] == ["r/manga", "r/manhwa"]
        assert reply.citations[0].url == "https://www.reddit.com/r/manga/"

    async def test_member_counts_are_shown_compactly(self, make_conversation):
        conversation = make_conversation(
            json_handler(ask_payload()),
            communities=FakeCommunities([community("manga", 3_000_000)]),
        )
        reply = await conversation.handle(Channel.TELEGRAM, USER, "/communities manga")
        # A round number loses its trailing ".0" — "3M members", not "3.0M".
        assert "3M members" in reply.citations[0].title

    async def test_never_reaches_the_catalogue(self, store, make_rag, open_limiter):
        """The catalogue knows nothing about subreddits, so it is not asked."""
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            return httpx.Response(200, json=ask_payload())

        from bot.conversation import Conversation
        from bot.escalation import Escalator

        conversation = Conversation(
            make_rag(handler),
            store,
            Escalator(store),
            open_limiter,
            FakeCommunities([community("manga")]),
        )
        await conversation.handle(Channel.TELEGRAM, USER, "/communities manga")
        assert calls == []

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("which subreddit for Solo Leveling?", "Solo Leveling"),
            ("where can I discuss Omniscient Reader", "Omniscient Reader"),
            ("any communities about Berserk", "Berserk"),
            ("recommend subreddits for Vinland Saga", "Vinland Saga"),
        ],
    )
    async def test_natural_phrasing_is_routed(
        self, make_conversation, text, expected
    ):
        communities = FakeCommunities([community("manga")])
        conversation = make_conversation(
            json_handler(ask_payload()), communities=communities
        )
        await conversation.handle(Channel.TELEGRAM, USER, text)
        assert communities.queries == [expected]

    async def test_falls_back_to_stored_titles(self, make_conversation):
        communities = FakeCommunities([community("manga")])
        conversation = make_conversation(
            json_handler(ask_payload()), communities=communities
        )
        await conversation.handle(Channel.TELEGRAM, USER, "/subscribe")
        await conversation.handle(
            Channel.TELEGRAM, USER, "Solo Leveling, Omniscient Reader"
        )
        await conversation.handle(Channel.TELEGRAM, USER, "skip")

        await conversation.handle(Channel.TELEGRAM, USER, "/communities")
        assert communities.queries == ["Solo Leveling"]

    async def test_asks_when_nothing_to_go_on(self, make_conversation):
        conversation = make_conversation(
            json_handler(ask_payload()), communities=FakeCommunities()
        )
        reply = await conversation.handle(Channel.TELEGRAM, USER, "/communities")
        assert "which series" in reply.body.lower()
        assert not reply.citations

    async def test_unconfigured_explains_itself(self, make_conversation):
        conversation = make_conversation(
            json_handler(ask_payload()),
            communities=FakeCommunities(configured=False),
        )
        reply = await conversation.handle(Channel.TELEGRAM, USER, "/communities manga")
        assert "isn't switched on" in reply.body

    async def test_missing_entirely_explains_itself(self, make_conversation):
        conversation = make_conversation(json_handler(ask_payload()))
        reply = await conversation.handle(Channel.TELEGRAM, USER, "/communities manga")
        assert "isn't switched on" in reply.body

    async def test_no_matches_says_so(self, make_conversation):
        conversation = make_conversation(
            json_handler(ask_payload()), communities=FakeCommunities([])
        )
        reply = await conversation.handle(Channel.TELEGRAM, USER, "/communities zzzz")
        assert "Nothing on Reddit matched" in reply.body

    async def test_outage_does_not_masquerade_as_no_results(self, make_conversation):
        """An outage reported as 'no communities exist' would be a lie."""
        conversation = make_conversation(
            json_handler(ask_payload()),
            communities=FakeCommunities(error=CommunityLookupError("down")),
        )
        reply = await conversation.handle(Channel.TELEGRAM, USER, "/communities manga")

        assert "couldn't reach Reddit" in reply.body
        assert "rather not guess" in reply.body
        assert not reply.citations


class TestRateLimiting:
    async def test_second_message_is_held_back(self, make_conversation):
        conversation = make_conversation(
            json_handler(ask_payload()),
            limiter=RateLimiter(min_seconds=5.0, per_hour=100, clock=lambda: 100.0),
        )
        first = await conversation.handle(Channel.TELEGRAM, USER, "one")
        second = await conversation.handle(Channel.TELEGRAM, USER, "two")

        assert first.citations
        assert not second.citations
        assert "one at a time" in second.body.lower()

    async def test_commands_are_never_limited(self, make_conversation):
        """A limited user must still be able to reach /help and /stop."""
        conversation = make_conversation(
            json_handler(ask_payload()),
            limiter=RateLimiter(min_seconds=999.0, per_hour=1, clock=lambda: 100.0),
        )
        await conversation.handle(Channel.TELEGRAM, USER, "one")
        reply = await conversation.handle(Channel.TELEGRAM, USER, "/help")
        assert "/stop" in reply.body
