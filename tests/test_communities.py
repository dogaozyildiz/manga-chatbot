"""Reddit community lookup.

The behaviour under test is mostly about honesty: an outage must be
distinguishable from "no such community", and nothing may be returned that
Reddit did not actually send.
"""

from __future__ import annotations

import httpx
import pytest

from bot.communities import CommunityLookupError, RedditCommunities

TOKEN_BODY = {"access_token": "tok-1", "expires_in": 3600, "token_type": "bearer"}


def subreddit(name: str, subs: int = 1000, **overrides) -> dict:
    data = {
        "display_name": name,
        "subscribers": subs,
        "public_description": f"Everything about {name}.",
        "url": f"/r/{name}/",
        "over18": False,
    }
    data.update(overrides)
    return {"kind": "t5", "data": data}


def listing(*subs: dict) -> dict:
    return {"kind": "Listing", "data": {"children": list(subs)}}


def make(
    handler,
    *,
    client_id: str = "id",
    secret: str = "secret",
    clock=None,
) -> RedditCommunities:
    return RedditCommunities(
        client_id,
        secret,
        "python:manga-chatbot:test (by /u/tester)",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        clock=clock or (lambda: 0.0),
    )


def ok_handler(search_body: dict, token_body: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if "access_token" in request.url.path:
            return httpx.Response(200, json=token_body or TOKEN_BODY)
        return httpx.Response(200, json=search_body)

    return handler


class TestConfiguration:
    async def test_unconfigured_returns_nothing(self):
        client = make(ok_handler(listing()), client_id="", secret="")
        assert client.configured is False
        assert await client.search("Solo Leveling") == []

    async def test_configured_when_both_present(self):
        assert make(ok_handler(listing())).configured is True

    async def test_blank_query_returns_nothing(self):
        assert await make(ok_handler(listing())).search("   ") == []


class TestSearch:
    async def test_parses_results(self):
        client = make(ok_handler(listing(subreddit("manga", 3_000_000))))
        found = await client.search("manga")

        assert len(found) == 1
        assert found[0].name == "manga"
        assert found[0].subscribers == 3_000_000
        assert found[0].url == "https://www.reddit.com/r/manga/"
        assert "Everything about manga" in found[0].description

    async def test_sorted_by_subscribers(self):
        client = make(
            ok_handler(
                listing(
                    subreddit("small", 500),
                    subreddit("huge", 900_000),
                    subreddit("medium", 40_000),
                )
            )
        )
        assert [c.name for c in await client.search("x")] == ["huge", "medium", "small"]

    async def test_respects_the_limit(self):
        client = make(
            ok_handler(listing(*[subreddit(f"s{i}", 100 * i) for i in range(20)]))
        )
        assert len(await client.search("x", limit=3)) == 3

    async def test_drops_nsfw(self):
        """`include_over_18=off` is a request, not a guarantee."""
        client = make(
            ok_handler(listing(subreddit("safe"), subreddit("nsfw", over18=True)))
        )
        assert [c.name for c in await client.search("x")] == ["safe"]

    async def test_drops_user_profiles(self):
        """`u_someone` rows are profiles, not communities."""
        client = make(ok_handler(listing(subreddit("manga"), subreddit("u_someone"))))
        assert [c.name for c in await client.search("x")] == ["manga"]

    async def test_empty_results_are_not_an_error(self):
        assert await make(ok_handler(listing())).search("zzzz") == []

    async def test_sends_credentials_and_user_agent(self):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if "access_token" in request.url.path:
                return httpx.Response(200, json=TOKEN_BODY)
            return httpx.Response(200, json=listing(subreddit("manga")))

        await make(handler).search("manga")

        token_request, search_request = seen[0], seen[1]
        assert token_request.headers["authorization"].startswith("Basic ")
        assert search_request.headers["authorization"] == "Bearer tok-1"
        # Reddit blocks generic agents and forbids masking this.
        assert "manga-chatbot" in search_request.headers["user-agent"]
        assert search_request.url.params["q"] == "manga"

    async def test_absolute_url_is_left_alone(self):
        client = make(
            ok_handler(listing(subreddit("manga", url="https://old.reddit.com/r/manga/")))
        )
        assert (await client.search("x"))[0].url == "https://old.reddit.com/r/manga/"


class TestTokenHandling:
    async def test_token_is_reused(self):
        calls = {"token": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if "access_token" in request.url.path:
                calls["token"] += 1
                return httpx.Response(200, json=TOKEN_BODY)
            return httpx.Response(200, json=listing(subreddit("manga")))

        client = make(handler)
        await client.search("a")
        await client.search("b")
        assert calls["token"] == 1

    async def test_token_refreshes_after_expiry(self):
        calls = {"token": 0}
        now = {"t": 0.0}

        def handler(request: httpx.Request) -> httpx.Response:
            if "access_token" in request.url.path:
                calls["token"] += 1
                return httpx.Response(200, json={"access_token": "t", "expires_in": 100})
            return httpx.Response(200, json=listing(subreddit("manga")))

        client = make(handler, clock=lambda: now["t"])
        await client.search("a")
        now["t"] = 500.0
        await client.search("b")
        assert calls["token"] == 2

    async def test_bad_credentials_raise(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "invalid_client"})

        with pytest.raises(CommunityLookupError):
            await make(handler).search("manga")

    async def test_token_response_without_a_token_raises(self):
        with pytest.raises(CommunityLookupError):
            await make(ok_handler(listing(), token_body={"scope": "read"})).search("x")


class TestFailuresAreNotSilent:
    async def test_transport_error_raises(self):
        """An outage must never look like 'no communities exist'."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        with pytest.raises(CommunityLookupError):
            await make(handler).search("manga")

    async def test_rate_limited_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if "access_token" in request.url.path:
                return httpx.Response(200, json=TOKEN_BODY)
            return httpx.Response(429, json={"message": "Too Many Requests"})

        with pytest.raises(CommunityLookupError):
            await make(handler).search("manga")

    async def test_expired_token_is_discarded(self):
        """A 401 mid-flight must not leave a dead token cached."""

        def handler(request: httpx.Request) -> httpx.Response:
            if "access_token" in request.url.path:
                return httpx.Response(200, json=TOKEN_BODY)
            return httpx.Response(401, json={"message": "Unauthorized"})

        client = make(handler)
        with pytest.raises(CommunityLookupError):
            await client.search("manga")
        assert client._token is None

    async def test_malformed_json_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if "access_token" in request.url.path:
                return httpx.Response(200, json=TOKEN_BODY)
            return httpx.Response(200, content=b"<html>nope</html>")

        with pytest.raises(CommunityLookupError):
            await make(handler).search("manga")

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"data": None},
            {"data": {"children": None}},
            {"data": {"children": [None, 3, "x"]}},
            {"data": {"children": [{"data": None}]}},
            {"data": {"children": [{"data": {"display_name": ""}}]}},
        ],
    )
    async def test_malformed_listings_yield_nothing(self, body):
        assert await make(ok_handler(body)).search("manga") == []
