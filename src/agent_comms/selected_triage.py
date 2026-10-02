"""One bounded decoded decision owns its claim settlement and continuation."""

from __future__ import annotations

import json
from abc import abstractmethod
from dataclasses import dataclass
from typing import Literal

from .assignment_states import FailedAssignment, IgnoredAssignment, TriagePendingAssignment
from .coordination_tables.assignments import WakeAssignment
from .declared_family import DeclaredFamily
from .field_codec import FieldCodec, JsonShapeFamily, JsonShapeMember, TextRepresentation
from .typed_table import TextStorage
from .pi_rpc import unique_fields


class InvalidTriageDecision(ValueError):
    """A model result violates its contract; participant identity is unchanged."""


class SelectedTriage(DeclaredFamily, affix="SelectedTriage"):
    family_discriminator = "decision"

    @classmethod
    def output_instruction(cls) -> str:
        names = [member.declared_name for member in cls.members_with(cls)]
        return (
            f"Output ONLY a JSON object with one key {cls.family_discriminator} and "
            f"one of these declared values: {json.dumps(names)}. "
            "No tools, extra keys, prose or markdown. "
            "Original messages are the selected JSON above.\n"
        )

    @classmethod
    def parse(cls, text: str) -> SelectedTriage:
        if not 0 < len(text.encode("utf-8")) <= 256:
            raise InvalidTriageDecision("triage response is not bounded")
        try:
            return FieldCodec.decode(cls, json.loads(text, object_pairs_hook=unique_fields))
        except (ValueError, TypeError) as error:
            raise InvalidTriageDecision("triage response is not an unambiguous decision") from error

    @classmethod
    @abstractmethod
    def proves_source(cls, evidence) -> bool: ...

    @abstractmethod
    def settle(self, store, db, current: WakeAssignment) -> None: ...

    @abstractmethod
    async def continue_turn(self, participant, session, input_id, execution, settled): ...


@dataclass(frozen=True)
class IgnoreSelectedTriage(SelectedTriage, declared_name="IGNORE"):
    @classmethod
    def proves_source(cls, evidence) -> bool:
        return True

    def settle(self, store, db, current):
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

    async def continue_turn(self, participant, session, input_id, execution, settled):
        from .selected_result import CoordinatedTurn

        participant.consume_reply_wait()
        return await CoordinatedTurn.capture(participant, session, input_id, IgnoredAssignment)


@dataclass(frozen=True)
class FullSelectedTriage(SelectedTriage, declared_name="FULL"):
    @classmethod
    def proves_source(cls, evidence) -> bool:
        return any(proof.execution.proves_full_source(proof) for proof in evidence)

    def settle(self, store, db, current):
        # Deferred until the execution owner atomically engages this exact claim.
        pass

    async def continue_turn(self, participant, session, input_id, execution, settled):
        from .coordination_tables.executions import ExecutionOrigin
        from .selected_turn import SelectedAttempt

        participant.require_current(participant.store)
        created = participant.store.executions.create_after_triage(
            participant.batch.execution_id, ExecutionOrigin.WIRE,
            participant.lookup, participant.owner.thread.name, 1,
            sources=participant.batch.sources, settled=settled,
        ).value
        attempt = SelectedAttempt.engage(participant, created)
        return await attempt.run(execution.native_package, session,
                                 execution.action(session), execution.write_authority)


class SelectedTriageOutcome(DeclaredFamily, affix="TriageOutcome"):
    """One decoded model result owns settlement and continued inbox behavior."""

    @classmethod
    def acquire(cls, text: str) -> SelectedTriageOutcome:
        try:
            return DecidedTriageOutcome(SelectedTriage.parse(text))
        except InvalidTriageDecision as error:
            return RejectedTriageOutcome(error)

    @abstractmethod
    def settle(self, participant, stage, admission, context) -> tuple[WakeAssignment, ...]: ...

    @abstractmethod
    async def continue_turn(self, participant, session, input_id, execution, settled): ...


@dataclass(frozen=True)
class DecidedTriageOutcome(SelectedTriageOutcome):
    decision: SelectedTriage

    def settle(self, participant, stage, admission, context):
        return stage.commit(participant.store, participant.identity, admission.input_id,
                            admission.token_digest, context, self.decision)

    async def continue_turn(self, participant, session, input_id, execution, settled):
        return await self.decision.continue_turn(participant, session, input_id, execution, settled)


@dataclass(frozen=True)
class RejectedTriageOutcome(SelectedTriageOutcome):
    error: InvalidTriageDecision

    def settle(self, participant, stage, admission, context):
        from .selected_result import publish_native_failure

        settled = stage.reject(participant.store, participant.identity, admission.input_id,
                               admission.token_digest, context)
        publish_native_failure(participant, admission.input_id,
                               "The selected model returned an invalid triage decision.",
                               source_error=self.error)
        return settled

    async def continue_turn(self, participant, session, input_id, execution, settled):
        from .selected_result import CoordinatedTurn

        return await CoordinatedTurn.capture(participant, session, input_id, FailedAssignment)


class TriageDecisionRecord(DeclaredFamily, JsonShapeFamily, affix="TriageDecisionRecord"):
    """Decode the original nullable SQL scalar once, without inventing a verdict."""

    @abstractmethod
    def historical_proof(self, record, lifecycle, execution, **source): ...


@dataclass(frozen=True)
class TriageDecisionText(TriageDecisionRecord, JsonShapeMember):
    value: str

    @classmethod
    def from_json_value(cls, value):
        return DecidedTriageRecord(FieldCodec.decode(type[SelectedTriage], value))

    def historical_proof(self, record, lifecycle, execution, **source):
        raise TypeError("Triage decision text must be decoded at ingress")


@dataclass(frozen=True)
class DecidedTriageRecord(TriageDecisionRecord):
    decision: type[SelectedTriage]

    def historical_proof(self, record, lifecycle, execution, **source):
        from .historical_native_inputs import TriageHistoricalNativeInput

        return TriageHistoricalNativeInput(execution=execution, decision=self.decision, **source)


@dataclass(frozen=True)
class AbsentTriageDecisionRecord(TriageDecisionRecord, JsonShapeMember):
    value: None = None

    def historical_proof(self, record, lifecycle, execution, **source):
        return lifecycle.rejected_triage_history(execution, **source)


class RecordedTriageText(TextRepresentation):
    """The original native SQL decision uses lowercase declared triage names."""

    @classmethod
    def encode(cls, value):
        return FieldCodec.encode(value, type[SelectedTriage]).lower()

    @classmethod
    def from_text(cls, value: str):
        decision = FieldCodec.decode(type[SelectedTriage], value.upper())
        if value != decision.declared_name.lower():
            raise ValueError("Recorded triage requires its exact lowercase SQL spelling")
        return decision

    @classmethod
    def schema(cls):
        return {
            "type": "string",
            "enum": [member.declared_name.lower() for member in SelectedTriage.members_with(SelectedTriage)],
        }

    @classmethod
    def sql_constraint(cls, column: str) -> str:
        names = tuple(member.declared_name.lower() for member in SelectedTriage.members_with(SelectedTriage))
        return TextStorage.constraints(column, Literal[names])[0]
