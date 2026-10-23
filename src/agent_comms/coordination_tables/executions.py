"""Execution rows own their SQL constraints and lifecycle relations."""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Annotated, ClassVar

from agent_comms.coordination_contracts import (
    MAX_IDENTIFIER_CHARS,
    require_bounded,
    require_nonempty,
    validate_execution_id,
)
from agent_comms.coordination_errors import IdentityConflict, IntegrityViolationError, StaleRevision
from agent_comms.coordination_schema import CoordinatorRow
from agent_comms.declared_family import DeclaredFamily
from agent_comms.coordination_tables.attempts import (
    APPROVED_REPLAY_FACT_MASK,
    AttemptRecord,
    ReplayFact,
)
from agent_comms.execution_states import ExecutionState
from agent_comms.field_codec import (
    MalformedRecord,
    NullMember,
    SingleFieldValue,
    SnapshotExclude,
    SubclassUnion,
    projected,
)
from agent_comms.reason_codes import ReasonCode
from agent_comms.thread_text import AbsentText, GivenText, TextChoice
from agent_comms.typed_table import (
    AccumulatingBits,
    Column,
    ColumnGuard,
    ForeignKey,
    FrozenColumn,
    Generated,
    GuardedAfterWrites,
    GuardedColumns,
    GuardedInserts,
    IncrementingColumn,
    Index,
    IntegerStorage,
    LifecycleTransitions,
    MonotoneColumns,
    NoDelete,
    NonDecreasing,
    NonIncreasing,
    OrdinalTransitions,
    TextStorage,
    sql_literal,
)


if TYPE_CHECKING:
    from agent_comms.coordination_tables.assignments import WakeAssignment
    from agent_comms.coordination_tables.responses import ResponseObligation
    from agent_comms.selected_source_batch import SelectedSource


class ExecutionProvenance(DeclaredFamily, affix="Origin"):
    """The origin owns source membership, route eligibility and retry policy."""

    @classmethod
    @abstractmethod
    def require_sources(cls, sources: tuple[SelectedSource, ...]) -> None: ...

    @classmethod
    @abstractmethod
    def validate_routes(
        cls, assignments: tuple[WakeAssignment, ...], obligations: tuple[ResponseObligation, ...],
    ) -> None: ...

    @classmethod
    @abstractmethod
    def retry_authorized(cls, obligations: tuple[ResponseObligation, ...]) -> bool: ...

    @classmethod
    @abstractmethod
    def require_obligation(cls) -> None: ...


class ClaimlessOrigin(ExecutionProvenance, declared_name=None):
    @classmethod
    def require_sources(cls, sources: tuple[SelectedSource, ...]) -> None:
        if sources:
            raise IdentityConflict("claimless execution cannot bind claims")

    @classmethod
    def validate_routes(
        cls, assignments: tuple[WakeAssignment, ...], obligations: tuple[ResponseObligation, ...],
    ) -> None:
        if assignments or obligations:
            raise IntegrityViolationError("claimless snapshots cannot have claims or obligations")

    @classmethod
    def retry_authorized(cls, obligations: tuple[ResponseObligation, ...]) -> bool:
        return True

    @classmethod
    def require_obligation(cls) -> None:
        from agent_comms.wake_candidate_index import ProjectionUnavailableError

        raise ProjectionUnavailableError("open obligation has no wire origin")


