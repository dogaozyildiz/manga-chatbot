"""HTTP entrypoint: `uvicorn bot.api:app`.

This process exists for WhatsApp, which — unlike Telegram — cannot work without
a publicly reachable URL. Telegram runs in its own process by long polling, so
neither channel can take the other down.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from bot.channels.whatsapp import WhatsAppSender, build_router
from bot.config import Settings
from bot.services import Services

log = logging.getLogger(__name__)


def _telegram_notifier(settings: Settings):
    """Push escalations to the operator's Telegram, even from this process.

    A WhatsApp conversation still needs a human somewhere, and the operator
    already has a Telegram chat with the bot. Posting straight to the Bot API
    avoids standing up a second `Application` just to send one message.
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


def create_app(services: Services | None = None) -> FastAPI:
    services = services or Services()
    sender = WhatsAppSender(services.settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await services.start()
        notifier = _telegram_notifier(services.settings)
        if notifier:
            services.escalator.set_notifier(notifier)
        if not services.settings.whatsapp_configured:
            log.warning(
                "WhatsApp is not configured — the webhook will reject "
                "verification. Set WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID "
                "and WHATSAPP_VERIFY_TOKEN; see .env.example."
            )
        yield
        await sender.aclose()
        await services.close()

    app = FastAPI(
        title="Manga chatbot",
        description="WhatsApp Cloud API webhook over the manhwa-rag catalogue.",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(build_router(services, sender))

    @app.get("/health")
    async def health() -> dict:
        # Reports the backend separately from the bot: during the backend's
        # 60-90s warm-up the bot is genuinely up and the backend genuinely
        # isn't, and one flag cannot say that.
        counts = await services.store.counts()
        return {
            "status": "ok",
            "rag_url": services.settings.rag_url,
            "rag_ready": await services.rag.healthy(),
            "whatsapp_configured": services.settings.whatsapp_configured,
            **counts,
        }

    return app


app = create_app()
