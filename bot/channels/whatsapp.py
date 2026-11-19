"""WhatsApp Cloud API adapter.

Three details drive the shape of this module, and all three are things the
Cloud API does that Telegram does not:

1. **Meta retries a webhook it considers slow**, and a RAG answer takes 2-4
   seconds (much longer on a cold backend). So the POST handler returns 200
   immediately and does the work in a background task. Answering synchronously
   would earn duplicate deliveries under load.
2. **Redelivery happens anyway** — network hiccups, Meta's own retries. Every
   message carries a stable `id`, so ids are remembered and repeats dropped.
   Without this the user gets the same answer two or three times.
3. **The webhook is also a verification endpoint.** Meta GETs it with
   `hub.challenge` and expects the challenge echoed back as plain text when
   `hub.verify_token` matches the string you configured.

Delivery-status callbacks arrive on the same webhook as `statuses` rather than
`messages`, and are ignored.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections import deque

import httpx
from fastapi import APIRouter, BackgroundTasks, Query, Request, Response
from fastapi.responses import PlainTextResponse

from bot.config import Settings
from bot.format import render
from bot.schema import Channel
from bot.services import Services

log = logging.getLogger(__name__)


class SeenMessages:
    """A bounded memory of handled message ids.

    Bounded because this runs for days and an unbounded set is a slow leak.
    512 is far more than any plausible retry window.
    """

    def __init__(self, maxlen: int = 512) -> None:
        self._order: deque[str] = deque(maxlen=maxlen)
        self._ids: set[str] = set()

    def add_if_new(self, message_id: str) -> bool:
        if message_id in self._ids:
            return False
        if len(self._order) == self._order.maxlen and self._order:
            self._ids.discard(self._order[0])
        self._order.append(message_id)
        self._ids.add(message_id)
        return True


class WhatsAppSender:
    def __init__(
        self, settings: Settings, client: httpx.AsyncClient | None = None
    ) -> None:
        self._settings = settings
        self._client = client or httpx.AsyncClient(timeout=15.0)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def send_text(self, to: str, body: str) -> None:
        url = (
            f"https://graph.facebook.com/{self._settings.whatsapp_api_version}"
            f"/{self._settings.whatsapp_phone_number_id}/messages"
        )
        response = await self._client.post(
            url,
            headers={"Authorization": f"Bearer {self._settings.whatsapp_token}"},
            json={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "text",
                # Source links are listed in the body; a preview card would
                # push the answer itself off the screen.
                "text": {"preview_url": False, "body": body},
            },
        )
        if response.status_code >= 400:
            log.error("whatsapp send failed %s: %s", response.status_code, response.text)


def extract_messages(payload: dict) -> list[dict]:
    """Pull text messages out of a Cloud API webhook payload.

    Defensive throughout: this parses attacker-reachable JSON, and a malformed
    body must produce an empty list rather than an exception that Meta then
    retries forever.
    """
    out: list[dict] = []
    for entry in _as_list(payload.get("entry")):
        for change in _as_list(_get(entry, "changes")):
            value = _get(change, "value") or {}
            if not isinstance(value, dict):
                continue
            for message in _as_list(value.get("messages")):
                if not isinstance(message, dict):
                    continue
                if message.get("type") != "text":
                    continue
                text = (message.get("text") or {}).get("body")
                sender = message.get("from")
                message_id = message.get("id")
                if not (text and sender and message_id):
                    continue
                out.append({"id": str(message_id), "from": str(sender), "text": str(text)})
    return out


def _as_list(value) -> list:
    return value if isinstance(value, list) else []


def _get(container, key):
    return container.get(key) if isinstance(container, dict) else None


def valid_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    """Check Meta's `X-Hub-Signature-256: sha256=<hex>` over the raw body.

    The verify token only protects the GET handshake. Without this check the
    POST endpoint accepts anything, so a forged request can act as any phone
    number -- including `/stop`, which deletes that user's data. Constant-time
    comparison; computed over the exact bytes received, before parsing.
    """
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def build_router(
    services: Services, sender: WhatsAppSender, seen: SeenMessages | None = None
) -> APIRouter:
    router = APIRouter()
    settings = services.settings
    seen = seen or SeenMessages()

    @router.get("/webhook/whatsapp")
    async def verify(
        mode: str | None = Query(None, alias="hub.mode"),
        token: str | None = Query(None, alias="hub.verify_token"),
        challenge: str | None = Query(None, alias="hub.challenge"),
    ):
        if mode == "subscribe" and token and hmac.compare_digest(
            token, settings.whatsapp_verify_token
        ):
            # Meta requires the challenge back as bare text, not JSON.
            return PlainTextResponse(challenge or "")
        log.warning("webhook verification rejected (mode=%s)", mode)
        return PlainTextResponse("verification failed", status_code=403)

    @router.post("/webhook/whatsapp")
    async def receive(request: Request, background: BackgroundTasks):
        raw = await request.body()
        if not valid_signature(
            settings.whatsapp_app_secret, raw, request.headers.get("x-hub-signature-256")
        ):
            # Not from Meta, so no retry semantics to respect.
            log.warning("whatsapp webhook: missing or invalid signature, rejected")
            return Response(status_code=401)
        try:
            payload = json.loads(raw)
        except Exception:
            # Still 200: a body we cannot parse will not parse on retry either.
            return Response(status_code=200)

        for message in extract_messages(payload):
            if not seen.add_if_new(message["id"]):
                log.info("duplicate delivery %s ignored", message["id"])
                continue
            background.add_task(
                _process, services, sender, message["from"], message["text"]
            )

        # Always 200, always immediately. Anything else invites a retry.
        return Response(status_code=200)

    return router


async def _process(
    services: Services, sender: WhatsAppSender, user_id: str, text: str
) -> None:
    try:
        reply = await services.conversation.handle(Channel.WHATSAPP, user_id, text)
        body = render(reply, Channel.WHATSAPP)
    except Exception:
        log.exception("failed handling whatsapp message")
        body = "Something broke on my side. It's logged — try again in a moment."
    await sender.send_text(user_id, body)