class WireOrigin(ExecutionProvenance):
    @classmethod
    def require_sources(cls, sources: tuple[SelectedSource, ...]) -> None:
        if not sources:
            raise IdentityConflict("wire execution requires ordered original sources")

    @classmethod
    def validate_routes(
        cls, assignments: tuple[WakeAssignment, ...], obligations: tuple[ResponseObligation, ...],
    ) -> None:
        if not assignments or not obligations:
            raise IntegrityViolationError("wire snapshots require original obligations")

    @classmethod
    def retry_authorized(cls, obligations: tuple[ResponseObligation, ...]) -> bool:
        return bool(obligations) and all(row.lifecycle.retryable for row in obligations)

    @classmethod
    def require_obligation(cls) -> None:
        pass

    @classmethod
    def matches_sql(cls, column: str) -> str:
        members = cls.members_with(cls)
        # Preserve the existing scalar DDL; additional sourced cases derive a set.
        if len(members) == 1:
            return f"{column} = {sql_literal(members[0])}"
        return f"{column} IN {cls.sql_choices()}"

    @classmethod
    def retry_sql(cls, execution: str) -> str:
        return (
            f"(NOT ({cls.matches_sql(execution + '.origin')}) OR (EXISTS (SELECT 1 FROM obligations o\n"
            f"       WHERE o.execution_id={execution}.execution_id) AND NOT EXISTS "
            "(SELECT 1 FROM obligations o\n"
            f"       WHERE o.execution_id={execution}.execution_id AND o.retryable != 1)))"
        )


class AcpOrigin(ClaimlessOrigin):
    pass


class GoalOrigin(ClaimlessOrigin):
    pass


class SystemOrigin(ClaimlessOrigin):
    pass


class PointerExecution(SubclassUnion, TextChoice, affix="Execution"):
    """The execution an owner's pointer names: its id, or SQL NULL while the owner is idle."""

    pointed: ClassVar[bool]


@dataclass(frozen=True, slots=True)
class PointedExecution(GivenText, PointerExecution):
    pointed: ClassVar[bool] = True
    text: str

    def __post_init__(self) -> None:
        validate_execution_id(self.text)


@dataclass(frozen=True, slots=True)
class NoPointedExecution(AbsentText, PointerExecution):
    pointed: ClassVar[bool] = False


class PointerAttempt(SubclassUnion, affix="Attempt"):
    """The attempt an owner's pointer names: its ordinal, or SQL NULL while the owner is idle."""

    pointed: ClassVar[bool]

    @abstractmethod
    def otherwise[Default](self, default: Default) -> int | Default: ...


@dataclass(frozen=True, slots=True)
class PointedAttempt(SingleFieldValue, PointerAttempt):
    pointed: ClassVar[bool] = True
    ordinal: int

    def __post_init__(self) -> None:
        if self.ordinal <= 0:
            raise ValueError("pointer ordinal must be positive")

    def otherwise[Default](self, default: Default) -> int:
        return self.ordinal


@dataclass(frozen=True, slots=True)
class NoPointedAttempt(NullMember, PointerAttempt):
    pointed: ClassVar[bool] = False

    def otherwise[Default](self, default: Default) -> Default:
        return default


@dataclass(frozen=True, slots=True)
class CurrentExecutions(
    IncrementingColumn, GuardedInserts, NoDelete, CoordinatorRow
):
    revision_column = "pointer_revision"
    insert_condition = """NOT EXISTS (SELECT 1 FROM owner_generations
        WHERE owner_lookup = NEW.owner_lookup)"""

    @property
    def unresolved(self) -> bool:
        return self.execution_id.pointed

    def require_idle(self) -> None:
        from agent_comms.coordination_errors import StaleFence

        if self.unresolved:
            raise StaleFence(
                "selected owner has an unresolved execution; new claims remain pending"
            )

    owner_lookup: Annotated[str, FrozenColumn(primary_key=True)]
    execution_id: Annotated[PointerExecution, Column(storage=TextStorage, nullable=True)]
    attempt_ordinal: Annotated[PointerAttempt, Column(storage=IntegerStorage, nullable=True)]
    pointer_revision: Annotated[int, Column(check="pointer_revision >= 0")]

    def __post_init__(self) -> None:
        require_nonempty(self.owner_lookup, "owner_lookup")
        require_bounded(self.owner_lookup, "owner_lookup", MAX_IDENTIFIER_CHARS)
        if self.execution_id.pointed != self.attempt_ordinal.pointed:
            raise IntegrityViolationError("pointer execution and ordinal must pair")
        if self.pointer_revision < 0:
            raise ValueError("pointer_revision cannot be negative")

    required_active: ClassVar[Generated] = Generated(
        str,
        Column(generated="CASE WHEN execution_id IS NOT NULL THEN 'active' END", nullable=True),
    )

    checks = ("(execution_id IS NULL) = (attempt_ordinal IS NULL)",)

    unique = (("owner_lookup", "execution_id", "attempt_ordinal"),)

    @classmethod
    def references(cls):
        from agent_comms.coordination_tables.participants import Participants

        return (
            ForeignKey(columns=("owner_lookup",), target=Participants, target_columns=("participant_lookup",)),
            ForeignKey(
                columns=("execution_id", "attempt_ordinal", "owner_lookup", "required_active"),
                target=ExecutionRecord,
                target_columns=("execution_id", "current_attempt_ordinal", "owner_lookup", "status"),
                deferred=True,
            ),
            ForeignKey(
                columns=("execution_id", "attempt_ordinal", "owner_lookup", "required_active"),
                target=AttemptRecord,
                target_columns=("execution_id", "attempt_ordinal", "owner_lookup", "phase_kind"),
                deferred=True,
            ),
        )

