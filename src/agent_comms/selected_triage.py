"""One bounded decoded decision owns its claim settlement and continuation."""

from __future__ import annotations

import json
from abc import abstractmethod
from dataclasses import dataclass

from .assignment_states import IgnoredAssignment, TriagePendingAssignment
from .coordination_errors import IdentityConflict
from .coordination_tables.assignments import WakeAssignment
from .declared_family import DeclaredFamily
from .field_codec import FieldCodec
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

    @abstractmethod
    def settle(self, store, db, current: WakeAssignment) -> None: ...

    @abstractmethod
    def continue_turn(self, participant, session, input_id): ...


@dataclass(frozen=True)
class IgnoreSelectedTriage(SelectedTriage, declared_name="IGNORE"):
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
    def settle(self, store, db, current):
        # Deferred until the execution owner atomically engages this exact claim.
        pass

    def continue_turn(self, participant, session, input_id):
        return None
