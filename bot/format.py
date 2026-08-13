"""Render a `Reply` for one channel.

Telegram is sent with `parse_mode=HTML`, not MarkdownV2. MarkdownV2 requires
escaping eighteen characters anywhere they appear — including `.`, `-` and `!`
— and manga titles are full of them. A single missed escape makes Telegram
reject the whole message, so the failure mode is "the bot silently says
nothing". HTML needs three escapes and has no such trap.

Both channels cap a text message at 4096 characters. Truncation happens on the
*body*, never on the assembled string: cutting the assembled string could slice
through an `<a href=...>` tag and produce markup Telegram refuses to parse.
"""

from __future__ import annotations

import html

from bot.schema import Channel, Citation, Reply

# Enough sources to be credible, few enough to stay readable on a phone.
MAX_CITATIONS = 4
MAX_QUOTE_CHARS = 160
ELLIPSIS = "…"


def render(reply: Reply, channel: Channel, *, max_chars: int = 4096) -> str:
    """Assemble the message. The body is trimmed to whatever the tail leaves."""
    tail = _tail(reply, channel)
    budget = max_chars - len(tail)
    body = _truncate(reply.body, budget)
    if channel is Channel.TELEGRAM:
        body = _escape_text(body)
    return (body + tail)[:max_chars]


def _escape_text(text: str) -> str:
    """Escape text content for Telegram's HTML mode.

    `quote=False`, so an apostrophe stays an apostrophe instead of becoming
    `&#x27;`. Only `<`, `>` and `&` can break the markup in a text node, and
    every answer here is full of apostrophes — no reason to make them all
    depend on the client resolving numeric entities. Attribute values are a
    different matter and are escaped with `quote=True` where they are built.
    """
    return html.escape(text, quote=False)


def _tail(reply: Reply, channel: Channel) -> str:
    """Everything below the answer: filters, sources, note."""
    bold, italic, esc = _primitives(channel)
    parts: list[str] = []

    if reply.filters:
        parts.append(italic("Filtered on: " + esc(" · ".join(reply.filters))))

    citations = _dedupe(reply.citations)[:MAX_CITATIONS]
    if citations:
        lines = [bold("Sources")]
        for i, citation in enumerate(citations, 1):
            lines.append(f"{i}. {_render_citation(citation, channel)}")
        parts.append("\n".join(lines))

    if reply.rejected_claims:
        # This is the citation verifier visibly working, so it is stated rather
        # than hidden: the backend checked every quote and threw these away.
        n = reply.rejected_claims
        parts.append(
            italic(
                f"{n} unverifiable claim{'s' if n != 1 else ''} "
                f"dropped before you saw this."
            )
        )

    if reply.note:
        parts.append(italic(esc(reply.note)))

    return ("\n\n" + "\n\n".join(parts)) if parts else ""


def _render_citation(citation: Citation, channel: Channel) -> str:
    bold, italic, esc = _primitives(channel)
    title = esc(citation.title)

    if channel is Channel.TELEGRAM and citation.url:
        head = f'<a href="{html.escape(citation.url, quote=True)}">{title}</a>'
    else:
        head = bold(title)

    line = head
    if citation.quote:
        quote = _truncate(citation.quote, MAX_QUOTE_CHARS)
        line += "\n   " + italic("“" + esc(quote) + "”")
    # WhatsApp has no link markup; a bare URL autolinks in the client.
    if channel is Channel.WHATSAPP and citation.url:
        line += "\n   " + citation.url
    return line


def _primitives(channel: Channel):
    """(bold, italic, escape) for the channel."""
    if channel is Channel.TELEGRAM:
        return (
            lambda s: f"<b>{s}</b>",
            lambda s: f"<i>{s}</i>",
            _escape_text,
        )
    # WhatsApp: *bold*, _italic_, and no escaping scheme at all. Asterisks and
    # underscores inside a title simply render as emphasis; there is nothing to
    # escape them with, so the text is passed through unchanged.
    return (lambda s: f"*{s}*", lambda s: f"_{s}_", lambda s: s)


def _dedupe(citations: list[Citation]) -> list[Citation]:
    """One entry per source.

    A single source often supports several claims, and repeating it four times
    reads as four sources. The first quote is kept as the representative one.
    """
    seen: set[str] = set()
    out: list[Citation] = []
    for citation in citations:
        key = citation.ref or citation.title
        if key in seen:
            continue
        seen.add(key)
        out.append(citation)
    return out


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    # Prefer a word boundary, but only if one is reasonably near the cut.
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip() + ELLIPSIS
