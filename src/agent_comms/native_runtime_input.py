"""Declared native input and cursor rows, never reconstructed native proof.

These are runtime tables in coordination.sqlite3. Installation resets their
store at a quiet migration; admission remains fenced by the root authority.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from agent_comms.coordination_tables.assignments import WakeAssignment
from agent_comms.coordination_tables.executions import ExecutionRecord
from agent_comms.coordination_tables.participants import Participants
from agent_comms.private_runtime_schema import PrivateRuntimeSchema

from .coordination_errors import StaleFence
from .native_input_record import NativeInputRecord
from .typed_table import Column, TypedTable

if TYPE_CHECKING:
    from .native_pi import NativeContextProof


class NativeRuntimeTable:
    """Capability identifying the native-runtime schema's declared tables."""


@dataclass(frozen=True)
class NativeRuntimeSchemaMeta(NativeRuntimeTable, TypedTable, PrivateRuntimeSchema):
    @classmethod
    def install(cls, store) -> None:
        from .coordinated_runtime_schema import install_native_runtime_schema

        install_native_runtime_schema(store)

    singleton: Literal[1] = field(metadata={"sql": Column(primary_key=True, check="singleton=1")})
    version: Literal[4]
    ddl_digest: str = field(metadata={"sql": Column(check="length(ddl_digest)=64")})

    @classmethod
    def triggers(cls) -> dict[str, str]:
        return {
            f"{cls.declared_name}_{operation.lower()}_guard": f"CREATE TRIGGER {cls.declared_name}_"
            f"{operation.lower()}_guard "
            f"BEFORE {operation} ON {cls.declared_name} "
            "BEGIN SELECT RAISE(ABORT,'native runtime schema is frozen'); END"
            for operation in ("UPDATE", "DELETE")
        }


@dataclass(frozen=True)
class NativeRuntimeInput(NativeInputRecord, NativeRuntimeTable, TypedTable):
    input_id: str = field(
        metadata={
            "sql": Column(
                primary_key=True, check="length(input_id)=32 AND input_id NOT GLOB '*[^0-9a-f]*'"
            )
        }
    )
    stage: Literal["triage", "full"]
    assignment_id: str = field(
        metadata={"sql": Column(references=(WakeAssignment, "assignment_id"))}
    )
    execution_id: str | None = field(
        metadata={"sql": Column(references=(ExecutionRecord, "execution_id"))}
    )
    attempt_ordinal: int | None
    owner_lookup: str = field(
        metadata={"sql": Column(references=(Participants, "participant_lookup"))}
    )
    owner_thread: str
    owner_generation: int = field(metadata={"sql": Column(check="owner_generation>0")})
    owner_token_digest: str = field(metadata={"sql": Column(check="length(owner_token_digest)=64")})
    sent_owner_admission_generation: int | None = field(
        default=None, metadata={"sql": Column(check="sent_owner_admission_generation>0")}
    )
    session_id: str | None = None
    session_file: str | None = None
    session_entry_id: str | None = None
    request_generation: int | None = None
    llm_context_digest: str | None = None
    verdict: Literal["ignore", "full"] | None = None

    without_rowid = True
    unique = (("stage", "assignment_id"), ("execution_id", "attempt_ordinal"))
    checks = (
        "(stage='triage' AND execution_id IS NULL AND attempt_ordinal IS NULL) OR "
        "(stage='full' AND execution_id IS NOT NULL AND attempt_ordinal>0)",
        "(session_id IS NULL AND session_file IS NULL AND session_entry_id IS NULL "
        "AND request_generation IS NULL AND llm_context_digest IS NULL) OR "
        "(session_id IS NOT NULL AND length(session_id)>0 AND session_file IS NOT NULL "
        "AND length(session_file)>0 AND session_entry_id IS NOT NULL AND "
        "length(session_entry_id)>0 "
        "AND request_generation>0 AND length(llm_context_digest)=64)",
        "stage='triage' OR verdict IS NULL",
    )

    def require_unproven(self, token_digest: str) -> None:
        """Only the original reserved capability can acquire its first proof."""
        if self.owner_token_digest != token_digest or self.session_id is not None:
            raise StaleFence("native proof belongs to a different or already settled dispatch")

    def commit_context(
        self,
        db: sqlite3.Connection,
        context: NativeContextProof,
        *,
        verdict: Literal["ignore", "full"] | None = None,
    ) -> None:
        """Commit the five context facts together, once, after live verification."""
        if context.input_id != self.input_id:
            raise StaleFence("native context belongs to another reserved input")
        updated = self.update(
            db,
            where="input_id=? AND session_id IS NULL",
            parameters=(self.input_id,),
            session_id=context.session_id,
            session_file=str(context.session_file),
            session_entry_id=context.session_entry_id,
            request_generation=context.request_generation,
            llm_context_digest=context.llm_context_digest,
            verdict=verdict,
        )
        if updated.rowcount != 1:
            raise StaleFence("native proof was previously committed")

    @classmethod
    def triggers(cls) -> dict[str, str]:
        name = cls.declared_name
        return {
            f"{name}_identity_guard": f"""CREATE TRIGGER {name}_identity_guard
            BEFORE UPDATE ON {name}
            WHEN NEW.input_id IS NOT OLD.input_id OR NEW.stage IS NOT OLD.stage
                OR NEW.assignment_id IS NOT OLD.assignment_id
                OR NEW.execution_id IS NOT OLD.execution_id
                OR NEW.attempt_ordinal IS NOT OLD.attempt_ordinal
                OR NEW.owner_lookup IS NOT OLD.owner_lookup
                OR NEW.owner_thread IS NOT OLD.owner_thread
                OR NEW.owner_generation IS NOT OLD.owner_generation
                OR NEW.owner_token_digest IS NOT OLD.owner_token_digest
                OR (OLD.sent_owner_admission_generation
                    IS NOT NULL
                    AND NEW.sent_owner_admission_generation
                        IS NOT OLD.sent_owner_admission_generation)
                OR (NEW.sent_owner_admission_generation IS NULL AND NEW.session_id IS NOT NULL)
                OR OLD.session_id IS NOT NULL
                OR (NEW.session_id IS NULL AND NEW.sent_owner_admission_generation IS NULL)
            BEGIN SELECT RAISE(ABORT,'native runtime input identity is frozen'); END""",
            f"{name}_delete_guard": f"CREATE TRIGGER {name}_delete_guard BEFORE DELETE ON {name} "
            "BEGIN SELECT RAISE(ABORT,'native runtime input cannot be deleted'); END",
        }