_RETRY_BASIS = f"""SELECT e.execution_id,
  CASE WHEN e.current_attempt_ordinal IS NOT NULL
     AND e.current_attempt_ordinal < e.max_attempts
     AND EXISTS (SELECT 1 FROM replay_assessments r
       WHERE r.execution_id = e.execution_id AND r.retry_authorized = 1)
     AND {WireOrigin.retry_sql("e")}
  THEN 1 ELSE 0 END AS authorized
FROM executions e"""


@dataclass(frozen=True, slots=True)
class ExecutionRecord(
    LifecycleTransitions,
    OrdinalTransitions,
    IncrementingColumn,
    MonotoneColumns,
    GuardedColumns,
    NoDelete,
    CoordinatorRow,
    declared_name="executions",
):
    column_orders = (NonDecreasing(("updated_at_ms",)),)
    column_guards = (
        ColumnGuard(("lifecycle",), f"""(json_extract(NEW.lifecycle, '$.kind')
        IN ({{optional_attempt_execution_names}}) AND json_extract(NEW.lifecycle, '$.ordinal')
        IS NOT NULL AND NOT EXISTS (SELECT 1 FROM attempts a WHERE a.execution_id = NEW.execution_id
        AND a.attempt_ordinal = json_extract(NEW.lifecycle, '$.ordinal')
        AND a.owner_lookup = NEW.owner_lookup AND a.phase IN ({{failed_attempt_phases}})))
        OR (({WireOrigin.matches_sql("NEW.origin")}) AND ((NOT EXISTS (SELECT 1 FROM obligations
        WHERE execution_id = NEW.execution_id))
        OR (json_extract(NEW.lifecycle, '$.kind') = 'completed'
        AND EXISTS (SELECT 1 FROM obligations WHERE execution_id = NEW.execution_id
        AND success_terminal != 1)) OR (json_extract(NEW.lifecycle, '$.kind') = 'deferred'
        AND json_extract(NEW.lifecycle, '$.ordinal') IS NOT NULL AND EXISTS (
        SELECT 1 FROM obligations WHERE execution_id = NEW.execution_id AND retryable != 1))))
        OR (json_extract(NEW.lifecycle, '$.kind') = 'failed' AND EXISTS (
        SELECT 1 FROM publication_receipts WHERE execution_id = NEW.execution_id))
        OR (json_extract(NEW.lifecycle, '$.kind') = 'failed'
        AND json_extract(NEW.lifecycle, '$.ordinal')
        IS NOT NULL AND EXISTS (SELECT 1 FROM retry_disposition_basis b
        WHERE b.execution_id = NEW.execution_id AND b.authorized = 1))"""),
    )

    retry_view = "retry_disposition_basis"

    @classmethod
    def schema_objects(cls, *, if_not_exists: bool = False):
        """The retry basis view reads executions, so this row declares it."""
        existence = "IF NOT EXISTS " if if_not_exists else ""
        return super().schema_objects(if_not_exists=if_not_exists) | {
            cls.retry_view: f"CREATE VIEW {existence}{cls.retry_view} AS\n" + _RETRY_BASIS}

    def retry_authorized(
        self, replay: ReplayState, obligations: tuple[ResponseObligation, ...],
    ) -> bool:
        return (
            self.lifecycle.has_retry_budget(self.max_attempts)
            and replay.allows_retry
            and self.origin.retry_authorized(obligations)
        )

    execution_id: Annotated[
        str,
        FrozenColumn(
            primary_key=True,
            check=(
                "\n"
                "        length(execution_id) BETWEEN 1 AND 256 AND instr(executi"
                "on_id, ':') = 0\n"
                "    "
            ),
        ),
    ]
    origin: Annotated[type[ExecutionProvenance], FrozenColumn()]
    lifecycle: Annotated[ExecutionState, SnapshotExclude()]
    owner_thread: Annotated[str, FrozenColumn(check="length(owner_thread) BETWEEN 1 AND 256")]
    owner_lookup: Annotated[str, FrozenColumn()]
    revision: Annotated[int, Column(check="revision > 0")]
    max_attempts: Annotated[int, FrozenColumn(check="max_attempts > 0")]
    reason_code: Annotated[
        ReasonCode,
        Column(storage=TextStorage, nullable=True,
               check="reason_code IS NULL OR length(reason_code) BETWEEN 1 AND 64"),
    ]
    created_at_ms: Annotated[int, FrozenColumn(check="created_at_ms >= 0")]
    updated_at_ms: Annotated[int, Column(check="updated_at_ms >= created_at_ms")]

    @projected(view="snapshot", name="status")
    def snapshot_status(self):
        return type(self.lifecycle)

    @projected(view="snapshot", name="current_attempt_ordinal")
    def snapshot_current_attempt_ordinal(self):
        return self.lifecycle.current_attempt_ordinal

    def __post_init__(self) -> None:
        validate_execution_id(self.execution_id)
        for field, value in (
            ("owner_thread", self.owner_thread),
            ("owner_lookup", self.owner_lookup),
        ):
            require_nonempty(value, field)
            require_bounded(value, field, MAX_IDENTIFIER_CHARS)
        if self.revision <= 0 or self.max_attempts <= 0:
            raise ValueError("revision and max_attempts must be positive")
        self.lifecycle.validate_budget(self.max_attempts)
        if self.created_at_ms < 0 or self.updated_at_ms < self.created_at_ms:
            raise ValueError("execution timestamps are inconsistent")

    status: ClassVar[Generated] = Generated(
        str,
        Column(
            generated="json_extract(lifecycle, '$.kind')",
            check="status IN\n      ({execution_names})",
        ),
    )
    current_attempt_ordinal: ClassVar[Generated] = Generated(
        int,
        Column(
            generated="json_extract(lifecycle, '$.ordinal')",
            check=(
                "\n"
                "        current_attempt_ordinal IS NULL OR current_attempt_ordin"
                "al BETWEEN 1 AND max_attempts\n"
                "    "
            ),
            nullable=True,
        ),
    )
    required_attempt_kind: ClassVar[Generated] = Generated(
        str,
        Column(
            generated=(
                "CASE\n"
                "        WHEN status = 'active' THEN 'active'\n"
                "        WHEN status = 'completed' THEN 'succeeded'\n"
                "    END"
            ),
            nullable=True,
        ),
    )
    active_execution_id: ClassVar[Generated] = Generated(
        str,
        Column(generated="CASE WHEN status = 'active' THEN execution_id END", nullable=True),
    )
    active_attempt_ordinal: ClassVar[Generated] = Generated(
        int,
        Column(
            generated="CASE WHEN status = 'active' THEN current_attempt_ordinal END",
            nullable=True,
        ),
    )
    wire_execution_id: ClassVar[Generated] = Generated(
        str,
        Column(generated=f"CASE WHEN {WireOrigin.matches_sql('origin')} THEN execution_id END", nullable=True),
    )
    wire_claim_ordinal: ClassVar[Generated] = Generated(
        int,
        Column(generated=f"CASE WHEN {WireOrigin.matches_sql('origin')} THEN 0 END", nullable=True),
    )
    claim_status_kind: ClassVar[Generated] = Generated(
        str,
        Column(
            generated=(
                "CASE\n"
                "        WHEN status IN ({engaged_execution_names}) THEN 'engaged"
                "'\n"
                "        ELSE status END"
            ),
            nullable=True,
        ),
    )
    deferred_replay_id: ClassVar[Generated] = Generated(
        str,
        Column(
            generated=(
                "CASE WHEN status = 'deferred' AND current_attempt_ordinal IS NOT"
                " NULL\n"
                "              THEN execution_id END"
            ),
            nullable=True,
        ),
    )
    deferred_replay_required: ClassVar[Generated] = Generated(
        int,
        Column(
            generated=(
                "CASE WHEN status = 'deferred' AND current_attempt_ordinal IS NOT"
                " NULL\n"
                "              THEN 1 END"
            ),
            nullable=True,
        ),
    )
    checks = (
        (
            "(status IN ({unstarted_execution_names}) AND current_attempt_ord"
            "inal IS NULL)\n"
            "      OR (status IN ({required_attempt_execution_names}) AND cur"
            "rent_attempt_ordinal IS NOT NULL)\n"
            "      OR status IN ({optional_attempt_execution_names})"
        ),
        (
            "status != 'deferred' OR current_attempt_ordinal IS NULL\n"
            "           OR current_attempt_ordinal < max_attempts"
        ),
    )
    unique = (
        ("execution_id", "owner_lookup"),
        ("execution_id", "claim_status_kind"),
        ("execution_id", "current_attempt_ordinal", "owner_lookup", "status"),
    )
    indexes = (Index(("owner_lookup", "status"), unique=False, where=None),)

    @classmethod
    def references(cls):
        from agent_comms.coordination_tables.assignments import ExecutionAssignmentLink
        from agent_comms.coordination_tables.participants import Participants

        return (
            ForeignKey(columns=("owner_lookup",), target=Participants, target_columns=("participant_lookup",)),
            ForeignKey(
                columns=(
                    "execution_id",
                    "current_attempt_ordinal",
                    "owner_lookup",
                    "required_attempt_kind",
                ),
                target=AttemptRecord,
                target_columns=("execution_id", "attempt_ordinal", "owner_lookup", "phase_kind"),
                deferred=True,
            ),
            ForeignKey(
                columns=("owner_lookup", "active_execution_id", "active_attempt_ordinal"),
                target=CurrentExecutions,
                target_columns=("owner_lookup", "execution_id", "attempt_ordinal"),
                deferred=True,
            ),
            ForeignKey(
                columns=("wire_execution_id", "wire_claim_ordinal"),
                target=ExecutionAssignmentLink,
                target_columns=("execution_id", "ordinal"),
                deferred=True,
            ),
            ForeignKey(
                columns=("deferred_replay_id", "deferred_replay_required"),
                target=ReplayAssessments,
                target_columns=("execution_id", "retry_authorized"),
                deferred=True,
            ),
        )


