"""Captured selected sources share one input; they do not create another inbox."""

from dataclasses import dataclass

from .bus_publication import CommittedDelivery
from .channel_input_batch import InputBatch
from .coordination_errors import IdentityConflict
from .coordination_tables.assignments import WakeAssignment
from .wake import derive_exact_reply_target


@dataclass(frozen=True)
class SelectedSource:
    assignment: WakeAssignment
    delivery: CommittedDelivery

    def require(self, owner) -> None:
        self.assignment.require_selected_source(self.delivery, owner)


@dataclass(frozen=True)
class SelectedSourceBatch(InputBatch):
    sources: tuple[SelectedSource, ...]

    def __post_init__(self):
        if not self.sources:
            raise IdentityConflict("Selected input needs original sources")
        assignments = self.assignments
        if len(set(self.assignment_ids)) != len(assignments):
            raise IdentityConflict("Selected batch repeats an original source")
        if tuple(sorted(assignments, key=lambda row: row.wire_seq)) != assignments:
            raise IdentityConflict("Selected batch changed original source order")
        if len({row.recipient_lookup for row in assignments}) != 1:
            raise IdentityConflict("Selected batch crosses recipient ownership")
        targets = {derive_exact_reply_target(source.delivery.message) for source in self.sources}
        if len(targets) != 1 or None in targets:
            raise IdentityConflict("Selected response batch needs one original reply route")

    @property
    def admits_multiple(self) -> bool:
        return len(self.sources) > 1

    @property
    def assignments(self) -> tuple[WakeAssignment, ...]:
        return tuple(source.assignment for source in self.sources)

    @property
    def assignment_ids(self) -> tuple[str, ...]:
        return tuple(row.assignment_id for row in self.assignments)

    @property
    def requires_triage(self) -> bool:
        # A mandatory FULL original already requires work. Its pending peers are
        # considered in that same original full input, not separate triage calls.
        return all(row.lifecycle.requires_selected_triage() for row in self.assignments)

    @property
    def target(self) -> str:
        target = derive_exact_reply_target(self.sources[0].delivery.message)
        assert target is not None
        return target
