"""One place that wires the object graph together.

Both entrypoints — the Telegram poller and the FastAPI app serving the WhatsApp
webhook — need the same core. Construction is synchronous and `start()` is
async, so a caller can build the graph up front and open connections inside
whatever lifecycle hook it has (FastAPI's `lifespan`, PTB's `post_init`).
"""

from __future__ import annotations

from bot.config import Settings, get_settings
from bot.conversation import Conversation
from bot.escalation import Escalator
from bot.rag import RagClient
from bot.ratelimit import RateLimiter
from bot.store import Store


class Services:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.rag = RagClient(
            self.settings.rag_url,
            timeout=self.settings.rag_timeout_seconds,
            top_k=self.settings.rag_top_k,
        )
        self.store = Store(self.settings.database_url)
        self.escalator = Escalator(self.store)
        self.limiter = RateLimiter(
            min_seconds=self.settings.rate_limit_min_seconds,
            per_hour=self.settings.rate_limit_per_hour,
        )
        self.conversation = Conversation(
            self.rag, self.store, self.escalator, self.limiter
        )

    async def start(self) -> None:
        await self.store.init()

    async def close(self) -> None:
        await self.rag.aclose()
        await self.store.close()
