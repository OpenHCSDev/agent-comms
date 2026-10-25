"""A settled selected result owns auxiliary cursor projection and failure notices."""

from __future__ import annotations

from typing import Annotated
from dataclasses import dataclass

from agent_comms import field_codec

from .assignment_states import AssignmentState
from .coordination_tables.publications import PublicationEvidence
from .diagnostics import record_terminal_failure
from .field_codec import AssignmentId, FieldCodec, InputId
from .messages import AlertKind, NoticeRole
from .send_result import SendResult
from .turn_failure import BackendFailed
from .turn_outcome import TurnOutcome


@dataclass(frozen=True, slots=True)
class CoordinatedTurn(TurnOutcome):
    assignment_ids: Annotated[tuple[AssignmentId, ...], field_codec.WireName("claim_ids")]
    disposition: type[AssignmentState]
    input_id: InputId
    publications: tuple[PublicationEvidence, ...]
    cursor_status: str
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
        # A failed projection does not undo a committed response or authorize replay.
        # Report the original error rather than pretending it is a cursor observation.
        state = await participant.cursor_delivery.advance(participant, input_id)
        return "proven" if state.proves_input(input_id) else "blocked_gap"


def publish_native_failure(
    participant, input_id, description, *, native_response=None, source_error=None
) -> SendResult:
    """Existing durable alert, never another wake or retry authorization."""
    diagnostic = record_terminal_failure(
        participant.comms.root,
        subject=FieldCodec.decode(InputId, input_id),
        thread=participant.owner.thread.name,
        failure=BackendFailed,
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
