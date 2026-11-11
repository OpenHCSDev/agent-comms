from __future__ import annotations

import json
from abc import abstractmethod
from collections.abc import (
    Awaitable,
    Callable,
)
from dataclasses import dataclass
from functools import partial
from typing import (
    TYPE_CHECKING,
    ClassVar,
    Literal,
)

from agent_comms.coordination.assignment_states import (
    FailedAssignment,
    IgnoredAssignment,
    TriagePendingAssignment,
)
from agent_comms.coordination.coordinator import Coordination
from agent_comms.coordination.tables.assignments import WakeAssignment
from agent_comms.coordination.tables.executions import WireOrigin
from agent_comms.execution.selected.result import CoordinatedTurn
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import (
    FieldCodec,
    InputId,
    JsonShapeFamily,
    JsonShapeMember,
    MalformedRecord,
    TextRepresentation,
)
from agent_comms.storage.typed_table import TextStorage

if TYPE_CHECKING:
    from sqlite3 import Connection

    from agent_comms.coordinated_runtime import SelectedExecution
    from agent_comms.coordination.assignment_states import AssignmentState
    from agent_comms.execution.historical_native_inputs import HistoricalNativeInput
    from agent_comms.execution.native_input_proof import NativeContextProof
    from agent_comms.execution.native_input_record import TriageNativeExecution
    from agent_comms.execution.native_runtime_input import NativeRuntimeInput
    from agent_comms.execution.private_send_stage import TriageNativeSend
    from agent_comms.execution.selected.participant import SelectedParticipant
    from agent_comms.execution.selected.session import SelectedSession
    from agent_comms.private_send_claim import PrivateSendClaim


class InvalidTriageDecision(ValueError):
    """A model result violates its contract; participant identity is unchanged."""


class SelectedTriage(DeclaredFamily, affix="SelectedTriage"):
    family_discriminator = "decision"

    @classmethod
    def output_values(cls) -> dict[str, str]:
        return dict(discriminator=cls.family_discriminator, names=json.dumps(cls.names()))

    @classmethod
    def parse(cls, text: str) -> SelectedTriage:
        if not 0 < len(text.encode("utf-8")) <= 256:
            raise InvalidTriageDecision("triage response is not bounded")
        try:
            return FieldCodec.decode(cls, FieldCodec.parse(text))
        except MalformedRecord as error:
            raise InvalidTriageDecision("triage response is not an unambiguous decision") from error

    @classmethod
    def recognizes(cls, text: str) -> bool:
        """Whether this text decodes as a bounded verdict from this family."""
        try:
            cls.parse(text)
        except InvalidTriageDecision:
            return False
        return True

    @classmethod
    @abstractmethod
    def proves_source(cls, evidence: tuple[HistoricalNativeInput, ...]) -> bool: ...

    @abstractmethod
    def settle(self, store: Coordination, db: Connection, current: WakeAssignment) -> None: ...

    @abstractmethod
    async def continue_turn(
        self, participant: SelectedParticipant, session: SelectedSession, input_id: InputId,
        execution: SelectedExecution, settled: tuple[WakeAssignment, ...],
    ) -> CoordinatedTurn: ...


@dataclass(frozen=True)
class IgnoreSelectedTriage(SelectedTriage, declared_name="IGNORE"):
    @classmethod
    def proves_source(cls, evidence: tuple[HistoricalNativeInput, ...]) -> bool:
        return True

    def settle(self, store: Coordination, db: Connection, current: WakeAssignment) -> None:
        # Both declared edges stay in the native proof's one transaction. Never
        # publish a retryable TRIAGE_PENDING state after the input has been sent.
        now = store.session.now(current.updated_at_ms)
        WakeAssignment.update(
            db,
            where="assignment_id=?",
            parameters=(current.assignment_id,),
            lifecycle=TriagePendingAssignment(),
            revision=current.revision + 1,
            updated_at_ms=now,
        )
        WakeAssignment.update(
            db,
            where="assignment_id=?",
            parameters=(current.assignment_id,),
            lifecycle=IgnoredAssignment(),
            revision=current.revision + 2,
            updated_at_ms=store.session.now(now),
        )

    async def continue_turn(
        self, participant: SelectedParticipant, session: SelectedSession, input_id: InputId,
        execution: SelectedExecution, settled: tuple[WakeAssignment, ...],
    ) -> CoordinatedTurn:
        await Coordination.run_worker(participant.consume_reply_wait)
        return await CoordinatedTurn.capture(participant, input_id, IgnoredAssignment)


