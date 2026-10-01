"""One bounded decoded decision owns its claim settlement and continuation."""

from __future__ import annotations

import json
from abc import abstractmethod
from dataclasses import dataclass
from typing import Literal

from .assignment_states import IgnoredAssignment, TriagePendingAssignment
from .coordination_errors import IdentityConflict
from .coordination_tables.assignments import WakeAssignment
from .declared_family import DeclaredFamily
from .field_codec import FieldCodec, TextRepresentation
from .typed_table import TextStorage
from .pi_rpc import unique_fields


class SelectedTriage(DeclaredFamily, affix="SelectedTriage"):
    family_discriminator = "decision"

    @classmethod
    def parse(cls, text: str) -> SelectedTriage:
        if not 0 < len(text.encode("utf-8")) <= 256:
            raise IdentityConflict("triage response is not bounded")
        try:
            return FieldCodec.decode(cls, json.loads(text, object_pairs_hook=unique_fields))
        except (ValueError, TypeError) as error:
            raise IdentityConflict("triage response is not an unambiguous decision") from error

    @classmethod
    @abstractmethod
    def proves_source(cls, evidence) -> bool: ...

    @abstractmethod
    def settle(self, store, db, current: WakeAssignment) -> None: ...

    @abstractmethod
    def continue_turn(self, participant, session, input_id): ...


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

    def continue_turn(self, participant, session, input_id):
        from .selected_result import CoordinatedTurn

        participant.consume_reply_wait()
        return CoordinatedTurn.ignored(participant, session, input_id)


@dataclass(frozen=True)
class FullSelectedTriage(SelectedTriage, declared_name="FULL"):
    @classmethod
    def proves_source(cls, evidence) -> bool:
        return any(proof.execution.proves_full_source(proof) for proof in evidence)

    def settle(self, store, db, current):
        # Deferred until the execution owner atomically engages this exact claim.
        pass

    def continue_turn(self, participant, session, input_id):
        return None


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
