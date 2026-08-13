"""Telegram adapter.

Long polling rather than a webhook, deliberately: it needs no public URL, no
tunnel and no TLS, so the whole Telegram half runs and demos on a laptop with
nothing but a token from @BotFather.

One handler takes both commands and plain text. `Conversation` already parses
`/commands`, and registering separate `CommandHandler`s would split that logic
across two layers for no gain.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from bot.config import Settings
from bot.format import render
from bot.schema import Channel
from bot.services import Services

log = logging.getLogger(__name__)


def build_application(services: Services) -> Application:
    settings: Settings = services.settings
    if not settings.telegram_bot_token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not set. Get one from @BotFather and put it "
            "in .env — see .env.example."
        )

    async def post_init(app: Application) -> None:
        await services.start()
        if settings.admin_chat_id:
            services.escalator.set_notifier(_notifier(app, settings.admin_chat_id))
            log.info("escalations will be pushed to chat %s", settings.admin_chat_id)
        else:
            log.info("ADMIN_CHAT_ID unset — escalations are recorded but not pushed")
        if not await services.rag.healthy():
            # Not fatal. The backend takes 60-90s to warm its embedding model,
            # so "not ready yet" at boot is the normal case, not an error.
            log.warning(
                "RAG backend at %s is not answering /health yet — it needs "
                "60-90s to load the embedding model",
                settings.rag_url,
            )

    async def post_shutdown(app: Application) -> None:
        await services.close()

    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.add_handler(
        MessageHandler(filters.TEXT | filters.COMMAND, _make_handler(services))
    )
    application.add_error_handler(_on_error)
    return application


def _make_handler(services: Services):
    async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if message is None or not message.text:
            return

        chat_id = str(message.chat_id)
        user = update.effective_user
        display_name = user.full_name if user else ""

        # The backend takes 2-4s. Without this the chat looks dead for the
        # whole of it.
        try:
            await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
        except Exception:
            pass

        # Operator-visible traffic. Without it a successful message logs
        # nothing at all, and "handled fine" is indistinguishable from "never
        # arrived" — which is exactly the question you ask when a bot seems
        # silent.
        log.info("<- %s: %s", chat_id, message.text[:80])

        reply = await services.conversation.handle(
            Channel.TELEGRAM, chat_id, message.text, display_name=display_name
        )
        log.info(
            "-> %s: %d chars, %d sources%s",
            chat_id,
            len(reply.body),
            len(reply.citations),
            ", escalated" if reply.escalated else "",
        )
        await message.reply_text(
            render(reply, Channel.TELEGRAM),
            parse_mode=ParseMode.HTML,
            # Every answer carries source links; previews would bury the answer
            # under a stack of link cards.
            disable_web_page_preview=True,
        )

    return handle


def _notifier(app: Application, admin_chat_id: str):
    async def notify(text: str) -> None:
        # No parse_mode: this carries a raw user question, and any stray "<" in
        # it would make Telegram reject the operator's notification.
        await app.bot.send_message(chat_id=admin_chat_id, text=text)

    return notify


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("handler failed", exc_info=context.error)
    message = getattr(update, "effective_message", None)
    if message is None:
        return
    try:
        await message.reply_text(
            "Something broke on my side. It's logged — try again in a moment."
        )
    except Exception:
        pass
