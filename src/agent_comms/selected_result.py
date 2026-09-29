"""A settled selected result owns auxiliary cursor projection and failure notices."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .assignment_states import AssignmentState, CompletedAssignment, IgnoredAssignment
from .coordination_errors import IdentityConflict, PublicationActivationBlocked, StaleFence
from .diagnostics import record_terminal_failure
from .errors import RelationViolationError
from .fresh_private_session import FreshPrivateSession
from .messages import MessageType
from .native_source_cursor import NativeSourceCursor
from .private_sidecar import SidecarCommitUnknown
from .wake import derive_exact_reply_target


@dataclass(frozen=True, slots=True)
class CoordinatedTurn:
    assignment_id: str = field(metadata={"wire_name": "claim_id"})
    disposition: type[AssignmentState]
    input_id: str
    response_message_id: str | None
    exact_target: str | None
    cursor_status: str = "unavailable"
    fresh_session: FreshPrivateSession | None = None

    @classmethod
    def ignored(cls, participant, session, input_id):
        return cls(
            participant.assignment.assignment_id,
            IgnoredAssignment,
            input_id,
            None,
            None,
            cls.cursor_status_for(participant, input_id),
            session.creation,
        )

    @classmethod
    def published(cls, participant, session, input_id, published):
        receipt = published.publication_receipt
        if receipt is None:
            raise IdentityConflict("fenced response has no durable receipt")
        return cls(
            participant.assignment.assignment_id,
            CompletedAssignment,
            input_id,
            receipt.message_id,
            published.execution.exact_target,
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


def publish_native_failure(participant, input_id, description, *, native_response=None):
    """Existing durable alert, never another wake or retry authorization."""
    diagnostic = record_terminal_failure(
        participant.comms.root,
        turn_id=input_id,
        thread=participant.owner.thread.name,
        event={},
        sequences=(participant.initial.message.seq,),
        native_response=native_response,
    )
    target = derive_exact_reply_target(participant.initial.message)
    assert target is not None
    participant.comms.messaging.send(
        participant.owner.thread.name,
        target,
        f"Message processing failed: {description} "
        f"No automatic retry. [Open diagnostic]({diagnostic.as_uri()})",
        MessageType.ALERT,
        notice=True,
    )