class ExecutionReference:
    """An execution-owned row cannot outlive its original execution."""

    @classmethod
    def references(cls) -> tuple[ForeignKey, ...]:
        return (
            ForeignKey(columns=("execution_id",), target=ExecutionRecord, target_columns=("execution_id",), on_delete="RESTRICT"),
        )


class RetryPartition(GuardedAfterWrites):
    """A failed execution cannot retain authorization to retry."""

    after_write_condition = """EXISTS (SELECT 1 FROM retry_disposition_basis b JOIN executions e
        ON e.execution_id = b.execution_id WHERE b.execution_id = NEW.execution_id
        AND b.authorized = 1 AND e.status = 'failed' AND e.current_attempt_ordinal IS NOT NULL)"""


class ReplayState(ABC):
    """An original replay assessment, or the state before one has been recorded."""

    @property
    @abstractmethod
    def allows_retry(self) -> bool: ...

    @property
    @abstractmethod
    def next_revision(self) -> int: ...

    @abstractmethod
    def require_revision(self, expected: object) -> None: ...

    @abstractmethod
    def require_execution(self, execution_id: str) -> None: ...

    @abstractmethod
    def require_assessment(self) -> ReplayAssessments: ...

    @abstractmethod
    def authorize_retry(self, db: sqlite3.Connection, execution_id: str) -> None: ...

    @abstractmethod
    def revoke_retry(self, db: sqlite3.Connection) -> None: ...

    @abstractmethod
    def record(self, db: sqlite3.Connection, after: ReplayAssessments) -> bool: ...

    def observe(
        self, db: sqlite3.Connection, execution_id: str, *, facts: ReplayFact,
        replay_safe: bool, side_effects_possible: bool,
    ) -> bool:
        self.require_execution(execution_id)
        after = ReplayAssessments(
            execution_id, facts, replay_safe, side_effects_possible, self.next_revision
        )
        return self.record(db, after)