@dataclass(frozen=True)
class FullSelectedTriage(SelectedTriage, declared_name="FULL"):
    @classmethod
    def proves_source(cls, evidence: tuple[HistoricalNativeInput, ...]) -> bool:
        return any(proof.execution.proves_full_source(proof) for proof in evidence)

    def settle(self, store: Coordination, db: Connection, current: WakeAssignment) -> None:
        # Deferred until the execution owner atomically engages this exact claim.
        pass

    async def continue_turn(
        self, participant: SelectedParticipant, session: SelectedSession, input_id: InputId,
        execution: SelectedExecution, settled: tuple[WakeAssignment, ...],
    ) -> CoordinatedTurn:
        await participant.require_current()
        created = participant.store.create_after_triage(
            participant.batch.execution_id, WireOrigin,
            participant.lookup,
            execution.max_attempts,
            sources=participant.batch.sources, settled=settled,
        ).value
        return await execution.full(participant, session, created)


class SelectedTriageOutcome(DeclaredFamily, affix="TriageOutcome"):
    """One decoded model result owns settlement and continued inbox behavior."""

    @classmethod
    def acquire(cls, text: str) -> SelectedTriageOutcome:
        try:
            return DecidedTriageOutcome(SelectedTriage.parse(text))
        except InvalidTriageDecision as error:
            return RejectedTriageOutcome(error, text)

    @abstractmethod
    def settle(self, participant: SelectedParticipant, stage: TriageNativeSend, claim_scope: PrivateSendClaim, context: NativeContextProof) -> tuple[WakeAssignment, ...]: ...

    def close_ask(self, participant: SelectedParticipant, stage: TriageNativeSend, claim_scope: PrivateSendClaim, context: NativeContextProof) -> tuple[WakeAssignment, ...]:
        """End the first ask inside its own failure boundary; a readable decision settles."""
        return self.settle(participant, stage, claim_scope, context)

    async def asked_again[AnswerT](
        self, answer: AnswerT, reask: Callable[[RejectedTriageOutcome], Awaitable[AnswerT]],
    ) -> AnswerT:
        """Only an unreadable decision earns a second ask; every other answer stands."""
        return answer

    @abstractmethod
    async def continue_turn(
        self, participant: SelectedParticipant, session: SelectedSession, input_id: InputId,
        execution: SelectedExecution, settled: tuple[WakeAssignment, ...],
    ) -> CoordinatedTurn | None: ...


@dataclass(frozen=True)
class DecidedTriageOutcome(SelectedTriageOutcome):
    decision: SelectedTriage

    def settle(self, participant: SelectedParticipant, stage: TriageNativeSend, claim_scope: PrivateSendClaim, context: NativeContextProof) -> tuple[WakeAssignment, ...]:
        return stage.commit(participant.store, participant.identity, claim_scope.input_id,
                            claim_scope.token_digest, context, self.decision)

    async def continue_turn(
        self, participant: SelectedParticipant, session: SelectedSession, input_id: InputId,
        execution: SelectedExecution, settled: tuple[WakeAssignment, ...],
    ) -> CoordinatedTurn | None:
        return await self.decision.continue_turn(participant, session, input_id, execution, settled)


