"""Discord adapter, with no gateway connection.

The rule worth a test of its own: Discord discards an interaction that is not
acknowledged within 3 seconds, and a catalogue answer takes 2-4. So the deferral
must happen *before* the slow call, not after it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import discord
import pytest
from discord import app_commands

from bot.channels.discord import _respond, register_commands
from bot.schema import Channel, Citation, Reply


class FakeResponse:
    def __init__(self) -> None:
        self.deferred = False
        self.messages: list[tuple[str, bool]] = []

    async def defer(self, thinking: bool = False, ephemeral: bool = False) -> None:
        self.deferred = True

    def is_done(self) -> bool:
        return self.deferred

    async def send_message(self, content: str, ephemeral: bool = False) -> None:
        self.messages.append((content, ephemeral))


class FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[tuple[str, bool]] = []

    async def send(self, content: str, ephemeral: bool = False) -> None:
        self.sent.append((content, ephemeral))


@dataclass
class FakeUser:
    id: int = 4242
    display_name: str = "Tester"


@dataclass
class FakeInteraction:
    response: FakeResponse = field(default_factory=FakeResponse)
    followup: FakeFollowup = field(default_factory=FakeFollowup)
    user: FakeUser = field(default_factory=FakeUser)


class RecordingConversation:
    """Captures what it was asked, and whether the interaction was deferred."""

    def __init__(self, reply: Reply, interaction: FakeInteraction | None = None) -> None:
        self._reply = reply
        self._interaction = interaction
        self.calls: list[tuple[Channel, str, str]] = []
        self.deferred_when_called: list[bool] = []

    async def handle(self, channel, user_id, text, *, display_name="") -> Reply:
        self.calls.append((channel, user_id, text))
        if self._interaction is not None:
            self.deferred_when_called.append(self._interaction.response.deferred)
        return self._reply


@dataclass
class FakeServices:
    conversation: RecordingConversation


ANSWER = Reply(
    body="Two completed romance manhwa.",
    filters=["Korean", "<100 chapters"],
    citations=[
        Citation(
            ref="series:1",
            title="Rosen Blood",
            url="https://anilist.co/manga/1",
            quote="84 chapters",
        )
    ],
)


class TestRespond:
    async def test_defers_before_the_slow_call(self):
        """The whole reason this adapter defers: a 2-4s answer would otherwise
        blow Discord's 3-second acknowledgement window."""
        interaction = FakeInteraction()
        conversation = RecordingConversation(ANSWER, interaction)

        await _respond(interaction, FakeServices(conversation), "a question")

        assert conversation.deferred_when_called == [True]

    async def test_sends_through_followup(self):
        interaction = FakeInteraction()
        await _respond(
            interaction, FakeServices(RecordingConversation(ANSWER)), "a question"
        )

        assert len(interaction.followup.sent) == 1
        body, _ = interaction.followup.sent[0]
        assert "Two completed romance manhwa." in body

    async def test_renders_with_discord_markup(self):
        interaction = FakeInteraction()
        await _respond(interaction, FakeServices(RecordingConversation(ANSWER)), "q")

        body, _ = interaction.followup.sent[0]
        assert "[Rosen Blood](<https://anilist.co/manga/1>)" in body
        assert "<b>" not in body and "<i>" not in body

    async def test_passes_the_discord_channel_and_user_id(self):
        interaction = FakeInteraction()
        conversation = RecordingConversation(ANSWER)
        await _respond(interaction, FakeServices(conversation), "a question")

        channel, user_id, text = conversation.calls[0]
        assert channel is Channel.DISCORD
        assert user_id == "4242"
        assert text == "a question"

    async def test_respects_the_2000_character_ceiling(self):
        interaction = FakeInteraction()
        long_reply = Reply(body="word " * 3000, citations=ANSWER.citations)
        await _respond(interaction, FakeServices(RecordingConversation(long_reply)), "q")

        body, _ = interaction.followup.sent[0]
        assert len(body) <= 2000

    async def test_ephemeral_is_propagated(self):
        interaction = FakeInteraction()
        await _respond(
            interaction,
            FakeServices(RecordingConversation(ANSWER)),
            "/status",
            ephemeral=True,
        )
        assert interaction.followup.sent[0][1] is True


class TestCommandRegistration:
    @pytest.fixture
    def tree(self):
        client = discord.Client(intents=discord.Intents.none())
        tree = app_commands.CommandTree(client)
        register_commands(tree, FakeServices(RecordingConversation(ANSWER)))
        return tree

    def test_expected_commands_exist(self, tree):
        names = {command.name for command in tree.get_commands()}
        assert names == {"ask", "communities", "subscribe", "status", "stop", "help"}

    def test_commands_carry_descriptions(self, tree):
        """An undescribed command shows as blank in Discord's picker."""
        assert all(command.description for command in tree.get_commands())


class TestSubscribeCommand:
    async def test_drives_the_shared_capture_flow(self):
        """Discord collects both answers at once, then feeds the existing
        state machine its steps in order rather than duplicating it."""
        client = discord.Client(intents=discord.Intents.none())
        tree = app_commands.CommandTree(client)
        conversation = RecordingConversation(Reply(body="You're set."))
        register_commands(tree, FakeServices(conversation))

        interaction = FakeInteraction()
        subscribe = tree.get_command("subscribe")
        await subscribe.callback(
            interaction, series="Solo Leveling", contact="me@example.com"
        )

        assert [text for _, _, text in conversation.calls] == [
            "/subscribe",
            "Solo Leveling",
            "me@example.com",
        ]

    async def test_missing_contact_becomes_skip(self):
        client = discord.Client(intents=discord.Intents.none())
        tree = app_commands.CommandTree(client)
        conversation = RecordingConversation(Reply(body="You're set."))
        register_commands(tree, FakeServices(conversation))

        await tree.get_command("subscribe").callback(
            FakeInteraction(), series="Berserk", contact=None
        )
        assert conversation.calls[-1][2] == "skip"

    async def test_reply_is_ephemeral(self):
        """A contact address must not be posted into a public channel."""
        client = discord.Client(intents=discord.Intents.none())
        tree = app_commands.CommandTree(client)
        register_commands(
            tree, FakeServices(RecordingConversation(Reply(body="You're set.")))
        )

        interaction = FakeInteraction()
        await tree.get_command("subscribe").callback(
            interaction, series="Berserk", contact="me@example.com"
        )
        assert interaction.followup.sent[0][1] is True


class TestCommunitiesCommand:
    async def test_passes_the_series_through(self):
        client = discord.Client(intents=discord.Intents.none())
        tree = app_commands.CommandTree(client)
        conversation = RecordingConversation(ANSWER)
        register_commands(tree, FakeServices(conversation))

        await tree.get_command("communities").callback(
            FakeInteraction(), series="Solo Leveling"
        )
        assert conversation.calls[0][2] == "/communities Solo Leveling"

    async def test_without_a_series_sends_the_bare_command(self):
        client = discord.Client(intents=discord.Intents.none())
        tree = app_commands.CommandTree(client)
        conversation = RecordingConversation(ANSWER)
        register_commands(tree, FakeServices(conversation))

        await tree.get_command("communities").callback(FakeInteraction(), series=None)
        assert conversation.calls[0][2] == "/communities"
