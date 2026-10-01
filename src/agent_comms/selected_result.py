"""A settled selected result owns auxiliary cursor projection and failure notices."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .assignment_states import AssignmentState, CompletedAssignment, FailedAssignment, IgnoredAssignment
from .coordination_errors import IdentityConflict, PublicationActivationBlocked, StaleFence
from .coordination_tables.publications import PublicationReceipt
from .diagnostics import record_terminal_failure
from .errors import RelationViolationError
from .fresh_private_session import FreshPrivateSession
from .messages import MessageType
from .native_source_cursor import NativeSourceCursor
from .private_sidecar import SidecarCommitUnknown
from .wake import derive_exact_reply_target


@dataclass(frozen=True, slots=True)
class CoordinatedTurn:
    assignment_ids: tuple[str, ...] = field(metadata={"wire_name": "claim_ids"})
    disposition: type[AssignmentState]
    input_id: str
    publications: tuple[PublicationReceipt, ...]
    cursor_status: str = "unavailable"
    fresh_session: FreshPrivateSession | None = None

    @classmethod
    def failed(cls, participant, session, input_id):
        return cls(
            participant.batch.assignment_ids,
            FailedAssignment,
            input_id,
            (),
            cls.cursor_status_for(participant, input_id),
            session.creation,
        )

    @classmethod
    def ignored(cls, participant, session, input_id):
        return cls(
            participant.batch.assignment_ids,
            IgnoredAssignment,
            input_id,
            (),
            cls.cursor_status_for(participant, input_id),
            session.creation,
        )

    @classmethod
    def published(cls, participant, session, input_id, publications: tuple[PublicationReceipt, ...]):
        return cls(
            participant.batch.assignment_ids,
            CompletedAssignment,
            input_id,
            publications,
            cls.cursor_status_for(participant, input_id),
            session.creation,
        )

    @staticmethod
    def cursor_status_for(participant, input_id) -> str:
        # Projection is auxiliary: failure never undoes a committed response,
        # creates another attempt or gives permission to replay native input.
        try:
            cursor = NativeSourceCursor(
                participant.bus,
                participant.store,
                wire_root_id=participant.root_id,
            ).advance(
                owner=participant.owner.thread,
                owner_admission_generation=participant.owner.admission_generation,
                owner_generation=participant.identity.generation,
                committed_input_id=input_id,
            )
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
):
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
    for target in participant.batch.targets:
        participant.comms.messaging.send(
            participant.owner.thread.name,
            target,
            f"Message processing failed: {description} "
            f"No automatic retry. [Open diagnostic]({diagnostic.as_uri()})",
            MessageType.ALERT,
            notice=True,
        )
