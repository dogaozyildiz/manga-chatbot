"""Discord adapter.

Slash commands only, and that is a deliberate constraint rather than a
limitation. Reading ordinary message text requires the Message Content
privileged intent, which has to be toggled on in the developer portal and
re-applied for annually once an app passes 10,000 users. An interaction payload
carries its own arguments, so a slash-command bot needs no privileged intents at
all — it runs on `Intents.none()`.

The other Discord-specific rule this module exists to obey: **an interaction
must be acknowledged within 3 seconds**, and a catalogue answer takes 2-4. So
every command defers first and sends through `followup`. Without the defer,
Discord shows "the application did not respond" and throws the answer away.

The multi-step preference flow is driven by passing its answers through the same
state machine the other channels use, rather than reimplementing it here — see
`_subscribe`.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from bot.escalation import telegram_notifier
from bot.format import render
from bot.schema import Channel
from bot.services import Services

log = logging.getLogger(__name__)


class MangaBotClient(discord.Client):
    def __init__(self, services: Services) -> None:
        # Nothing privileged: no message content, no members, no presence.
        super().__init__(intents=discord.Intents.none())
        self.services = services
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        await self.services.start()

        notifier = telegram_notifier(self.services.settings)
        if notifier:
            self.services.escalator.set_notifier(notifier)
            log.info("escalations will be pushed to the operator's Telegram")
        else:
            log.info("no operator configured — escalations are recorded only")

        if not self.services.settings.communities_configured:
            log.warning(
                "Reddit credentials unset — /communities will explain itself "
                "rather than answer"
            )

        register_commands(self.tree, self.services)

        guild_id = self.services.settings.discord_guild_id
        if guild_id:
            # Guild commands appear immediately; global ones can take an hour
            # to propagate, which makes iterating unbearable.
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("commands synced to guild %s", guild_id)
        else:
            await self.tree.sync()
            log.info("commands synced globally (may take up to an hour)")

    async def on_ready(self) -> None:
        log.info("connected as %s", self.user)
        if not await self.services.rag.healthy():
            log.warning(
                "RAG backend at %s is not answering /health yet",
                self.services.settings.rag_url,
            )

    async def close(self) -> None:
        await self.services.close()
        await super().close()


def build_client(services: Services) -> MangaBotClient:
    if not services.settings.discord_bot_token:
        raise RuntimeError(
            "DISCORD_BOT_TOKEN is not set. Create an application at "
            "discord.com/developers, add a bot, and copy its token into .env — "
            "see .env.example."
        )
    return MangaBotClient(services)


async def _respond(
    interaction: discord.Interaction,
    services: Services,
    text: str,
    *,
    ephemeral: bool = False,
) -> None:
    """Defer, run the conversation, send the rendered reply."""
    await interaction.response.defer(thinking=True, ephemeral=ephemeral)
    user = interaction.user
    log.info("<- discord %s: %s", user.id, text[:80])

    reply = await services.conversation.handle(
        Channel.DISCORD,
        str(user.id),
        text,
        display_name=getattr(user, "display_name", "") or "",
    )
    body = render(reply, Channel.DISCORD)
    log.info(
        "-> discord %s: %d chars, %d sources%s",
        user.id,
        len(reply.body),
        len(reply.citations),
        ", escalated" if reply.escalated else "",
    )
    await interaction.followup.send(body, ephemeral=ephemeral)


def register_commands(tree: app_commands.CommandTree, services: Services) -> None:
    @tree.command(name="ask", description="Ask about a manga or manhwa series")
    @app_commands.describe(question="e.g. completed romance manhwa under 100 chapters")
    async def ask(interaction: discord.Interaction, question: str) -> None:
        await _respond(interaction, services, question)

    @tree.command(
        name="communities",
        description="Find real subreddits where a series is discussed",
    )
    @app_commands.describe(series="Leave empty to use the series you follow")
    async def communities(
        interaction: discord.Interaction, series: str | None = None
    ) -> None:
        await _respond(interaction, services, f"/communities {series or ''}".strip())

    @tree.command(name="subscribe", description="Tell the bot what you read")
    @app_commands.describe(
        series="Comma-separated titles",
        contact="Optional email for alerts",
    )
    async def subscribe(
        interaction: discord.Interaction, series: str, contact: str | None = None
    ) -> None:
        # Discord collects both answers up front, so the multi-step flow is fed
        # its steps in order rather than duplicated. Same state machine, same
        # storage, same tests.
        await interaction.response.defer(thinking=True, ephemeral=True)
        user_id = str(interaction.user.id)
        handle = services.conversation.handle
        await handle(Channel.DISCORD, user_id, "/subscribe")
        await handle(Channel.DISCORD, user_id, series)
        reply = await handle(Channel.DISCORD, user_id, contact or "skip")
        # Ephemeral: a contact address should not be posted into a public channel.
        await interaction.followup.send(render(reply, Channel.DISCORD), ephemeral=True)

    @tree.command(name="status", description="What the bot has stored about you")
    async def status(interaction: discord.Interaction) -> None:
        await _respond(interaction, services, "/status", ephemeral=True)

    @tree.command(name="stop", description="Delete everything stored about you")
    async def stop(interaction: discord.Interaction) -> None:
        await _respond(interaction, services, "/stop", ephemeral=True)

    @tree.command(name="help", description="What this bot can do")
    async def help_(interaction: discord.Interaction) -> None:
        await _respond(interaction, services, "/help", ephemeral=True)

    tree.on_error = _on_error


async def _on_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    log.exception("command failed", exc_info=error)
    message = "Something broke on my side. It's logged — try again in a moment."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except Exception:
        pass
