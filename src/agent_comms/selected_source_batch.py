"""Captured selected sources share one input; they do not create another inbox."""

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .bus_publication import CommittedDelivery
from .coordination_errors import IdentityConflict
from .coordination_tables.assignments import WakeAssignment
from .messages import Message, MessageType
from .native_pi import NativePiUnavailable
from .wake import derive_exact_reply_target
from .turn_context import InstructionFile, ReplyRouteSegment, WireProvenance

if TYPE_CHECKING:
    from .assignment_store import AssignmentStore


@dataclass(frozen=True)
class SelectedSource:
    """An original delivery and its relation to the owning mutable claim store.

    Membership is captured once; lifecycle belongs to AssignmentStore. Native
    send stages separately capture exact rows for their reservation fences.
    """

    assignment_id: str
    store: AssignmentStore
    delivery: CommittedDelivery

    @property
    def assignment(self) -> WakeAssignment:
        with self.store.session.read():
            current = self.store.get(self.assignment_id)
            current.require_committed_source(self.delivery)
            return current

    def require(self, owner) -> None:
        self.assignment.require_selected_source(self.delivery, owner)


@dataclass(frozen=True)
class SelectedSourceBatch:
    sources: tuple[SelectedSource, ...]

    def __post_init__(self):
        if not self.sources:
            raise IdentityConflict("Selected input needs original sources")
        if any(source.store is not self.sources[0].store for source in self.sources):
            raise IdentityConflict("Selected batch crosses assignment store ownership")
        assignments = self.assignments
        if len(set(self.assignment_ids)) != len(assignments):
            raise IdentityConflict("Selected batch repeats an original source")
        if tuple(sorted(assignments, key=lambda row: row.wire_seq)) != assignments:
            raise IdentityConflict("Selected batch changed original source order")
        if len({row.recipient_lookup for row in assignments}) != 1:
            raise IdentityConflict("Selected batch crosses recipient ownership")
        if any(derive_exact_reply_target(source.delivery.message) is None for source in self.sources):
            raise IdentityConflict("Selected source lacks an original reply route")

    @property
    def assignments(self) -> tuple[WakeAssignment, ...]:
        with self.sources[0].store.session.read():
            return tuple(source.assignment for source in self.sources)

    @property
    def assignment_ids(self) -> tuple[str, ...]:
        return tuple(source.assignment_id for source in self.sources)

    @property
    def execution_id(self) -> str:
        return "wirev1" + hashlib.sha256(
            json.dumps(self.assignment_ids, separators=(",", ":")).encode()
        ).hexdigest()

    @property
    def requires_triage(self) -> bool:
        # A mandatory FULL original already requires work. Its pending peers are
        # considered in that same original full input, not separate triage calls.
        return all(row.lifecycle.requires_selected_triage() for row in self.assignments)

    @property
    def targets(self) -> tuple[str, ...]:
        """Original reply routes in first-source order, never a second route store."""
        return tuple(dict.fromkeys(
            derive_exact_reply_target(source.delivery.message) for source in self.sources
        ))

    def response_segment(self):
        """The reply route belongs to the original owners; no answer format is
        demanded of the model."""
        instruction = InstructionFile.read("reply-route.md")
        return ReplyRouteSegment(
            provenance=(
                instruction.source,
                *(WireProvenance(source.delivery.message.reference) for source in self.sources),
            ),
            instruction=instruction,
            targets=self.targets,
        )

    def response_messages(self, text: str, sender: str) -> tuple[Message, ...]:
        """The model's one plain answer, stamped onto each original reply route.

        The turn already fixes the sender, targets and type, so routing is not
        asked of the model and no answer shape is decoded. These Message values
        are proposals only; the sending step owns the actual timestamp,
        audience and bus row.
        """
        answer = text.strip()
        if not answer:
            raise NativePiUnavailable("Native turn produced no publishable answer")
        return tuple(
            Message(sender, target, answer, MessageType.INFO)
            for target in self.targets
        )
