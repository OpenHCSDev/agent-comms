from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from agent_comms.coordination.errors import PublicationActivationBlocked
from agent_comms.coordination.owner_fence import OwnerFence
from agent_comms.coordination.private_runtime_schema import PrivateCoordinatorTable
from agent_comms.coordination.tables.publications import PublicationIntents
from agent_comms.nominal.field_codec import RootId
from agent_comms.storage.typed_table import Column, ForeignKey, ImmutableRows, WithoutRowid


class ResponseTable(ImmutableRows, PrivateCoordinatorTable):
    """An immutable response definition with its write checks and append evidence."""

    schema_error = PublicationActivationBlocked


@dataclass(frozen=True)
class PublicationAppendDispatches(ResponseTable, WithoutRowid):
    execution_id: Annotated[str, Column(primary_key=True)]
    exact_target: Annotated[
        str, Column(primary_key=True, check="length(exact_target) BETWEEN 1 AND 256")
    ]
    wire_root_id: RootId
    owner_generation: Annotated[int, Column(check="owner_generation>0")]
    attempt_ordinal: Annotated[int, Column(check="attempt_ordinal>0")]
    dispatched_at_ms: Annotated[int, Column(check="dispatched_at_ms>=0")]

    @classmethod
    def references(cls):
        return (ForeignKey(columns=("execution_id", "exact_target"), target=PublicationIntents,
                          target_columns=("execution_id", "exact_target")),)

    def matches(self, wire_root_id: RootId, fence: OwnerFence, exact_target: str) -> bool:
        return self.matches_attempt(
            wire_root_id, fence.execution_id, fence.owner_generation,
            fence.attempt_ordinal, exact_target,
        )

    def matches_attempt(
        self, wire_root_id: RootId, execution_id: str, owner_generation: int,
        attempt_ordinal: int, exact_target: str,
    ) -> bool:
        return self == PublicationAppendDispatches(
            execution_id=execution_id,
            exact_target=exact_target,
            wire_root_id=wire_root_id,
            owner_generation=owner_generation,
            attempt_ordinal=attempt_ordinal,
            dispatched_at_ms=self.dispatched_at_ms,
        )
