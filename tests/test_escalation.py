"""Handoff policy and the operator notification."""

from __future__ import annotations

from bot.escalation import Escalator, reason_for
from bot.schema import RagAnswer


class TestReasonFor:
    def test_good_answer_needs_no_human(self):
        assert reason_for(RagAnswer(text="an answer")) is None

    def test_refusal(self):
        assert reason_for(RagAnswer(refused=True)) == "refused"

    def test_degraded(self):
        assert reason_for(RagAnswer(degraded=True)) == "degraded"

    def test_unavailable(self):
        assert reason_for(RagAnswer(unavailable=True)) == "unavailable"

    def test_unavailable_outranks_refused(self):
        """An unreachable backend is an outage, not a refusal — the operator
        needs to know which one it is."""
        answer = RagAnswer(unavailable=True, refused=True)
        assert reason_for(answer) == "unavailable"


class TestEscalator:
    async def test_records_a_row(self, store):
        escalator = Escalator(store)
        await escalator.escalate("telegram", "7", "a hard question", "refused")
        assert (await store.counts())["escalations"] == 1

    async def test_notifies_the_operator(self, store):
        sent: list[str] = []

        async def notifier(text: str) -> None:
            sent.append(text)

        escalator = Escalator(store, notifier)
        await escalator.escalate("telegram", "7", "a hard question", "refused")

        assert len(sent) == 1
        assert "refused" in sent[0]
        assert "a hard question" in sent[0]
        assert "telegram:7" in sent[0]

    async def test_works_with_no_operator_configured(self, store):
        """ADMIN_CHAT_ID is optional; the record must still be written."""
        escalator = Escalator(store)
        await escalator.escalate("whatsapp", "7", "q", "refused")
        assert (await store.counts())["escalations"] == 1

    async def test_a_failing_notifier_does_not_break_the_reply(self, store):
        """The user's answer must not depend on the operator's chat working."""

        async def notifier(text: str) -> None:
            raise RuntimeError("telegram is down")

        escalator = Escalator(store, notifier)
        await escalator.escalate("telegram", "7", "q", "refused")

        assert (await store.counts())["escalations"] == 1

    async def test_notifier_can_be_installed_later(self, store):
        sent: list[str] = []

        async def notifier(text: str) -> None:
            sent.append(text)

        escalator = Escalator(store)
        escalator.set_notifier(notifier)
        await escalator.escalate("telegram", "7", "q", "refused")
        assert sent
