"""Discord entrypoint: `python -m bot.discord_main`.

Runs independently of the Telegram poller and the WhatsApp webhook, so no
channel can take another down.
"""

from __future__ import annotations

import logging

from bot.channels.discord import build_client
from bot.config import get_settings
from bot.services import Services


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # discord.py logs its own gateway chatter at INFO; keep the useful lines.
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)

    services = Services(get_settings())
    client = build_client(services)
    logging.getLogger(__name__).info(
        "starting discord client; backend=%s", services.settings.rag_url
    )
    client.run(services.settings.discord_bot_token, log_handler=None)


if __name__ == "__main__":
    main()
