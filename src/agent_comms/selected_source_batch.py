"""Captured selected sources share one input; they do not create another inbox."""

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .bus_publication import CommittedDelivery
from .channel_input_batch import InputBatch
from .coordination_errors import IdentityConflict
from .coordination_tables.assignments import WakeAssignment
from .field_codec import FieldCodec
from .messages import Message, MessageType
from .native_pi import NativePiUnavailable
from .wake import derive_exact_reply_target

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
class SelectedSourceBatch(InputBatch):
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
    def admits_multiple(self) -> bool:
        return len(self.sources) > 1

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

    def response_instruction(self, sender: str) -> str:
        examples = tuple(
            Message(sender, target, "Combined answer for originals on this reply route only",
                    MessageType.INFO, timestamp=0)
            for target in self.targets
        )
        return (
            "Return ONLY a JSON array of Message records, one combined answer per listed reply route. "
            "Use the shown sender, target and info type; replace each text with its answer. "
            "Do not disclose another route's private request or answer in this route's text. "
            "No markdown fences, extra records or additional metadata. Publication owns actual timing. "
            "Declared Message examples: " + json.dumps(FieldCodec.encode(examples)) + "\n"
        )

    def response_messages(self, text: str, sender: str) -> tuple[Message, ...]:
        """Decode answer proposals once; original sources own every permitted route.

        These uncommitted Message values grant no publication. The fenced
        publisher still owns the timestamp, audience, receipt and actual row.
        """
        try:
            replies = FieldCodec.decode(tuple[Message, ...], json.loads(text))
            for reply in replies:
                expected = Message(sender, reply.target, reply.body, MessageType.INFO,
                                   timestamp=reply.timestamp)
                if reply != expected:
                    raise ValueError("Native reply claims ungranted message metadata")
            by_target = {reply.target: reply for reply in replies}
            if len(by_target) != len(replies):
                raise ValueError("Native answer repeats an original reply route")
            if by_target.keys() != set(self.targets):
                raise ValueError("Native answer does not cover the exact original reply routes")
            return tuple(by_target[target] for target in self.targets)
        except (TypeError, ValueError) as error:
            raise NativePiUnavailable("Native batch answer does not match its original reply routes") from error
