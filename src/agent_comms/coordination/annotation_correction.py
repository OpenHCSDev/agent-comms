"""Correcting one original stored model answer through the human identity owner."""

from __future__ import annotations

from agent_comms.comms import Comms
from agent_comms.coordination.coordinator import Coordination
from agent_comms.coordination.database import CoordinationStore
from agent_comms.projects.checkouts import Checkout
from agent_comms.working_memory.working_memory_labels import HumanLabel, ModelLabel
from agent_comms.working_memory.working_memory_questions import SpanAnswer


class CorrectsAnnotation:
    """What the CLI verb and the owner request both are: a human's answer to one stored model
    label, recorded under the identity that owns the worktree. Each declares the three fields
    with its own boundary's metadata; the correction is written once, here."""

    label: ModelLabel
    answer: type[SpanAnswer]
    worktree: Checkout

    def correct(self, comms: Comms) -> HumanLabel:
        author = comms.messaging.user_identity(self.worktree).incarnation
        snapshot = comms.registry.snapshot()
        with Coordination(str(CoordinationStore.path_in(comms.root))) as store:
            return store.annotations.correct(self.label, self.answer, author, snapshot)
