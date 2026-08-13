"""Telegram entrypoint: `python -m bot.telegram_main`.

Runs on its own, with WhatsApp entirely unconfigured. That is the point — the
free, instant channel should never be blocked on the one that needs a Meta app.
"""

from __future__ import annotations

import logging

from bot.channels.telegram import build_application
from bot.config import get_settings
from bot.services import Services


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # httpx logs every Telegram poll at INFO, which buries everything else.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    services = Services(get_settings())
    application = build_application(services)
    logging.getLogger(__name__).info(
        "polling; backend=%s", services.settings.rag_url
    )
    application.run_polling()


if __name__ == "__main__":
    main()
