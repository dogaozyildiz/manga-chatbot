"""The Cloud API webhook: verification handshake, parsing, and redelivery.

These are the parts with no Telegram equivalent, so they get their own tests.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
import pytest
from fastapi import FastAPI

from bot.channels.whatsapp import SeenMessages, build_router, extract_messages
from bot.conversation import Conversation

from tests.conftest import ask_payload, json_handler

SENDER = "905551112233"


def wa_payload(text: str = "hello", message_id: str = "wamid.1") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "0",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": "1234567890"},
                            "contacts": [{"wa_id": SENDER}],
                            "messages": [
                                {
                                    "from": SENDER,
                                    "id": message_id,
                                    "timestamp": "1700000000",
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, to: str, body: str) -> None:
        self.sent.append((to, body))


@dataclass
class FakeServices:
    conversation: Conversation
    settings: object


@pytest.fixture
def sender() -> FakeSender:
    return FakeSender()


@pytest.fixture
def webhook(make_conversation, settings, sender):
    """An app serving only the WhatsApp router."""
    conversation = make_conversation(json_handler(ask_payload()))
    services = FakeServices(conversation=conversation, settings=settings)
    app = FastAPI()
    app.include_router(build_router(services, sender, SeenMessages()))
    return app


def client_for(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


class TestVerification:
    async def test_echoes_the_challenge(self, webhook):
        async with client_for(webhook) as client:
            response = await client.get(
                "/webhook/whatsapp",
                params={
                    "hub.mode": "subscribe",
                    "hub.verify_token": "verify-me",
                    "hub.challenge": "1158201444",
                },
            )
        assert response.status_code == 200
        # Bare text, not JSON — Meta rejects anything else.
        assert response.text == "1158201444"

    async def test_rejects_a_wrong_token(self, webhook):
        async with client_for(webhook) as client:
            response = await client.get(
                "/webhook/whatsapp",
                params={
                    "hub.mode": "subscribe",
                    "hub.verify_token": "not-the-token",
                    "hub.challenge": "1158201444",
                },
            )
        assert response.status_code == 403

    async def test_rejects_a_missing_token(self, webhook):
        async with client_for(webhook) as client:
            response = await client.get(
                "/webhook/whatsapp", params={"hub.mode": "subscribe"}
            )
        assert response.status_code == 403


class TestExtractMessages:
    def test_parses_a_text_message(self):
        messages = extract_messages(wa_payload("hi there", "wamid.9"))
        assert messages == [{"id": "wamid.9", "from": SENDER, "text": "hi there"}]

    def test_ignores_delivery_statuses(self):
        """Status callbacks arrive on the same webhook and are not questions."""
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "statuses": [
                                    {"id": "wamid.1", "status": "delivered"}
                                ]
                            }
                        }
                    ]
                }
            ]
        }
        assert extract_messages(payload) == []

    def test_ignores_non_text_messages(self):
        payload = wa_payload()
        payload["entry"][0]["changes"][0]["value"]["messages"][0]["type"] = "image"
        assert extract_messages(payload) == []

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"entry": None},
            {"entry": [{}]},
            {"entry": [{"changes": "nope"}]},
            {"entry": [{"changes": [{"value": None}]}]},
            {"entry": [{"changes": [{"value": {"messages": [None]}}]}]},
            {"entry": [{"changes": [{"value": {"messages": [{"type": "text"}]}}]}]},
        ],
    )
    def test_malformed_payloads_yield_nothing(self, payload):
        """This parses attacker-reachable JSON; it must not raise."""
        assert extract_messages(payload) == []

    def test_several_messages_in_one_delivery(self):
        payload = wa_payload()
        payload["entry"][0]["changes"][0]["value"]["messages"].append(
            {
                "from": SENDER,
                "id": "wamid.2",
                "type": "text",
                "text": {"body": "second"},
            }
        )
        assert len(extract_messages(payload)) == 2


class TestReceiving:
    async def test_answers_the_user(self, webhook, sender):
        async with client_for(webhook) as client:
            response = await client.post("/webhook/whatsapp", json=wa_payload("hello"))

        assert response.status_code == 200
        assert len(sender.sent) == 1
        to, body = sender.sent[0]
        assert to == SENDER
        assert "Rosen Blood" in body
        # WhatsApp markup, not Telegram's.
        assert "<a href" not in body

    async def test_redelivery_is_ignored(self, webhook, sender):
        """Meta retries. The user must not get the same answer twice."""
        payload = wa_payload("hello", "wamid.same")
        async with client_for(webhook) as client:
            await client.post("/webhook/whatsapp", json=payload)
            await client.post("/webhook/whatsapp", json=payload)

        assert len(sender.sent) == 1

    async def test_a_different_message_is_not_deduplicated(self, webhook, sender):
        async with client_for(webhook) as client:
            await client.post("/webhook/whatsapp", json=wa_payload("a", "wamid.a"))
            await client.post("/webhook/whatsapp", json=wa_payload("b", "wamid.b"))

        assert len(sender.sent) == 2

    async def test_unparseable_body_still_returns_200(self, webhook, sender):
        """A 200 stops Meta retrying a body that will never parse."""
        async with client_for(webhook) as client:
            response = await client.post(
                "/webhook/whatsapp",
                content=b"not json",
                headers={"content-type": "application/json"},
            )
        assert response.status_code == 200
        assert sender.sent == []

    async def test_status_callback_produces_no_reply(self, webhook, sender):
        async with client_for(webhook) as client:
            response = await client.post(
                "/webhook/whatsapp",
                json={"entry": [{"changes": [{"value": {"statuses": []}}]}]},
            )
        assert response.status_code == 200
        assert sender.sent == []


class TestSeenMessages:
    def test_first_time_is_new(self):
        seen = SeenMessages()
        assert seen.add_if_new("a") is True

    def test_second_time_is_not(self):
        seen = SeenMessages()
        seen.add_if_new("a")
        assert seen.add_if_new("a") is False

    def test_memory_is_bounded(self):
        """Runs for days; an unbounded set is a slow leak."""
        seen = SeenMessages(maxlen=4)
        for i in range(10):
            assert seen.add_if_new(f"id-{i}") is True
        assert len(seen._ids) <= 4
        # The oldest has been forgotten, so it reads as new again — the
        # accepted trade for a bounded window.
        assert seen.add_if_new("id-0") is True
        assert seen.add_if_new("id-9") is False