@dataclass(frozen=True, slots=True)
class NoReplayAssessment(NullMember, ReplayState):
    @property
    def allows_retry(self) -> bool:
        return False

    @property
    def next_revision(self) -> int:
        return 1

    def require_revision(self, expected: object) -> None:
        try:
            type(self).from_wire(expected)
        except MalformedRecord as error:
            raise StaleRevision("replay assessment revision changed") from error

    def require_execution(self, execution_id: str) -> None:
        """No recorded assessment claims an execution identity."""

    def require_assessment(self) -> ReplayAssessments:
        raise IntegrityViolationError("replay assessment is absent")

    def authorize_retry(self, db: sqlite3.Connection, execution_id: str) -> None:
        self.observe(
            db, execution_id, facts=ReplayFact.NONE,
            replay_safe=True, side_effects_possible=False,
        )

    def revoke_retry(self, db: sqlite3.Connection) -> None:
        """An absent assessment has granted nothing to revoke."""

    def record(self, db: sqlite3.Connection, after: ReplayAssessments) -> bool:
        after.insert(db)
        return True


@dataclass(frozen=True, slots=True)
class ReplayAssessments(
    ReplayState,
    IncrementingColumn,
    MonotoneColumns,
    AccumulatingBits,
    ExecutionReference,
    RetryPartition,
    NoDelete,
    CoordinatorRow,
):
    column_orders = (
        NonDecreasing(("side_effects_possible",)),
        NonIncreasing(("replay_safe",)),
    )
    accumulating_columns = ("facts",)

    @property
    def allows_retry(self) -> bool:
        return self.replay_safe and self.facts == ReplayFact.NONE and not self.side_effects_possible

    execution_id: Annotated[str, SnapshotExclude(), FrozenColumn(primary_key=True)]
    facts: Annotated[ReplayFact, Column(check="facts >= 0 AND facts <= 1023")]
    replay_safe: Annotated[bool, Column()]
    side_effects_possible: Annotated[bool, Column()]
    revision: Annotated[int, Column(check="revision > 0")]

    @classmethod
    def accumulate(
        cls, db: sqlite3.Connection, execution_id: str, observed: ReplayFact
    ) -> None:
        """Join original observations under the caller's existing attempt fence.

        Missing replay evidence cannot establish safety. Only an existing
        assessment may retain that fact, and every incoming observation can
        revoke it. Output facts remain monotonic across all attempts.
        """
        if not observed or cls.exists(
            db,
            where=(
                "execution_id=? AND (facts & ?) = ? AND replay_safe=0 AND side_effects_possible=1"
            ),
            parameters=(execution_id, int(observed), int(observed)),
        ):
            return
        # Most streamed deltas repeat a fact already durably joined. Read only
        # that answer, not a decoded row or another write-lock acquisition.
        # A new fact joins atomically: another connection may have accumulated
        # different evidence since the existence check, even in autocommit use.
        statement, values = cls(execution_id, observed, False, True, 1)._insertion()
        db.execute(
            statement + """
            ON CONFLICT (execution_id) DO UPDATE SET
                facts = facts | excluded.facts,
                replay_safe = 0,
                side_effects_possible = 1,
                revision = revision + 1
            WHERE (facts | excluded.facts) != facts
               OR replay_safe != 0 OR side_effects_possible != 1
            """,
            values,
        )

    @classmethod
    def for_execution(cls, db: sqlite3.Connection, execution_id: str) -> ReplayAssessment:
        return next(iter(cls._keyed(db, dict(execution_id=execution_id))), NoReplayAssessment())

    @classmethod
    def authorize_if_unobserved(cls, db: sqlite3.Connection, execution_id: str) -> None:
        """Record provider-failure proof only while no output fact exists."""
        cls.for_execution(db, execution_id).authorize_retry(db, execution_id)

    @classmethod
    def revoke(cls, db: sqlite3.Connection, execution_id: str) -> None:
        """A nonretryable failure consumes any earlier provider-failure proof."""
        cls.for_execution(db, execution_id).revoke_retry(db)

    @property
    def next_revision(self) -> int:
        return self.revision + 1

    def require_revision(self, expected: object) -> None:
        if expected != self.revision:
            raise StaleRevision("replay assessment revision changed")

    def require_execution(self, execution_id: str) -> None:
        if self.execution_id != execution_id:
            raise IntegrityViolationError("snapshot records belong to another execution")

    def require_assessment(self) -> ReplayAssessments:
        return self

    def authorize_retry(self, db: sqlite3.Connection, execution_id: str) -> None:
        """An existing assessment cannot be authorized by a later provider failure."""

    def revoke_retry(self, db: sqlite3.Connection) -> None:
        if self.replay_safe:
            self.record(db, replace(self, replay_safe=False, revision=self.next_revision))

    def require_successor(self, after: ReplayAssessments) -> None:
        accumulated = replace(
            after,
            facts=self.facts | after.facts,
            replay_safe=self.replay_safe and after.replay_safe,
            side_effects_possible=self.side_effects_possible or after.side_effects_possible,
        )
        if accumulated != after:
            raise IdentityConflict("replay facts cannot be erased")

    def record(self, db: sqlite3.Connection, after: ReplayAssessments) -> bool:
        """The original recorded state validates and persists its successor."""
        after.require_execution(self.execution_id)
        if replace(self, revision=after.revision) == after:
            return False
        self.require_successor(after)
        ReplayAssessments.update(
            db,
            where="execution_id=?",
            parameters=(self.execution_id,),
            facts=after.facts,
            replay_safe=after.replay_safe,
            side_effects_possible=after.side_effects_possible,
            revision=self.next_revision,
        )
        return True

    def __post_init__(self) -> None:
        validate_execution_id(self.execution_id)
        if self.revision <= 0:
            raise ValueError("replay revision must be positive")
        if int(self.facts) < 0 or int(self.facts) & ~APPROVED_REPLAY_FACT_MASK:
            raise ValueError("unrecognized replay fact bits")
        if self.facts != ReplayFact.NONE and self.replay_safe:
            raise IntegrityViolationError("an execution with ambiguity facts cannot be replay-safe")

    retry_authorized: ClassVar[Generated] = Generated(
        int,
        Column(
            generated=(
                "CASE WHEN facts = 0 AND replay_safe = 1 AND side_effects_possibl"
                "e = 0\n"
                "              THEN 1 ELSE 0 END"
            ),
            nullable=True,
        ),
    )

    checks = ("facts = 0 OR replay_safe = 0",)
    unique = (("execution_id", "retry_authorized"),)


# The fixed external snapshot format is still a row object or JSON null.
ReplayAssessment = ReplayAssessments | NoReplayAssessment
