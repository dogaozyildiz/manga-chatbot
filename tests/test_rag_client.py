"""The backend contract, including every way it can fail."""

from __future__ import annotations

import httpx

from tests.conftest import ask_payload, json_handler, retrieve_payload, route_handler


class TestSuccessfulAnswer:
    async def test_parses_answer_and_metadata(self, make_rag):
        rag = make_rag(json_handler(ask_payload()))
        answer = await rag.ask("completed romance manhwa under 100 chapters")

        assert answer.text.startswith("Two completed romance manhwa")
        assert answer.refused is False
        assert answer.route == "catalogue"
        assert answer.applied_filters == [
            "Korean", "finished", "<100 chapters", "Romance",
        ]
        assert answer.rejected_claims == 1
        assert answer.model == "groq"
        assert answer.degraded is False and answer.unavailable is False

    async def test_joins_claims_to_their_sources(self, make_rag):
        """A quote without its source's title and URL is not a citation."""
        rag = make_rag(json_handler(ask_payload()))
        answer = await rag.ask("anything")

        assert len(answer.citations) == 1
        citation = answer.citations[0]
        assert citation.ref == "series:900001"
        assert citation.title == "Rosen Blood"
        assert citation.url == "https://anilist.co/manga/900001"
        assert citation.quote == "84 chapters"
        assert citation.kind == "series"

    async def test_claim_citing_unknown_source_still_renders(self, make_rag):
        """Defensive: the ref is used as the title rather than crashing."""
        payload = ask_payload(
            claims=[{"text": "x", "quote": "y", "ref": "series:404", "verified": True}]
        )
        rag = make_rag(json_handler(payload))
        answer = await rag.ask("anything")

        assert answer.citations[0].title == "series:404"
        assert answer.citations[0].url is None

    async def test_sends_k_when_configured(self, make_rag):
        seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            seen.append(json.loads(request.content))
            return httpx.Response(200, json=ask_payload())

        rag = make_rag(handler)
        rag._top_k = 3
        await rag.ask("anything")
        assert seen[0]["k"] == 3

    async def test_omits_k_by_default(self, make_rag):
        seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            seen.append(json.loads(request.content))
            return httpx.Response(200, json=ask_payload())

        rag = make_rag(handler)
        await rag.ask("anything")
        # Absent, so the backend applies its own top_k rather than being
        # overridden with a number this layer invented.
        assert "k" not in seen[0]


class TestRefusal:
    async def test_refusal_is_carried_through(self, make_rag):
        payload = ask_payload(
            answer="I couldn't find anything that covers that.",
            refused=True,
            claims=[],
        )
        rag = make_rag(json_handler(payload))
        answer = await rag.ask("who won the 2019 Cricket World Cup?")

        assert answer.refused is True
        assert "couldn't find" in answer.text

    async def test_refusal_still_shows_what_matched(self, make_rag):
        """A refusal with matched sources is transparent, not a dead end."""
        payload = ask_payload(refused=True, claims=[])
        rag = make_rag(json_handler(payload))
        answer = await rag.ask("something off-corpus")

        assert [c.title for c in answer.citations] == [
            "Rosen Blood",
            "Cheese in the Trap",
        ]
        assert all(c.quote == "" for c in answer.citations)


class TestDegradedAndUnavailable:
    async def test_503_falls_back_to_retrieve(self, make_rag):
        """No generation quota is not the same as no answer."""
        rag = make_rag(
            route_handler(
                ask=lambda r: httpx.Response(503, json={"detail": "no key"}),
                retrieve=retrieve_payload(),
            )
        )
        answer = await rag.ask("completed romance manhwa under 100 chapters")

        assert answer.degraded is True
        assert answer.unavailable is False
        assert answer.text == ""
        assert [c.title for c in answer.citations] == [
            "Rosen Blood",
            "Cheese in the Trap",
        ]
        assert answer.applied_filters == ["Korean", "finished", "<100 chapters"]

    async def test_503_with_retrieve_also_down_is_unavailable(self, make_rag):
        rag = make_rag(
            route_handler(
                ask=lambda r: httpx.Response(503, json={"detail": "no key"}),
                retrieve=lambda r: httpx.Response(500, json={"detail": "boom"}),
            )
        )
        answer = await rag.ask("anything")
        assert answer.unavailable is True

    async def test_503_with_no_sources_is_a_refusal(self, make_rag):
        rag = make_rag(
            route_handler(
                ask=lambda r: httpx.Response(503, json={"detail": "no key"}),
                retrieve=retrieve_payload(sources=[]),
            )
        )
        answer = await rag.ask("anything")
        assert answer.refused is True

    async def test_connect_error_is_unavailable(self, make_rag):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        rag = make_rag(handler)
        answer = await rag.ask("anything")

        assert answer.unavailable is True
        assert answer.degraded is False

    async def test_timeout_but_backend_healthy_degrades_to_retrieve(self, make_rag):
        """A slow question is not a dead backend.

        `/health` answering proves the service is up, so the useful move is to
        return what retrieval finds rather than claim an outage.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/ask":
                raise httpx.ReadTimeout("too slow", request=request)
            if path == "/health":
                return httpx.Response(200, json={"status": "ok"})
            return httpx.Response(200, json=retrieve_payload())

        rag = make_rag(handler)
        answer = await rag.ask("anything")

        assert answer.degraded is True
        assert answer.citations

    async def test_malformed_json_is_unavailable(self, make_rag):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"<html>not json</html>")

        rag = make_rag(handler)
        answer = await rag.ask("anything")
        assert answer.unavailable is True

    async def test_unexpected_status_is_unavailable(self, make_rag):
        rag = make_rag(json_handler({"detail": "nope"}, status_code=422))
        answer = await rag.ask("x")
        assert answer.unavailable is True


class TestHealth:
    async def test_healthy_when_200(self, make_rag):
        rag = make_rag(json_handler({"status": "ok", "series": 500}))
        assert await rag.healthy() is True

    async def test_unhealthy_when_unreachable(self, make_rag):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        rag = make_rag(handler)
        assert await rag.healthy() is False


class TestApiKeyHeader:
    """T6: the bot authenticates to a protected manhwa-rag."""

    def test_key_is_sent_when_configured(self):
        from bot.rag import RagClient

        client = RagClient("http://rag.test", api_key="s3cret")
        assert client._client.headers.get("X-API-Key") == "s3cret"

    def test_no_header_when_unset(self):
        from bot.rag import RagClient

        client = RagClient("http://rag.test")
        assert "X-API-Key" not in client._client.headers