@dataclass(frozen=True)
class CurrentNativeCursor(NativeRuntimeTable, TypedTable):
    wire_root_id: str = field(
        metadata={
            "sql": Column(
                primary_key=True,
                check="length(wire_root_id)=32 AND wire_root_id NOT GLOB '*[^0-9a-f]*'",
            )
        }
    )
    recipient_lookup: str = field(
        metadata={"sql": Column(primary_key=True, references=(Participants, "participant_lookup"))}
    )
    owner_thread: str = field(metadata={"sql": Column(check="owner_thread<>''")})
    owner_generation: int = field(
        metadata={"sql": Column(primary_key=True, check="owner_generation>0")}
    )
    owner_admission_generation: int = field(
        metadata={"sql": Column(primary_key=True, check="owner_admission_generation>0")}
    )
    covered_seq: int = field(metadata={"sql": Column(check="covered_seq>=0")})
    injected_seq: int = field(
        metadata={"sql": Column(check="injected_seq>=0 AND injected_seq<=covered_seq")}
    )
    input_id: str | None = field(
        metadata={"sql": Column(references=(NativeRuntimeInput, "input_id"))}
    )
    assignment_id: str | None
    stage: Literal["triage", "full"] | None
    session_id: str | None
    request_generation: int | None

    without_rowid = True
    checks = (
        "(injected_seq=0 AND input_id IS NULL AND assignment_id IS NULL AND stage IS NULL "
        "AND session_id IS NULL AND request_generation IS NULL) OR "
        "(injected_seq>0 AND input_id IS NOT NULL AND assignment_id IS NOT NULL "
        "AND stage IS NOT NULL "
        "AND session_id IS NOT NULL AND request_generation>0)",
    )

    @classmethod
    def triggers(cls) -> dict[str, str]:
        name = cls.declared_name
        return {
            f"{name}_update_guard": f"""CREATE TRIGGER {name}_update_guard BEFORE UPDATE ON {name}
            WHEN NEW.wire_root_id IS NOT OLD.wire_root_id
                OR NEW.recipient_lookup IS NOT OLD.recipient_lookup
                OR NEW.owner_thread IS NOT OLD.owner_thread
                OR NEW.owner_generation IS NOT OLD.owner_generation
                OR NEW.owner_admission_generation IS NOT OLD.owner_admission_generation
                OR NEW.covered_seq < OLD.covered_seq OR NEW.injected_seq < OLD.injected_seq
                OR (NEW.injected_seq = OLD.injected_seq AND NEW.input_id IS NOT OLD.input_id)
            BEGIN SELECT RAISE(ABORT,'native source cursor cannot regress'); END""",
            f"{name}_delete_guard": f"CREATE TRIGGER {name}_delete_guard BEFORE DELETE ON {name} "
            "BEGIN SELECT RAISE(ABORT,'native source cursor cannot be deleted'); END",
        }
