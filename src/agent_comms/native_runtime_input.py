"""Declared native input and cursor rows, never reconstructed native proof.

These original coordination.sqlite3 rows retain irreplaceable reservation,
send-admission and live-context authority. Installation never resets them.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Annotated, Literal

from agent_comms.coordination_tables.executions import ExecutionRecord
from agent_comms.coordination_tables.participants import Participants
from agent_comms.private_runtime_schema import PrivateRuntimeSchema

from .coordination_errors import StaleFence, IdentityConflict
from .native_admission_epoch import NativeAdmissionEpoch, UnrecordedNativeAdmission
from .native_input_record import NativeInputRecord, NativeInputContext, NativeInputExecution
from .selected_triage import SelectedTriage, RecordedTriageText
from .typed_table import Column, IntegerStorage, TypedRow, TypedTable

if TYPE_CHECKING:
    from .native_pi import NativeContextProof
    from .native_session_reopen import NativeSessionIdentity


class NativeRuntimeTable:
    """Capability identifying the native-runtime schema's declared tables."""


@dataclass(frozen=True)
class NativeRuntimeSchemaMeta(NativeRuntimeTable, TypedTable, PrivateRuntimeSchema):
    @classmethod
    def install(cls, store) -> None:
        from .coordinated_runtime_schema import install_native_runtime_schema

        install_native_runtime_schema(store)

    singleton: Literal[1] = field(metadata={"sql": Column(primary_key=True, check="singleton=1")})
    version: Literal[6] = field(default=6, kw_only=True)
    ddl_digest: str = field(metadata={"sql": Column(check="length(ddl_digest)=64")})

    @classmethod
    def create_schema(cls, db: sqlite3.Connection) -> None:
        """Create this owner's current declarations on an explicitly fresh store."""
        from .coordinated_runtime_schema import _schema, _digest

        schema = _schema()
        for statement in schema.values():
            db.execute(statement)
        cls(singleton=1, ddl_digest=_digest(schema)).insert(db)

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
class PublishedReplyRevision(TypedRow):
    """Derived identity of this source's immutable published native relations."""

    through_seq: int
    inputs: int