@dataclass(frozen=True)
class RejectedTriageOutcome(SelectedTriageOutcome):
    """One unparseable decision, keeping the answer it received and what was accepted.

    The raw text is retained so the terminal states what actually arrived. Without it
    the only way to learn what the model sent is to go looking in the session file."""

    error: InvalidTriageDecision
    received: str

    def reason(self) -> str:
        """The refusal and the answer it refused, bounded for one published line."""
        accepted = ", ".join(SelectedTriage.names())
        received = " ".join(self.received.split())[:200]
        return (f"The selected model returned an invalid triage decision ({self.error}). "
                f"Received: {received!r}. Reply with exactly one decision: {accepted}.")

    def settle(self, participant: SelectedParticipant, stage: TriageNativeSend, claim_scope: PrivateSendClaim, context: NativeContextProof) -> tuple[WakeAssignment, ...]:
        return stage.fail_decision(participant.store, participant.identity,
                                   claim_scope.input_id, claim_scope.token_digest, context)

    def close_ask(self, participant: SelectedParticipant, stage: TriageNativeSend, claim_scope: PrivateSendClaim, context: NativeContextProof) -> tuple[WakeAssignment, ...]:
        """An unreadable decision costs the turn, not the claim: close the answer and return
        the claim to triage-pending, so one replacement may be asked."""
        stage.burn_turn(participant.store, participant.identity, claim_scope.input_id,
                        claim_scope.token_digest, context)
        return ()

    async def asked_again[AnswerT](
        self, answer: AnswerT, reask: Callable[[RejectedTriageOutcome], Awaitable[AnswerT]],
    ) -> AnswerT:
        return await reask(self)

    async def continue_turn(
        self, participant: SelectedParticipant, session: SelectedSession, input_id: InputId,
        execution: SelectedExecution, settled: tuple[WakeAssignment, ...],
    ) -> CoordinatedTurn | None:
        from agent_comms.execution.selected.result import (
            CoordinatedTurn,
            publish_native_failure,
        )

        sent = await Coordination.run_worker(partial(
            publish_native_failure, participant, input_id,
            self.reason(),
            source_error=self.error,
        ))

        return await CoordinatedTurn.capture(
            participant, input_id, FailedAssignment,
            notices=sent.notices)


class TriageDecisionRecord(DeclaredFamily, JsonShapeFamily, affix="TriageDecisionRecord"):
    """Decode the original nullable SQL scalar once, without inventing a verdict."""

    decided: ClassVar[bool] = True

    @abstractmethod
    def historical_proof(self, record: NativeRuntimeInput, lifecycle: AssignmentState, execution: TriageNativeExecution, **source) -> HistoricalNativeInput: ...

    def require_undecided(self) -> None:
        raise MalformedRecord("A full native input records no triage verdict")


@dataclass(frozen=True)
class TriageDecisionText(TriageDecisionRecord, JsonShapeMember):
    value: str

    @classmethod
    def from_json_value(cls, value: str) -> DecidedTriageRecord:
        return DecidedTriageRecord(RecordedTriageText.from_text(value))

    def historical_proof(self, record: NativeRuntimeInput, lifecycle: AssignmentState, execution: TriageNativeExecution, **source) -> HistoricalNativeInput:
        raise TypeError("Triage decision text must be decoded at ingress")


@dataclass(frozen=True)
class DecidedTriageRecord(TriageDecisionRecord):
    decision: type[SelectedTriage]

    def to_wire(self) -> str:
        return RecordedTriageText.encode(self.decision)

    def historical_proof(self, record: NativeRuntimeInput, lifecycle: AssignmentState, execution: TriageNativeExecution, **source) -> HistoricalNativeInput:
        from agent_comms.execution.historical_native_inputs import TriageHistoricalNativeInput

        return TriageHistoricalNativeInput(execution=execution, decision=self.decision, **source)


@dataclass(frozen=True)
class AbsentTriageDecisionRecord(TriageDecisionRecord, JsonShapeMember):
    value: None = None
    decided = False

    def require_undecided(self) -> None:
        pass

    def historical_proof(self, record: NativeRuntimeInput, lifecycle: AssignmentState, execution: TriageNativeExecution, **source) -> HistoricalNativeInput:
        return lifecycle.rejected_triage_history(execution, **source)


class RecordedTriageText(TextRepresentation[type[SelectedTriage]]):
    """The original native SQL decision uses lowercase declared triage names."""

    @classmethod
    def encode(cls, value) -> str:
        return FieldCodec.encode(value, type[SelectedTriage]).lower()

    @classmethod
    def from_text(cls, value: str) -> type[SelectedTriage]:
        decision = FieldCodec.decode(type[SelectedTriage], value.upper())
        if value != decision.declared_name.lower():
            raise ValueError("Recorded triage requires its exact lowercase SQL spelling")
        return decision

    @classmethod
    def schema(cls) -> dict[str, str | list[str]]:
        return {
            "type": "string",
            "enum": [member.declared_name.lower() for member in SelectedTriage.members_with(SelectedTriage)],
        }

    @classmethod
    def sql_constraint(cls, column: str) -> str:
        names = tuple(member.declared_name.lower() for member in SelectedTriage.members_with(SelectedTriage))
        return TextStorage.constraints(column, Literal[names])[0]
