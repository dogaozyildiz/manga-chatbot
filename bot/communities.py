"""Find real subreddits for a series, via Reddit's search API.

This is the first capability whose data does not come from manhwa-rag. The
catalogue knows about series; it knows nothing about where people discuss them.
That left two options — let a model name some plausible-sounding subreddits, or
look them up — and a project whose entire premise is verified citations does not
get to invent community names. So every suggestion here is a subreddit that
exists, with the subscriber count and description Reddit itself returns.

Notes on the API, current as of August 2026:

* **OAuth is mandatory.** Unauthenticated `.json` endpoints now return 403.
  App-only `client_credentials` is enough for search — no user account needed.
* **A descriptive User-Agent is required.** Reddit rate-limits or blocks generic
  ones, and the Data API Terms forbid masking it.
* `/api/recommend/sr/{srnames}` looks like the obvious endpoint and is marked
  **DEPRECATED** in Reddit's own docs, so it is not used. `/subreddits/search`
  is current and matches against display name and public description.
* Free tier is 100 queries/minute per OAuth client. Per-user throttling upstream
  in `bot/ratelimit.py` keeps a single bot far below that.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

from bot.schema import Community

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API_BASE = "https://oauth.reddit.com"
# Refresh a little early rather than discovering expiry as a mid-request 401.
TOKEN_SKEW_SECONDS = 60


class CommunityLookupError(RuntimeError):
    """Reddit could not be reached or refused the request.

    Distinct from "no results" on purpose: an outage that reports itself as
    "no communities exist for this series" is a lie, and the caller needs to be
    able to tell the two apart.
    """


class RedditCommunities:
    def __init__(
        self,
        client_id: str = "",
        client_secret: str = "",
        user_agent: str = "",
        *,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._user_agent = user_agent or "python:manga-chatbot:v0.1 (by /u/unknown)"
        self._clock = clock
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._client = client or httpx.AsyncClient(timeout=15.0)
        self._owns_client = client is None

    @property
    def configured(self) -> bool:
        return bool(self._client_id and self._client_secret)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def search(self, query: str, limit: int = 4) -> list[Community]:
        """Subreddits matching `query`, most subscribed first.

        Raises `CommunityLookupError` if Reddit cannot be reached. Returns an
        empty list only when the search genuinely matched nothing.
        """
        query = (query or "").strip()
        if not query or not self.configured:
            return []

        token = await self._access_token()
        try:
            response = await self._client.get(
                f"{API_BASE}/subreddits/search",
                params={
                    "q": query,
                    # `activity` skews to whatever is busy right now; a series
                    # community is a name match, not a trending one.
                    "sort": "relevance",
                    # Over-fetch, because NSFW and profile rows get dropped
                    # below and would otherwise thin the list unpredictably.
                    "limit": min(limit * 4, 100),
                    "include_over_18": "off",
                },
                headers=self._headers(token),
            )
        except httpx.HTTPError as exc:
            raise CommunityLookupError(str(exc)) from exc

        if response.status_code == 401:
            # Token rejected mid-flight; drop it so the next call re-authenticates.
            self._token = None
            raise CommunityLookupError("reddit rejected the access token")
        if response.status_code >= 400:
            raise CommunityLookupError(f"reddit returned {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise CommunityLookupError("reddit returned a non-JSON body") from exc

        found = [
            community
            for child in _children(payload)
            if (community := _to_community(child)) is not None
        ]
        found.sort(key=lambda c: c.subscribers, reverse=True)
        return found[:limit]

    # -- auth --------------------------------------------------------------

    async def _access_token(self) -> str:
        now = self._clock()
        if self._token and now < self._token_expires_at:
            return self._token

        try:
            response = await self._client.post(
                TOKEN_URL,
                data={"grant_type": "client_credentials"},
                auth=httpx.BasicAuth(self._client_id, self._client_secret),
                headers={"User-Agent": self._user_agent},
            )
        except httpx.HTTPError as exc:
            raise CommunityLookupError(str(exc)) from exc

        if response.status_code >= 400:
            raise CommunityLookupError(
                f"reddit refused the credentials ({response.status_code})"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise CommunityLookupError("token response was not JSON") from exc

        token = payload.get("access_token")
        if not token:
            raise CommunityLookupError("token response carried no access_token")

        self._token = str(token)
        self._token_expires_at = now + float(payload.get("expires_in", 3600)) - (
            TOKEN_SKEW_SECONDS
        )
        return self._token

    def _headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}", "User-Agent": self._user_agent}


def _children(payload: object) -> list[dict]:
    """Rows out of a Reddit listing, defensively."""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    children = data.get("children")
    return [c for c in children if isinstance(c, dict)] if isinstance(children, list) else []


def _to_community(child: dict) -> Community | None:
    data = child.get("data")
    if not isinstance(data, dict):
        return None

    name = str(data.get("display_name") or "").strip()
    if not name:
        return None
    # `u_someone` rows are personal profiles, which are not communities.
    if name.startswith("u_"):
        return None
    # `include_over_18=off` is a request, not a guarantee — filter regardless.
    if data.get("over18"):
        return None

    path = str(data.get("url") or f"/r/{name}/")
    return Community(
        name=name,
        subscribers=int(data.get("subscribers") or 0),
        description=str(data.get("public_description") or "").strip(),
        url=f"https://www.reddit.com{path}" if path.startswith("/") else path,
    )
