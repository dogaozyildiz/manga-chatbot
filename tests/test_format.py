"""Rendering. The failure mode being guarded against is a message Telegram
refuses to parse, because that shows up as the bot saying nothing at all."""

from __future__ import annotations

from bot.format import MAX_CITATIONS, render
from bot.schema import Channel, Citation, Reply


def citation(**overrides) -> Citation:
    base = {
        "ref": "series:1",
        "title": "Rosen Blood",
        "url": "https://anilist.co/manga/1",
        "quote": "84 chapters",
        "kind": "series",
    }
    base.update(overrides)
    return Citation(**base)


class TestTelegram:
    def test_escapes_html_in_the_body(self):
        reply = Reply(body="Titles with <angle> & ampersand")
        out = render(reply, Channel.TELEGRAM)
        assert "&lt;angle&gt;" in out and "&amp;" in out
        assert "<angle>" not in out

    def test_escapes_html_in_titles(self):
        """A title containing markup must not become markup."""
        reply = Reply(body="x", citations=[citation(title="A <b>bold</b> title")])
        out = render(reply, Channel.TELEGRAM)
        assert "&lt;b&gt;bold&lt;/b&gt;" in out

    def test_apostrophes_stay_readable(self):
        """Text nodes only need <, > and & escaped. Turning every apostrophe
        into &#x27; makes the message depend on numeric-entity support for no
        reason — and almost every answer contains one."""
        out = render(Reply(body="I couldn't find it."), Channel.TELEGRAM)
        assert "couldn't find it." in out
        assert "&#x27;" not in out

    def test_url_attribute_is_quote_escaped(self):
        """An attribute is not a text node: a quote there does break markup."""
        reply = Reply(body="x", citations=[citation(url='https://e.com/a"b')])
        out = render(reply, Channel.TELEGRAM)
        assert '&quot;' in out
        assert 'href="https://e.com/a"b"' not in out

    def test_renders_source_as_a_link(self):
        reply = Reply(body="x", citations=[citation()])
        out = render(reply, Channel.TELEGRAM)
        assert '<a href="https://anilist.co/manga/1">Rosen Blood</a>' in out

    def test_source_without_url_is_bold_not_a_link(self):
        reply = Reply(body="x", citations=[citation(url=None)])
        out = render(reply, Channel.TELEGRAM)
        assert "<b>Rosen Blood</b>" in out
        assert "<a href" not in out

    def test_quotes_are_shown(self):
        reply = Reply(body="x", citations=[citation()])
        out = render(reply, Channel.TELEGRAM)
        assert "84 chapters" in out

    def test_filters_are_escaped(self):
        """`<100 chapters` is a real filter label and a real escaping trap."""
        reply = Reply(body="x", filters=["Korean", "<100 chapters"])
        out = render(reply, Channel.TELEGRAM)
        assert "&lt;100 chapters" in out
        assert "Filtered on:" in out

    def test_rejected_claims_are_reported(self):
        out = render(Reply(body="x", rejected_claims=2), Channel.TELEGRAM)
        assert "2 unverifiable claims" in out

    def test_single_rejected_claim_is_singular(self):
        out = render(Reply(body="x", rejected_claims=1), Channel.TELEGRAM)
        assert "1 unverifiable claim " in out

    def test_no_rejected_line_when_zero(self):
        out = render(Reply(body="x", rejected_claims=0), Channel.TELEGRAM)
        assert "unverifiable" not in out


class TestWhatsApp:
    def test_uses_asterisk_bold_not_html(self):
        reply = Reply(body="x", citations=[citation()])
        out = render(reply, Channel.WHATSAPP)
        assert "*Rosen Blood*" in out
        assert "<a href" not in out and "<b>" not in out

    def test_url_is_on_its_own_line(self):
        reply = Reply(body="x", citations=[citation()])
        out = render(reply, Channel.WHATSAPP)
        assert "https://anilist.co/manga/1" in out

    def test_body_is_not_html_escaped(self):
        reply = Reply(body="a < b & c")
        out = render(reply, Channel.WHATSAPP)
        assert "a < b & c" in out


class TestStructure:
    def test_deduplicates_repeated_sources(self):
        """Four claims from one source is one source, not four."""
        reply = Reply(
            body="x",
            citations=[citation(quote=f"quote {i}") for i in range(4)],
        )
        out = render(reply, Channel.TELEGRAM)
        assert out.count("Rosen Blood") == 1

    def test_caps_the_number_of_sources(self):
        reply = Reply(
            body="x",
            citations=[
                citation(ref=f"series:{i}", title=f"Series {i}") for i in range(10)
            ],
        )
        out = render(reply, Channel.TELEGRAM)
        assert f"{MAX_CITATIONS}." in out
        assert f"{MAX_CITATIONS + 1}." not in out

    def test_note_is_rendered(self):
        out = render(Reply(body="x", note="retrieval only"), Channel.TELEGRAM)
        assert "retrieval only" in out

    def test_plain_body_has_no_tail(self):
        out = render(Reply(body="Just an answer."), Channel.TELEGRAM)
        assert out == "Just an answer."


class TestTruncation:
    def test_respects_the_limit(self):
        reply = Reply(
            body="word " * 3000,
            filters=["Korean"],
            citations=[citation()],
            rejected_claims=2,
        )
        out = render(reply, Channel.TELEGRAM, max_chars=4096)
        assert len(out) <= 4096

    def test_the_tail_survives_a_long_body(self):
        """The sources are the point; they must not be what gets cut."""
        reply = Reply(body="word " * 3000, citations=[citation()])
        out = render(reply, Channel.TELEGRAM, max_chars=1000)
        assert "Rosen Blood" in out
        assert "…" in out

    def test_long_quotes_are_shortened(self):
        reply = Reply(body="x", citations=[citation(quote="q" * 500)])
        out = render(reply, Channel.TELEGRAM)
        assert "…" in out
        assert "q" * 500 not in out

    def test_no_unclosed_tags_after_truncation(self):
        """Truncating the assembled string could slice an <a href=...> in half
        and make Telegram reject the message. It must not happen."""
        reply = Reply(body="word " * 2000, citations=[citation()])
        out = render(reply, Channel.TELEGRAM, max_chars=900)
        assert out.count("<a href") == out.count("</a>")
        assert out.count("<b>") == out.count("</b>")
        assert out.count("<i>") == out.count("</i>")
