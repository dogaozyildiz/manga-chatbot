"""The types that cross the channel boundary.

A `Reply` is deliberately structured rather than pre-rendered text: Telegram
wants HTML and WhatsApp wants its own asterisk syntax, and building the string
in the conversation layer would force one channel's markup on the other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Channel(str, Enum):
    TELEGRAM = "telegram"
    WHATSAPP = "whatsapp"
    DISCORD = "discord"
    REDDIT = "reddit"


@dataclass(frozen=True)
class Citation:
    """One verified claim, joined to the source it cites.

    The backend only emits claims whose quote was found verbatim in the source
    it names — unverifiable ones are stripped before the response is built
    (manhwa-rag `app/api.py`, `answer.verified_claims`). So a Citation here is
    evidence, not a model's assertion that evidence exists.
    """

    ref: str
    title: str
    url: str | None = None
    quote: str = ""
    kind: str = ""


@dataclass(frozen=True)
class Community:
    """A real subreddit, as returned by Reddit's search API.

    Every field here comes off the wire — nothing is inferred and nothing is
    generated. That is the whole point: the catalogue knows about series, not
    about where people discuss them, so a suggested community is either looked
    up or it is a guess, and this project does not guess.
    """

    name: str  # "manga", without the r/ prefix
    subscribers: int
    description: str
    url: str

    def as_citation(self) -> Citation:
        """Render as a Citation so it flows through the existing formatter.

        Suggestions then get the same dedupe, truncation, numbering and
        per-channel markup as cited answers, with no parallel rendering path.
        """
        members = f"{_compact(self.subscribers)} members" if self.subscribers else ""
        return Citation(
            ref=f"reddit:{self.name}",
            title=f"r/{self.name}" + (f" · {members}" if members else ""),
            url=self.url,
            quote=self.description,
            kind="community",
        )


def _compact(n: int) -> str:
    """1234 -> 1.2k, 2100000 -> 2.1M. Chat has no room for seven digits."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1_000:
        return f"{n / 1_000:.1f}k".replace(".0k", "k")
    return str(n)


@dataclass
class RagAnswer:
    """A normalised view of the backend's `/ask` response."""

    text: str = ""
    refused: bool = False
    route: str = ""
    applied_filters: list[str] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    # Claims the backend's verifier rejected. Surfaced because a non-zero count
    # is the citation check visibly doing its job.
    rejected_claims: int = 0
    latency_ms: int = 0
    model: str = ""

    # Answered from `/retrieve` because generation was unavailable (503 — no
    # key, or the free tier's daily quota is spent). Sources are real; there is
    # no prose.
    degraded: bool = False
    # The backend could not be reached at all.
    unavailable: bool = False
    # The backend is up but still loading the embedding model (~60-90s).
    warming: bool = False


@dataclass
class Reply:
    """What the user gets back, before channel-specific rendering."""

    body: str
    filters: list[str] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    rejected_claims: int = 0
    # A short italicised line under the answer (e.g. "generation unavailable").
    note: str | None = None
    escalated: bool = False
