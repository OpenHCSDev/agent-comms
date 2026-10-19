"""A settled selected result owns auxiliary cursor projection and failure notices."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .assignment_states import AssignmentState
from .coordination_errors import IdentityConflict, PublicationActivationBlocked, StaleFence
from .coordination_tables.publications import PublicationEvidence
from .diagnostics import record_terminal_failure
from .errors import RelationViolationError
from .messages import AlertKind, NoticeRole
from .private_sidecar import SidecarCommitUnknown
from .send_result import SendResult


@dataclass(frozen=True, slots=True)
class CoordinatedTurn:
    assignment_ids: tuple[str, ...] = field(metadata={"wire_name": "claim_ids"})
    disposition: type[AssignmentState]
    input_id: str
    publications: tuple[PublicationEvidence, ...]
    cursor_status: str = "unavailable"
    notices: tuple[str, ...] = ()

    @classmethod
    async def capture(
        cls, participant, input_id, disposition: type[AssignmentState],
        publications: tuple[PublicationEvidence, ...] = (),
        *, notices: tuple[str, ...] = (),
    ):
        return cls(
            participant.batch.assignment_ids,
            disposition,
            input_id,
            publications,
            await cls.cursor_status_for(participant, input_id),
            notices,
        )

    @staticmethod
    async def cursor_status_for(participant, input_id) -> str:
        # Projection is auxiliary: failure never undoes a committed response,
        # creates another attempt or gives permission to replay native input.
        try:
            cursor = await participant.cursor_delivery.advance(participant, input_id)
        except (
            OSError,
            sqlite3.Error,
            ValueError,
            IdentityConflict,
            StaleFence,
            RelationViolationError,
            PublicationActivationBlocked,
            SidecarCommitUnknown,
        ):
            return "unavailable"
        return "proven" if cursor is not None and cursor.input_id == input_id else "blocked_gap"


def publish_native_failure(
    participant, input_id, description, *, native_response=None, source_error=None
) -> SendResult:
    """Existing durable alert, never another wake or retry authorization."""
    diagnostic = record_terminal_failure(
        participant.comms.root,
        turn_id=input_id,
        thread=participant.owner.thread.name,
        event={},
        sequences=tuple(source.delivery.message.seq for source in participant.batch.sources),
        native_response=native_response,
        source_error=source_error,
    )
    return participant.comms.messaging.send(
        participant.owner.thread.name,
        participant.batch.target,
        f"Message processing failed: {description} "
        f"No automatic retry. [Open diagnostic]({diagnostic.as_uri()})",
        AlertKind,
        role=NoticeRole(),
    )
