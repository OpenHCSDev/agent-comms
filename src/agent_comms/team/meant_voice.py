"""The rule gate's voice on the bus: his question to the session, and the session's answer to him."""

from __future__ import annotations

from abc import abstractmethod

from agent_comms.comms import Comms
from agent_comms.messaging.channel_targets import ThreadTarget
from agent_comms.task_attachments.owner_notices import MeantNotice
from agent_comms.team.rule_deal import Asked, RuleDeal, Speaking


class BusVoice(Speaking):
    """Every gate that publishes on the bus: a member names only the thread it gates."""

    comms: Comms

    @abstractmethod
    def speaker(self) -> str:
        """The thread whose session this deal gates."""

    def say(self, line: str, deal: RuleDeal) -> Asked:
        """His line goes to the thread under his own thread, as every owner notice does."""
        message = MeantNotice().publish(self.comms.messaging, ThreadTarget(self.speaker()), line)
        return Asked(message.message_id, line)

    def answered(self, question: Asked, answer: str) -> None:
        """The session's own audit answer replies to his question, so he reads it where he
        asked. Tristan, 2026-11-02: "are agents answering the do you mean it after compaction?
        I don't see them answering"."""
        reference = self.comms.bus.bus.message_reference(question.message_id)
        for human in self.comms.registry.snapshot().user_thread().existing():
            self.comms.messaging.send(self.speaker(), human.name, answer, answers=(reference,))
