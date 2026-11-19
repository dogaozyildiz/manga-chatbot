"""Fixtures.

Nothing here touches the network or needs a key: the backend is replaced by an
`httpx.MockTransport` and the store is in-memory SQLite. That is deliberate —
CI has no manhwa-rag to talk to, and a test suite that silently depends on a
running backend is a test suite that only passes on one laptop.
"""

from __future__ import annotations

import httpx
import pytest

from bot.config import Settings
from bot.conversation import Conversation
from bot.escalation import Escalator
from bot.rag import RagClient
from bot.ratelimit import RateLimiter
from bot.schema import Community
from bot.store import Store

RAG_BASE = "http://rag.test"


def ask_payload(**overrides) -> dict:
    """A realistic `POST /ask` body, shaped like manhwa-rag's `AskResponse`."""
    payload = {
        "question": "completed romance manhwa under 100 chapters",
        "answer": "Two completed romance manhwa come in under 100 chapters.",
        "refused": False,
        "route": "catalogue",
        "applied_filters": ["Korean", "finished", "<100 chapters", "Romance"],
        "claims": [
            {
                "text": "Rosen Blood runs 84 chapters.",
                "quote": "84 chapters",
                "ref": "series:900001",
                "verified": True,
            }
        ],
        "rejected_claims": 1,
        "sources": [
            {
                "ref": "series:900001",
                "kind": "series",
                "title": "Rosen Blood",
                "url": "https://anilist.co/manga/900001",
                "score": 0.0163,
                "origin": "fused",
                "meta": {"chapters": 84},
            },
            {
                "ref": "series:900002",
                "kind": "series",
                "title": "Cheese in the Trap",
                "url": None,
                "score": 0.0161,
                "origin": "vector",
                "meta": {"chapters": 96},
            },
        ],
        "latency_ms": 2400,
        "model": "groq",
    }
    payload.update(overrides)
    return payload


def retrieve_payload(**overrides) -> dict:
    payload = {
        "question": "completed romance manhwa under 100 chapters",
        "route": "catalogue",
        "applied_filters": ["Korean", "finished", "<100 chapters"],
        "sources": ask_payload()["sources"],
    }
    payload.update(overrides)
    return payload


def json_handler(payload: dict, status_code: int = 200):
    """A MockTransport handler that always answers with one JSON body."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload)

    return handler


def route_handler(**by_path):
    """Dispatch on URL path: `route_handler(ask=..., retrieve=...)`.

    Values are either an `httpx.Response` factory or a plain dict (200 JSON).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.url.path.lstrip("/")
        entry = by_path.get(key)
        if entry is None:
            return httpx.Response(404, json={"detail": "not found"})
        if callable(entry):
            return entry(request)
        return httpx.Response(200, json=entry)

    return handler


@pytest.fixture
async def store():
    store = Store("sqlite+aiosqlite:///:memory:")
    await store.init()
    yield store
    await store.close()


@pytest.fixture
async def make_rag():
    clients: list[httpx.AsyncClient] = []

    def factory(handler) -> RagClient:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=RAG_BASE
        )
        clients.append(client)
        return RagClient(RAG_BASE, client=client)

    yield factory
    for client in clients:
        await client.aclose()


@pytest.fixture
def open_limiter() -> RateLimiter:
    """A limiter that never refuses — rate limiting has its own tests."""
    return RateLimiter(min_seconds=0.0, per_hour=10_000)


class FakeCommunities:
    """Stands in for `RedditCommunities` without any HTTP."""

    def __init__(self, results=None, *, error=None, configured=True) -> None:
        self._results = results or []
        self._error = error
        self._configured = configured
        self.queries: list[str] = []

    @property
    def configured(self) -> bool:
        return self._configured

    async def search(self, query: str, limit: int = 4):
        self.queries.append(query)
        if self._error is not None:
            raise self._error
        return self._results[:limit]


def community(name: str, subscribers: int = 1000, description: str = "") -> Community:
    return Community(
        name=name,
        subscribers=subscribers,
        description=description or f"About {name}.",
        url=f"https://www.reddit.com/r/{name}/",
    )


@pytest.fixture
async def make_conversation(store, make_rag, open_limiter):
    def factory(
        handler, *, limiter=None, notifier=None, communities=None
    ) -> Conversation:
        rag = make_rag(handler)
        escalator = Escalator(store, notifier)
        return Conversation(
            rag, store, escalator, limiter or open_limiter, communities
        )

    return factory


@pytest.fixture
def settings() -> Settings:
    return Settings(
        rag_url=RAG_BASE,
        whatsapp_token="test-token",
        whatsapp_phone_number_id="1234567890",
        whatsapp_verify_token="verify-me",
        whatsapp_app_secret="test-app-secret",
        telegram_bot_token="",
        admin_chat_id="",
        database_url="sqlite+aiosqlite:///:memory:",
    )