@dataclass(frozen=True)
class NativeRuntimeInput(NativeInputRecord, NativeInputContext, NativeRuntimeTable, TypedTable):
    @classmethod
    def source_membership_sql(cls) -> str:
        return " UNION ALL ".join(
            member.source_membership_sql() for member in NativeInputExecution.members_with(NativeInputExecution)
        )

    def __post_init__(self):
        self.execution
        # The selected identity and the later context receipt are separate
        # acquisition phases of this ORIGINAL row, never two source selectors.
        if self.session_id is None:
            from .field_codec import FieldCodec

            FieldCodec.decode(type(None), self.session_file)
        else:
            self.require_session_identity()
        self.reference

    @property
    def context_anchor(self) -> str | None:
        return self.session_entry_id

    def require_session_identity(self) -> NativeSessionIdentity:
        """Recover the source originally attested before this row's raw writer."""
        from .native_session_reopen import NativeSessionIdentity

        try:
            return NativeSessionIdentity(self.session_id, self.session_file)
        except (TypeError, ValueError) as error:
            raise IdentityConflict("Native input has no recorded selected session") from error

    def require_context_proof(self) -> NativeContextProof:
        """Recover the original committed receipt, never manufacture it from a path."""
        from .native_pi import NativeContextProof

        reference = self.reference.require_recorded()
        return NativeContextProof(
            reference.input_id, reference.session_id, self.session_entry_id,
            reference.request_generation, self.llm_context_digest,
            self.require_session_identity().path,
        )

    @property
    def execution(self) -> NativeInputExecution:
        try:
            return self.stage.from_columns(self.execution_id, self.attempt_ordinal)
        except (TypeError, ValueError) as error:
            raise IdentityConflict("Native input execution columns conflict with their stage") from error

    @classmethod
    @contextmanager
    def _publication_read(cls, root):
        from .coordination_database import CoordinationStore
        from .coordinated_runtime_schema import assert_native_runtime_schema
        from .coordination_response import _assert_response_schema
        from .errors import RelationViolationError
        from .recovery_projection import _preflight

        database = root / "coordination.sqlite3"
        failure = _preflight(database)
        if failure == "missing":
            yield None
            return
        if failure:
            raise RelationViolationError("Native reply source has an invalid coordinator")
        with CoordinationStore.observing(database, lock_timeout=0.05) as db:
            assert_native_runtime_schema(db)
            _assert_response_schema(db)
            yield db

    @classmethod
    def for_native_user(cls, db, reader, user, *, session_id):
        """The original admitted native file/input owns stage and publication.

        Viewer names and current registry owners cannot classify inherited or
        saved native history. A recorded receipt additionally seals the exact
        original user entry; a precommit admitted row still owns its stage.
        """
        rows = cls.select(
            db, where="input_id=? AND session_file=? AND session_id=?",
            parameters=(user.input_id, str(reader.path), session_id),
        )
        for row in rows:
            user.require_tracked_user()
            if row.reference.recorded and row.session_entry_id != user.id:
                raise IdentityConflict("Native transcript input conflicts with its original entry")
        return rows

    @classmethod
    def transcript_projection(cls, db, reader, record, owner_lookup, *, user, session_id):
        """Borrow original stage/replies; defer rendering until SQL closes.

        No current lifecycle witness is retained. These original identities are
        frozen; TranscriptRead's original publication revision fences appends.
        """
        entry = record.entry
        originals, publications = (), ()
        if user is not None and user.input_id is not None and db is not None:
            originals = cls.for_native_user(db, reader, user, session_id=session_id)
            if entry.final_reply:
                for original in originals:
                    publications = original.published_replies(db, user, owner_lookup)
        for original in originals:
            return partial(original.execution.transcript_events, entry, user=user), publications
        # Untracked/detached history is not classified by text or native role.
        return entry.events, publications

    @classmethod
    def publication_revision(cls, root, reader, owner_lookup):
        """Read only this native source's committed original reply relations.

        Published receipts and their input/attempt links are immutable. Their
        maximum sequence and cardinality change on publication or proof reset;
        unrelated owner activity cannot revoke a prepared page. No counter or
        projection is persisted, and the read closes before presentation work.
        """
        from .coordination_tables.executions import ExecutionRecord
        from .coordination_tables.responses import ResponseObligation

        with cls._publication_read(root) as db:
            if db is None:
                return PublishedReplyRevision(0, 0)
            return PublishedReplyRevision.read(
                db.execute(
                    "SELECT COALESCE(MAX(o.receipt_seq),0) AS through_seq, COUNT(DISTINCT n.input_id) AS inputs "
                    f"FROM {cls.declared_name} n JOIN {ExecutionRecord.declared_name} e "
                    "ON e.execution_id=n.execution_id AND e.owner_lookup=n.owner_lookup "
                    "AND e.current_attempt_ordinal=n.attempt_ordinal "
                    f"JOIN {ResponseObligation.declared_name} o ON o.execution_id=n.execution_id "
                    "WHERE n.owner_lookup=? AND n.session_file=? "
                    "AND n.session_id=? AND n.session_entry_id IS NOT NULL "
                    "AND o.receipt_seq IS NOT NULL",
                    (owner_lookup, str(reader.path), reader.session_id),
                )
            )[0]

    def published_replies(self, db, user, owner_lookup):
        """Only this original receipt can lend its execution publication."""
        if (self.owner_lookup, self.session_entry_id) != (owner_lookup, user.id):
            return ()
        return self.execution.published_replies(db, owner_lookup)

    input_id: str = field(
        metadata={
            "sql": Column(
                primary_key=True, check="length(input_id)=32 AND input_id NOT GLOB '*[^0-9a-f]*'"
            )
        }
    )
    stage: type[NativeInputExecution] = field(metadata={"sql": Column(check=NativeInputExecution.sql_constraint("stage"))})
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
    sent_owner_admission_generation: NativeAdmissionEpoch = field(
        default_factory=UnrecordedNativeAdmission,
        metadata={"sql": Column(check="sent_owner_admission_generation>0",
                                 storage=IntegerStorage, nullable=True)},
    )
    session_id: str | None = None
    session_file: str | None = None
    session_entry_id: str | None = field(default=None, metadata={"native_context": str})
    request_generation: int | None = field(default=None, metadata={"native_context": int})
    llm_context_digest: str | None = field(default=None, metadata={"native_context": str})
    verdict: Annotated[type[SelectedTriage] | None, RecordedTriageText] = field(
        default=None, metadata={"sql": Column(check=RecordedTriageText.sql_constraint("verdict"))}
    )

    without_rowid = True
    unique = (("execution_id", "attempt_ordinal"),)
    checks = (
        "(stage='triage' AND execution_id IS NULL AND attempt_ordinal IS NULL) OR "
        "(stage='full' AND execution_id IS NOT NULL AND attempt_ordinal>0)",
        "(session_id IS NULL AND session_file IS NULL) OR "
        "(session_id IS NOT NULL AND length(session_id)>0 AND session_file IS NOT NULL "
        "AND length(session_file)>0 AND sent_owner_admission_generation IS NOT NULL)",
        "(session_entry_id IS NULL AND request_generation IS NULL AND llm_context_digest IS NULL) OR "
        "(session_entry_id IS NOT NULL AND length(session_entry_id)>0 AND request_generation>0 "
        "AND length(llm_context_digest)=64 AND session_id IS NOT NULL)",
        "stage='triage' OR verdict IS NULL",
    )

    def require_unproven(self, token_digest: str) -> None:
        """Only the original reserved capability can acquire its first proof."""
        if self.owner_token_digest != token_digest or self.reference.recorded:
            raise StaleFence("native proof belongs to a different or already settled dispatch")

    def commit_context(
        self,
        db: sqlite3.Connection,
        context: NativeContextProof,
        *,
        verdict: type[SelectedTriage] | None = None,
    ) -> None:
        """Commit the context receipt once against the original selected session."""
        if context.input_id != self.input_id:
            raise StaleFence("native context belongs to another reserved input")
        self.require_session_identity().require_context(context)
        updated = self.update(
            db,
            where="input_id=? AND session_entry_id IS NULL",
            parameters=(self.input_id,),
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
                OR (OLD.session_id IS NOT NULL AND
                    (NEW.session_id IS NOT OLD.session_id OR NEW.session_file IS NOT OLD.session_file))
                OR OLD.session_entry_id IS NOT NULL
                OR (NEW.session_id IS NULL AND NEW.sent_owner_admission_generation IS NULL)
            BEGIN SELECT RAISE(ABORT,'native runtime input identity is frozen'); END""",
            f"{name}_delete_guard": f"CREATE TRIGGER {name}_delete_guard BEFORE DELETE ON {name} "
            "BEGIN SELECT RAISE(ABORT,'native runtime input cannot be deleted'); END",
        }


@dataclass(frozen=True)
class CurrentNativeCursor(NativeInputContext, NativeRuntimeTable, TypedTable):
    @property
    def context_anchor(self) -> str | None:
        return self.session_id

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
    @property
    def owner_identity(self):
        from .coordination_tables.participants import OwnerGenerations

        return OwnerGenerations(owner_lookup=self.recipient_lookup, owner_thread=self.owner_thread, generation=self.owner_generation)

    covered_seq: int = field(metadata={"sql": Column(check="covered_seq>=0")})
    injected_seq: int = field(
        metadata={"sql": Column(check="injected_seq>=0 AND injected_seq<=covered_seq")}
    )
    input_id: str | None = field(
        metadata={"sql": Column(references=(NativeRuntimeInput, "input_id")), "native_context": str}
    )
    stage: type[NativeInputExecution] | None = field(metadata={"sql": Column(check=NativeInputExecution.sql_constraint("stage")), "native_context": type[NativeInputExecution]})
    session_id: str | None = field(metadata={"native_context": str})
    request_generation: int | None = field(metadata={"native_context": int})

    without_rowid = True
    checks = (
        "(injected_seq=0 AND input_id IS NULL AND stage IS NULL "
        "AND session_id IS NULL AND request_generation IS NULL) OR "
        "(injected_seq>0 AND input_id IS NOT NULL "
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
