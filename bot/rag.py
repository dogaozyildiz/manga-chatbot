"""The only thing in this repo that knows manhwa-rag's HTTP contract.

The backend's response is rich — `refused`, `route`, `applied_filters`,
verified `claims`, `sources`, `rejected_claims` — and this module's job is to
keep all of that intact rather than flattening it to a string. The features
that make the bot worth showing (visible filters, real citations, honest
refusal) are all downstream of not throwing that away here.

Failure modes are normalised into the same `RagAnswer` rather than raised,
because every one of them still needs to produce something to say to a user:

* **503** — generation is unavailable (no key, or the free Groq tier's 100k
  tokens/day are spent). `/retrieve` takes an identical body, needs no key, and
  still returns matched sources with the applied filters. So a quota failure
  degrades to "here is what I found" instead of an apology.
* **connect error** — the backend is not running.
* **/health failing** — it is running but still loading the embedding model,
  which takes 60-90s. Worth distinguishing: "starting up, try in a minute" is
  actionable and "it's broken" is not.
"""

from __future__ import annotations

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_fixed,
)

from bot.schema import Citation, RagAnswer


class RagClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 45.0,
        top_k: int | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._top_k = top_k
        # An injected client is how the tests drive this with MockTransport,
        # and how a caller shares a connection pool.
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout
        )
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -- health ------------------------------------------------------------

    async def healthy(self) -> bool:
        """True when the backend is up *and* past its embedder warm-up.

        `/health` counts rows, so it only answers once the app has started;
        during warm-up the connection is refused or hangs.
        """
        try:
            response = await self._client.get("/health", timeout=5.0)
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    # -- asking ------------------------------------------------------------

    async def ask(self, question: str) -> RagAnswer:
        payload: dict = {"question": question}
        if self._top_k is not None:
            payload["k"] = self._top_k

        try:
            response = await self._post("/ask", payload)
        except httpx.HTTPError:
            return await self._when_ask_unreachable(payload)

        if response.status_code == 503:
            # Generation is down but retrieval is not. Documented behaviour,
            # not a guess: the backend raises 503 only on ProviderError.
            return await self._retrieve_only(payload, note_degraded=True)

        if response.status_code >= 400:
            return RagAnswer(unavailable=True)

        return self._parse_ask(response)

    async def _when_ask_unreachable(self, payload: dict) -> RagAnswer:
        """A transport failure on /ask. Separate whether anything is listening."""
        if await self.healthy():
            # It is up and answering /health, so the failure was specific to
            # this question — most likely the request outlived the timeout.
            return await self._retrieve_only(payload, note_degraded=True)
        return RagAnswer(unavailable=True)

    # Only transport errors are retried. A 503 is a deterministic "no quota"
    # and retrying it just spends the timeout budget twice.
    @retry(
        retry=retry_if_exception_type(httpx.TransportError),
        stop=stop_after_attempt(2),
        wait=wait_fixed(1),
        reraise=True,
    )
    async def _post(self, path: str, payload: dict) -> httpx.Response:
        return await self._client.post(path, json=payload)

    async def _retrieve_only(self, payload: dict, *, note_degraded: bool) -> RagAnswer:
        try:
            response = await self._post("/retrieve", payload)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError):
            return RagAnswer(unavailable=True)

        sources = data.get("sources") or []
        return RagAnswer(
            text="",
            refused=not sources,
            route=str(data.get("route", "")),
            applied_filters=list(data.get("applied_filters") or []),
            citations=[self._source_citation(s) for s in sources],
            degraded=note_degraded,
        )

    # -- parsing -----------------------------------------------------------

    def _parse_ask(self, response: httpx.Response) -> RagAnswer:
        try:
            data = response.json()
        except ValueError:
            return RagAnswer(unavailable=True)

        # `claim.ref` joins to `source.ref`; that join is what turns a quote
        # into a clickable, named source.
        sources = {
            str(s.get("ref")): s for s in (data.get("sources") or []) if s.get("ref")
        }

        citations: list[Citation] = []
        for claim in data.get("claims") or []:
            ref = str(claim.get("ref", ""))
            source = sources.get(ref, {})
            citations.append(
                Citation(
                    ref=ref,
                    title=str(source.get("title") or ref or "source"),
                    url=source.get("url"),
                    quote=str(claim.get("quote", "")),
                    kind=str(source.get("kind", "")),
                )
            )

        # A refusal carries no claims, but the sources it *did* match are still
        # worth showing — that is what makes the refusal transparent rather
        # than a dead end.
        if not citations and data.get("refused"):
            citations = [self._source_citation(s) for s in list(sources.values())[:3]]

        return RagAnswer(
            text=str(data.get("answer") or "").strip(),
            refused=bool(data.get("refused")),
            route=str(data.get("route", "")),
            applied_filters=list(data.get("applied_filters") or []),
            citations=citations,
            rejected_claims=int(data.get("rejected_claims") or 0),
            latency_ms=int(data.get("latency_ms") or 0),
            model=str(data.get("model", "")),
        )

    @staticmethod
    def _source_citation(source: dict) -> Citation:
        ref = str(source.get("ref", ""))
        return Citation(
            ref=ref,
            title=str(source.get("title") or ref or "source"),
            url=source.get("url"),
            quote="",
            kind=str(source.get("kind", "")),
        )
