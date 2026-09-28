"""Durable coordination declarations and versioned SQLite schema.

This module owns coordinator authority.  Backend ``turn_state`` records are
attempt evidence only; they never mutate this store implicitly.  The first
implementation slice intentionally exposes schema initialization and immutable
value types only.  Transition APIs are added separately so initialization
cannot accidentally become execution authority.
"""

from __future__ import annotations

import hashlib
import math
import os
import sqlite3
import stat
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from enum import IntFlag, StrEnum
from pathlib import Path
from typing import Final, Self

from .assignment_states import AssignmentState
from .attempt_states import AttemptState
from .coordination_errors import CoordinationError as CoordinationError
from .coordination_errors import IntegrityViolationError, SchemaVersionError
from .execution_states import ExecutionState
from .field_codec import projected
from .messages import Message, MessageType
from .obligation_states import ResponseState
from .recovery_states import RecoveryCondition
from .typed_table import (
    Column,
    ExactStorage,
    ForeignKey,
    Index,
    SQLiteUserVersion,
    TypedRow,
    TypedTable,
)
from .wake_policy import WakePolicy

COORDINATION_SCHEMA_VERSION: Final = 7
COORDINATION_SNAPSHOT_VERSION: Final = 2
RESOLVER_VERSION: Final = "resolver-v1"
POLICY_VERSION: Final = "policy-v1"
MAX_PUBLICATION_PAYLOAD_BYTES: Final = 120_000
MAX_REASON_CODE_CHARS: Final = 64
MAX_SANITIZED_DETAIL_CHARS: Final = 512
MAX_IDENTIFIER_CHARS: Final = 256


class MessageAudience(StrEnum):
    DIRECT = "direct"
    MENTIONED = "mentioned"
    COLLECTIVE = "collective"


class ExecutionOrigin(StrEnum):
    WIRE = "wire"
    ACP = "acp"
    GOAL = "goal"
    SYSTEM = "system"


class ReplayFact(IntFlag):
    NONE = 0
    ASSISTANT_OUTPUT = 1 << 0
    THINKING_OUTPUT = 1 << 1
    TOOL_CALL_STARTED = 1 << 2
    TOOL_EXECUTED = 1 << 3
    INPUT_FORWARDED = 1 << 4
    COMPACTION = 1 << 5
    PROJECT_EFFECTS = 1 << 6
    WIRE_EFFECTS = 1 << 7
    EXTENSION_EFFECTS = 1 << 8
    UNKNOWN_EFFECTS = 1 << 9


APPROVED_REPLAY_FACT_MASK: Final = sum(fact.value for fact in ReplayFact)


class OwnerConnectivity(StrEnum):
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    OFFLINE = "offline"


class ACPClientConnectivity(StrEnum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"


def _nonempty(value: str, field: str) -> None:
    if not value:
        raise ValueError(f"{field} cannot be empty")


def _bounded(value: str | None, field: str, maximum: int) -> None:
    if value is not None and len(value) > maximum:
        raise ValueError(f"{field} exceeds {maximum} characters")


def _optional_nonempty(value: str | None, field: str, maximum: int) -> None:
    if value is not None:
        _nonempty(value, field)
        _bounded(value, field, maximum)


def _validate_execution_id(execution_id: str) -> None:
    _nonempty(execution_id, "execution_id")
    _bounded(execution_id, "execution_id", MAX_IDENTIFIER_CHARS)
    if ":" in execution_id:
        raise ValueError("execution_id cannot contain ':'")


def canonical_publication_key(execution_id: str, exact_target: str) -> str:
    """Return the sole deterministic publication identity for an obligation route."""
    _validate_execution_id(execution_id)
    _nonempty(exact_target, "exact_target")
    _bounded(exact_target, "exact_target", MAX_IDENTIFIER_CHARS)
    key = f"publication:v1:{execution_id}:{exact_target}"
    _bounded(key, "publication_key", MAX_IDENTIFIER_CHARS)
    return key


class CoordinatorTable:
    """Capability for the coordinator schema; cases and transitions come from their owners."""

    @classmethod
    def schema_objects(cls):
        return {
            name: sql.format(**_schema_context()) for name, sql in super().schema_objects().items()
        }


@dataclass(frozen=True, slots=True)
class WakeAssignment:
    assignment_id: str = dataclass_field(metadata={"wire_name": "claim_id"})
    recipient: str
    recipient_lookup: str
    wire_seq: int
    message_id: str
    audience: MessageAudience
    lifecycle: AssignmentState = dataclass_field(metadata={"snapshot_exclude": True})
    accepted_at_ms: int
    updated_at_ms: int
    revision: int = 1
    resolver_version: str = RESOLVER_VERSION
    policy_version: str = POLICY_VERSION

    @projected(view="snapshot", name="disposition")
    def snapshot_disposition(self):
        return self.lifecycle.declared_name

    @projected(view="snapshot", name="wake_mode")
    def snapshot_wake_mode(self):
        return self.lifecycle.mode.declared_name

    @projected(view="snapshot", name="triage_verdict")
    def snapshot_triage_verdict(self):
        verdict = self.lifecycle.verdict
        return verdict

    @projected(view="snapshot", name="execution_id")
    def snapshot_execution_id(self):
        return self.lifecycle.execution_id

    @projected(view="snapshot", name="exact_target")
    def snapshot_exact_target(self):
        return self.lifecycle.exact_target

    def __post_init__(self) -> None:
        for field, value in (
            ("claim_id", self.assignment_id),
            ("recipient", self.recipient),
            ("recipient_lookup", self.recipient_lookup),
            ("message_id", self.message_id),
            ("resolver_version", self.resolver_version),
            ("policy_version", self.policy_version),
        ):
            _nonempty(value, field)
            _bounded(value, field, MAX_IDENTIFIER_CHARS)
        _optional_nonempty(self.lifecycle.exact_target, "exact_target", MAX_IDENTIFIER_CHARS)
        if self.lifecycle.execution_id is not None:
            _validate_execution_id(self.lifecycle.execution_id)
        if self.wire_seq <= 0 or self.revision <= 0:
            raise ValueError("wire_seq and revision must be positive")
        if self.accepted_at_ms < 0 or self.updated_at_ms < self.accepted_at_ms:
            raise ValueError("claim timestamps are inconsistent")
        object.__setattr__(self, "audience", MessageAudience(self.audience))

    @property
    def durable_key(self) -> tuple[str, int]:
        return (self.recipient_lookup, self.wire_seq)


def assignment_transition_allowed(before: WakeAssignment, after: WakeAssignment) -> bool:
    """Check an edge without rewriting accepted wake, verdict, or execution facts."""
    frozen = (
        "assignment_id",
        "recipient_lookup",
        "wire_seq",
        "message_id",
        "recipient",
        "audience",
        "resolver_version",
        "policy_version",
        "accepted_at_ms",
    )
    edge = before.lifecycle.may_become(after.lifecycle)
    return (
        edge
        and before.lifecycle.mode == after.lifecycle.mode
        and all(getattr(before, field) == getattr(after, field) for field in frozen)
        and after.revision == before.revision + 1
        and after.updated_at_ms >= before.updated_at_ms
        and (
            before.lifecycle.verdict is None or after.lifecycle.verdict == before.lifecycle.verdict
        )
        and (
            before.lifecycle.execution_id is None
            or after.lifecycle.execution_id == before.lifecycle.execution_id
        )
        and (
            before.lifecycle.exact_target is None
            or after.lifecycle.exact_target == before.lifecycle.exact_target
        )
        and before.lifecycle.permits_engagement_change(after.lifecycle)
    )


def obligation_transition_allowed(before: ResponseObligation, after: ResponseObligation) -> bool:
    """Pure declared-edge and immutable route/receipt relation check."""
    return (
        before.execution_id == after.execution_id
        and before.exact_target == after.exact_target
        and before.created_at_ms == after.created_at_ms
        and before.lifecycle.may_become(after.lifecycle)
        and after.revision == before.revision + 1
        and after.updated_at_ms >= before.updated_at_ms
        and (
            before.lifecycle.receipt_message_id is None
            or before.lifecycle.receipt_message_id == after.lifecycle.receipt_message_id
        )
        and (
            before.lifecycle.receipt_seq is None
            or before.lifecycle.receipt_seq == after.lifecycle.receipt_seq
        )
    )


def execution_status_transition_allowed(before: ExecutionRecord, after: ExecutionRecord) -> bool:
    """Pure aggregate edge/CAS relation; detailed model state belongs to attempts."""
    if (
        before.execution_id,
        before.origin,
        before.owner_lookup,
        before.owner_thread,
        before.exact_target,
        before.max_attempts,
        before.created_at_ms,
    ) != (
        after.execution_id,
        after.origin,
        after.owner_lookup,
        after.owner_thread,
        after.exact_target,
        after.max_attempts,
        after.created_at_ms,
    ):
        return False
    if (
        type(after.lifecycle) is not type(before.lifecycle)
        and not before.lifecycle.may_become(after.lifecycle)
        or after.revision != before.revision + 1
        or after.updated_at_ms < before.updated_at_ms
    ):
        return False
    return after.lifecycle.accepts_previous(before.lifecycle)


def attempt_phase_transition_allowed(before: AttemptRecord, after: AttemptRecord) -> bool:
    """Pure per-attempt CAS; retry resets evidence only through a new row."""
    return (
        (
            before.execution_id,
            before.attempt_ordinal,
            before.owner_lookup,
            before.owner_thread,
            before.owner_generation,
            before.owner_token_digest,
            before.created_at_ms,
        )
        == (
            after.execution_id,
            after.attempt_ordinal,
            after.owner_lookup,
            after.owner_thread,
            after.owner_generation,
            after.owner_token_digest,
            after.created_at_ms,
        )
        and not before.lifecycle.terminal
        and (
            type(after.lifecycle) is type(before.lifecycle)
            or before.lifecycle.may_become(after.lifecycle)
        )
        and after.revision == before.revision + 1
        and after.updated_at_ms >= before.updated_at_ms
        and (not before.lifecycle.backend_done or after.lifecycle.backend_done)
        and (not before.lifecycle.process_dead or after.lifecycle.process_dead)
        and (
            before.last_progress_at_ms is None
            or (
                after.last_progress_at_ms is not None
                and after.last_progress_at_ms >= before.last_progress_at_ms
            )
        )
        and (
            before.lifecycle.lease_expires_at_ms is None
            or after.lifecycle.lease_expires_at_ms is None
            or after.lifecycle.lease_expires_at_ms >= before.lifecycle.lease_expires_at_ms
        )
        and (
            not after.lifecycle.terminal
            or (after.lifecycle.backend_done and after.lifecycle.process_dead)
        )
    )


@dataclass(frozen=True, slots=True)
class OwnerFence:
    execution_id: str
    attempt_ordinal: int
    owner_thread: str
    owner_generation: int
    revision: int
    token: str

    def __post_init__(self) -> None:
        _validate_execution_id(self.execution_id)
        for field, value in (("owner_thread", self.owner_thread), ("token", self.token)):
            _nonempty(value, field)
            _bounded(value, field, MAX_IDENTIFIER_CHARS)
        if min(self.attempt_ordinal, self.owner_generation, self.revision) <= 0:
            raise ValueError("fence ordinal, generation and revision must be positive")


@dataclass(frozen=True, slots=True)
class ExecutionAssignmentLink(CoordinatorTable, TypedTable, declared_name="execution_claims"):
    execution_id: str = dataclass_field(metadata={"sql": Column(primary_key=True)})
    assignment_id: str = dataclass_field(
        metadata={"wire_name": "claim_id", "sql": Column(unique=True)}
    )
    ordinal: int = dataclass_field(metadata={"sql": Column(primary_key=True, check="ordinal>=0")})
    unique = (("execution_id", "assignment_id"),)

    def __post_init__(self) -> None:
        _validate_execution_id(self.execution_id)
        _nonempty(self.assignment_id, "claim_id")
        _bounded(self.assignment_id, "claim_id", MAX_IDENTIFIER_CHARS)
        if self.ordinal < 0:
            raise ValueError("execution claim ordinal cannot be negative")

    @classmethod
    def references(cls):
        return (
            ForeignKey(
                ("execution_id",),
                ExecutionRecord,
                ("execution_id",),
                deferred=False,
                on_delete="RESTRICT",
            ),
            ForeignKey(
                ("assignment_id", "execution_id"),
                WakeClaims,
                ("claim_id", "execution_id"),
                deferred=False,
                on_delete=None,
            ),
        )

    @classmethod
    def triggers(cls):
        return {
            "execution_claim_membership_insert": (
                """CREATE TRIGGER execution_claim_membership_insert BEFORE INSERT ON
    execution_claims
    WHEN NOT EXISTS (SELECT 1 FROM executions e WHERE e.execution_id =
    NEW.execution_id
      AND e.status IN ({unstarted_execution_names}))
     OR NEW.ordinal != (SELECT count(*) FROM execution_claims
                        WHERE execution_id = NEW.execution_id)
    BEGIN SELECT RAISE(ABORT,
    'execution claims require initial contiguous membership' ); END"""
            ),
            "execution_claim_target_insert": (
                """CREATE TRIGGER execution_claim_target_insert
    BEFORE INSERT ON execution_claims
    WHEN NOT EXISTS (
        SELECT 1 FROM executions e JOIN wake_claims c
          ON c.execution_id = e.execution_id
        WHERE e.execution_id = NEW.execution_id AND c.claim_id = NEW.assignment_id
          AND c.exact_target = e.exact_target
          AND c.recipient_lookup = e.owner_lookup
    )
    BEGIN
        SELECT RAISE(ABORT, 'claim target does not match execution target');
    END"""
            ),
            "execution_claim_target_update": (
                """CREATE TRIGGER execution_claim_target_update
    BEFORE UPDATE ON execution_claims
    BEGIN
        SELECT RAISE(ABORT, 'execution claim relation is immutable');
    END"""
            ),
            "execution_claim_delete_frozen": (
                """CREATE TRIGGER execution_claim_delete_frozen BEFORE DELETE ON
    execution_claims BEGIN
        SELECT RAISE(ABORT, 'execution claim relation cannot be deleted'
        );
    END"""
            ),
        }


@dataclass(frozen=True, slots=True)
class CurrentExecutions(CoordinatorTable, TypedTable):
    owner_lookup: str = dataclass_field(metadata={"sql": Column(primary_key=True)})
    execution_id: str | None = dataclass_field(metadata={"sql": Column()})
    attempt_ordinal: int | None = dataclass_field(metadata={"sql": Column()})
    pointer_revision: int = dataclass_field(metadata={"sql": Column(check="pointer_revision >= 0")})

    def __post_init__(self) -> None:
        _nonempty(self.owner_lookup, "owner_lookup")
        _bounded(self.owner_lookup, "owner_lookup", MAX_IDENTIFIER_CHARS)
        if (self.execution_id is None) != (self.attempt_ordinal is None):
            raise IntegrityViolationError("pointer execution and ordinal must pair")
        if self.execution_id is not None:
            _validate_execution_id(self.execution_id)
            if self.attempt_ordinal is None or self.attempt_ordinal <= 0:
                raise ValueError("pointer ordinal must be positive")
        if self.pointer_revision < 0:
            raise ValueError("pointer_revision cannot be negative")

    def transition_allowed(
        self,
        after: CurrentExecutions,
        execution: ExecutionRecord | None,
        attempt: AttemptRecord | None,
    ) -> bool:
        if (
            self.owner_lookup != after.owner_lookup
            or after.pointer_revision != self.pointer_revision + 1
        ):
            return False
        if after.execution_id is None:
            return True
        if execution is None or attempt is None:
            return False
        try:
            after.assert_matches(execution, attempt)
        except IntegrityViolationError:
            return False
        return True

    def assert_matches(self, execution: ExecutionRecord, attempt: AttemptRecord) -> None:
        if (
            self.execution_id != execution.execution_id
            or self.attempt_ordinal != execution.lifecycle.current_attempt_ordinal
            or (attempt.execution_id, attempt.attempt_ordinal)
            != (self.execution_id, self.attempt_ordinal)
            or self.owner_lookup != execution.owner_lookup
            or attempt.owner_lookup != self.owner_lookup
            or not execution.lifecycle.active
            or attempt.lifecycle.terminal
        ):
            raise IntegrityViolationError("current pointer requires exact active attempt")

    required_active: str | None = dataclass_field(
        init=False,
        default=None,
        compare=False,
        metadata={"sql": Column(generated="CASE WHEN execution_id IS NOT NULL THEN 'active' END")},
    )

    checks = ("(execution_id IS NULL) = (attempt_ordinal IS NULL)",)

    unique = (("owner_lookup", "execution_id", "attempt_ordinal"),)

    @classmethod
    def references(cls):
        return (
            ForeignKey(
                ("owner_lookup",),
                Participants,
                ("participant_lookup",),
                deferred=False,
                on_delete=None,
            ),
            ForeignKey(
                ("execution_id", "attempt_ordinal", "owner_lookup", "required_active"),
                ExecutionRecord,
                ("execution_id", "current_attempt_ordinal", "owner_lookup", "status"),
                deferred=True,
                on_delete=None,
            ),
            ForeignKey(
                ("execution_id", "attempt_ordinal", "owner_lookup", "required_active"),
                AttemptRecord,
                ("execution_id", "attempt_ordinal", "owner_lookup", "phase_kind"),
                deferred=True,
                on_delete=None,
            ),
        )

    @classmethod
    def triggers(cls):
        return {
            "current_pointer_insert_owner": (
                """CREATE TRIGGER current_pointer_insert_owner BEFORE INSERT ON current_executions
WHEN NOT EXISTS (SELECT 1 FROM owner_generations
 WHERE owner_lookup = NEW.owner_lookup)
BEGIN SELECT RAISE(ABORT, 'current pointer owner is not registered'); END"""
            ),
            "current_pointer_update_owner": (
                """CREATE TRIGGER current_pointer_update_owner BEFORE UPDATE ON current_executions
WHEN NEW.owner_lookup IS NOT OLD.owner_lookup
 OR NEW.pointer_revision != OLD.pointer_revision + 1
BEGIN SELECT RAISE(ABORT, 'current pointer owner/revision mismatch'); END"""
            ),
            "current_pointer_delete_frozen": (
                """CREATE TRIGGER current_pointer_delete_frozen BEFORE DELETE ON
current_executions BEGIN
    SELECT RAISE(ABORT, 'current pointer cannot be deleted' );
END"""
            ),
        }


@dataclass(frozen=True, slots=True)
class ExecutionRecord(CoordinatorTable, TypedTable, declared_name="executions"):
    execution_id: str = dataclass_field(
        metadata={
            "sql": Column(
                primary_key=True,
                check=(
                    "\n"
                    "        length(execution_id) BETWEEN 1 AND 256 AND instr(executi"
                    "on_id, ':') = 0\n"
                    "    "
                ),
            )
        }
    )
    origin: ExecutionOrigin
    lifecycle: ExecutionState = dataclass_field(metadata={"snapshot_exclude": True})
    owner_thread: str = dataclass_field(
        metadata={"sql": Column(check="length(owner_thread) BETWEEN 1 AND 256")}
    )
    owner_lookup: str
    revision: int = dataclass_field(metadata={"sql": Column(check="revision > 0")})
    max_attempts: int = dataclass_field(metadata={"sql": Column(check="max_attempts > 0")})
    reason_code: str | None = dataclass_field(
        metadata={
            "sql": Column(check="reason_code IS NULL OR length(reason_code) BETWEEN 1 AND 64")
        }
    )
    created_at_ms: int = dataclass_field(metadata={"sql": Column(check="created_at_ms >= 0")})
    updated_at_ms: int = dataclass_field(
        metadata={"sql": Column(check="updated_at_ms >= created_at_ms")}
    )
    exact_target: str | None = dataclass_field(
        default=None,
        metadata={
            "sql": Column(check="exact_target IS NULL OR length(exact_target) BETWEEN 1 AND 256")
        },
    )

    @projected(view="snapshot", name="status")
    def snapshot_status(self):
        return self.lifecycle.declared_name

    @projected(view="snapshot", name="current_attempt_ordinal")
    def snapshot_current_attempt_ordinal(self):
        return self.lifecycle.current_attempt_ordinal

    def __post_init__(self) -> None:
        _validate_execution_id(self.execution_id)
        object.__setattr__(self, "origin", ExecutionOrigin(self.origin))
        for field, value in (
            ("owner_thread", self.owner_thread),
            ("owner_lookup", self.owner_lookup),
        ):
            _nonempty(value, field)
            _bounded(value, field, MAX_IDENTIFIER_CHARS)
        if self.revision <= 0 or self.max_attempts <= 0:
            raise ValueError("revision and max_attempts must be positive")
        self.lifecycle.validate_budget(self.max_attempts)
        _optional_nonempty(self.reason_code, "reason_code", MAX_REASON_CODE_CHARS)
        if self.created_at_ms < 0 or self.updated_at_ms < self.created_at_ms:
            raise ValueError("execution timestamps are inconsistent")
        if self.origin is ExecutionOrigin.WIRE:
            if self.exact_target is None:
                raise IntegrityViolationError("wire execution requires an exact target")
            _nonempty(self.exact_target, "exact_target")
            _bounded(self.exact_target, "exact_target", MAX_IDENTIFIER_CHARS)
        elif self.exact_target is not None:
            raise IntegrityViolationError("claimless execution requires null target")

    status: str = dataclass_field(
        init=False,
        compare=False,
        repr=False,
        default=None,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                generated="json_extract(lifecycle, '$.kind')",
                check="status IN\n      ({execution_names})",
            ),
        },
    )
    current_attempt_ordinal: int | None = dataclass_field(
        init=False,
        compare=False,
        repr=False,
        default=None,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                generated="json_extract(lifecycle, '$.ordinal')",
                check=(
                    "\n"
                    "        current_attempt_ordinal IS NULL OR current_attempt_ordin"
                    "al BETWEEN 1 AND max_attempts\n"
                    "    "
                ),
            ),
        },
    )
    required_attempt_kind: str | None = dataclass_field(
        init=False,
        default=None,
        compare=False,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                generated=(
                    "CASE\n"
                    "        WHEN status = 'active' THEN 'active'\n"
                    "        WHEN status = 'completed' THEN 'succeeded'\n"
                    "        WHEN status IN ({optional_attempt_execution_names}) AND "
                    "current_attempt_ordinal IS NOT NULL\n"
                    "          THEN 'attempt_failed'\n"
                    "    END"
                )
            ),
        },
    )
    active_execution_id: str | None = dataclass_field(
        init=False,
        default=None,
        compare=False,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(generated="CASE WHEN status = 'active' THEN execution_id END"),
        },
    )
    active_attempt_ordinal: int | None = dataclass_field(
        init=False,
        default=None,
        compare=False,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(generated="CASE WHEN status = 'active' THEN current_attempt_ordinal END"),
        },
    )
    wire_execution_id: str | None = dataclass_field(
        init=False,
        default=None,
        compare=False,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(generated="CASE WHEN origin = 'wire' THEN execution_id END"),
        },
    )
    wire_claim_ordinal: int | None = dataclass_field(
        init=False,
        default=None,
        compare=False,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(generated="CASE WHEN origin = 'wire' THEN 0 END"),
        },
    )
    claim_status_kind: str | None = dataclass_field(
        init=False,
        default=None,
        compare=False,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                generated=(
                    "CASE\n"
                    "        WHEN status IN ({engaged_execution_names}) THEN 'engaged"
                    "'\n"
                    "        ELSE status END"
                )
            ),
        },
    )
    completed_wire_id: str | None = dataclass_field(
        init=False,
        default=None,
        compare=False,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                generated="CASE WHEN status = 'completed' AND origin = 'wire' THEN execution_id END"
            ),
        },
    )
    required_obligation_terminal: int | None = dataclass_field(
        init=False,
        default=None,
        compare=False,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                generated="CASE WHEN status = 'completed' AND origin = 'wire' THEN 1 END"
            ),
        },
    )
    deferred_replay_id: str | None = dataclass_field(
        init=False,
        default=None,
        compare=False,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                generated=(
                    "CASE WHEN status = 'deferred' AND current_attempt_ordinal IS NOT"
                    " NULL\n"
                    "              THEN execution_id END"
                )
            ),
        },
    )
    deferred_replay_required: int | None = dataclass_field(
        init=False,
        default=None,
        compare=False,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                generated=(
                    "CASE WHEN status = 'deferred' AND current_attempt_ordinal IS NOT"
                    " NULL\n"
                    "              THEN 1 END"
                )
            ),
        },
    )
    deferred_obligation_id: str | None = dataclass_field(
        init=False,
        default=None,
        compare=False,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                generated=(
                    "CASE WHEN status = 'deferred' AND current_attempt_ordinal IS NOT"
                    " NULL\n"
                    "                   AND origin = 'wire' THEN execution_id END"
                )
            ),
        },
    )
    deferred_obligation_required: int | None = dataclass_field(
        init=False,
        default=None,
        compare=False,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                generated=(
                    "CASE WHEN status = 'deferred' AND current_attempt_ordinal IS NOT"
                    " NULL\n"
                    "                   AND origin = 'wire' THEN 1 END"
                )
            ),
        },
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
        (
            "(origin = 'wire' AND exact_target IS NOT NULL)\n"
            "      OR (origin != 'wire' AND exact_target IS NULL)"
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
        return (
            ForeignKey(
                ("owner_lookup",),
                Participants,
                ("participant_lookup",),
                deferred=False,
                on_delete=None,
            ),
            ForeignKey(
                (
                    "execution_id",
                    "current_attempt_ordinal",
                    "owner_lookup",
                    "required_attempt_kind",
                ),
                AttemptRecord,
                ("execution_id", "attempt_ordinal", "owner_lookup", "phase_kind"),
                deferred=True,
                on_delete=None,
            ),
            ForeignKey(
                ("owner_lookup", "active_execution_id", "active_attempt_ordinal"),
                CurrentExecutions,
                ("owner_lookup", "execution_id", "attempt_ordinal"),
                deferred=True,
                on_delete=None,
            ),
            ForeignKey(
                ("wire_execution_id", "wire_claim_ordinal"),
                ExecutionAssignmentLink,
                ("execution_id", "ordinal"),
                deferred=True,
                on_delete=None,
            ),
            ForeignKey(
                ("wire_execution_id",),
                ResponseObligation,
                ("execution_id",),
                deferred=True,
                on_delete=None,
            ),
            ForeignKey(
                ("completed_wire_id", "required_obligation_terminal"),
                ResponseObligation,
                ("execution_id", "success_terminal"),
                deferred=True,
                on_delete=None,
            ),
            ForeignKey(
                ("deferred_replay_id", "deferred_replay_required"),
                ReplayAssessments,
                ("execution_id", "retry_authorized"),
                deferred=True,
                on_delete=None,
            ),
            ForeignKey(
                ("deferred_obligation_id", "deferred_obligation_required"),
                ResponseObligation,
                ("execution_id", "retryable"),
                deferred=True,
                on_delete=None,
            ),
        )

    @classmethod
    def triggers(cls):
        return {
            "execution_status_edge": (
                """CREATE TRIGGER execution_status_edge BEFORE UPDATE OF lifecycle ON executions
WHEN json_extract(NEW.lifecycle, '$.kind') != json_extract(OLD.lifecycle, '$.kind') AND NOT ({execution_edges})
BEGIN SELECT RAISE(ABORT, 'execution status transition is not declared'); END"""
            ),
            "execution_frozen_facts": (
                """CREATE TRIGGER execution_frozen_facts BEFORE UPDATE ON executions
WHEN NEW.execution_id IS NOT OLD.execution_id OR NEW.origin IS NOT OLD.origin
 OR NEW.exact_target IS NOT OLD.exact_target OR NEW.owner_lookup IS NOT OLD.owner_lookup
 OR NEW.owner_thread IS NOT OLD.owner_thread OR NEW.max_attempts != OLD.max_attempts
 OR NEW.created_at_ms != OLD.created_at_ms OR NEW.revision != OLD.revision + 1
 OR NEW.updated_at_ms < OLD.updated_at_ms OR
 (json_extract(NEW.lifecycle, '$.kind') = 'active' AND
  (json_extract(OLD.lifecycle, '$.kind') NOT IN ({startable_execution_names}) OR
   json_extract(NEW.lifecycle, '$.ordinal') != coalesce(json_extract(OLD.lifecycle, '$.ordinal'), 0) + 1)) OR
 (json_extract(NEW.lifecycle, '$.kind') != 'active' AND
  json_extract(NEW.lifecycle, '$.ordinal') IS NOT json_extract(OLD.lifecycle, '$.ordinal'))
BEGIN SELECT RAISE(ABORT, 'execution transition rewrites frozen authority'); END"""
            ),
            "execution_failure_receipt_guard": (
                """CREATE TRIGGER execution_failure_receipt_guard BEFORE UPDATE OF lifecycle
ON executions
WHEN json_extract(NEW.lifecycle, '$.kind') = 'failed' AND EXISTS (
  SELECT 1 FROM publication_receipts WHERE execution_id =
  NEW.execution_id)
BEGIN SELECT RAISE(ABORT,
'failed execution cannot erase publication receipt' ); END"""
            ),
            "execution_delete_frozen": (
                """CREATE TRIGGER execution_delete_frozen BEFORE DELETE ON executions BEGIN
    SELECT RAISE(ABORT, 'execution cannot be deleted');
END"""
            ),
            "failed_retry_partition_update": (
                """CREATE TRIGGER failed_retry_partition_update BEFORE UPDATE OF lifecycle
ON executions
WHEN json_extract(NEW.lifecycle, '$.kind') = 'failed' AND json_extract(NEW.lifecycle, '$.ordinal') IS NOT NULL
 AND EXISTS (SELECT 1 FROM retry_disposition_basis b
             WHERE b.execution_id = NEW.execution_id AND b.authorized
             = 1)
BEGIN SELECT RAISE(ABORT, 'authorized retry cannot settle failed' );
END"""
            ),
        }


@dataclass(frozen=True, slots=True)
class AttemptRecord(CoordinatorTable, TypedTable, declared_name="attempts"):
    execution_id: str = dataclass_field(
        metadata={"snapshot_exclude": True, "sql": Column(primary_key=True)}
    )
    attempt_ordinal: int = dataclass_field(
        metadata={"sql": Column(primary_key=True, check="attempt_ordinal > 0")}
    )
    owner_lookup: str
    owner_thread: str = dataclass_field(
        metadata={"sql": Column(check="length(owner_thread) BETWEEN 1 AND 256")}
    )
    owner_generation: int = dataclass_field(metadata={"sql": Column(check="owner_generation > 0")})
    owner_token_digest: str = dataclass_field(
        metadata={
            "snapshot_exclude": True,
            "sql": Column(unique=True, check="length(owner_token_digest) BETWEEN 1 AND 256"),
        }
    )
    lifecycle: AttemptState = dataclass_field(metadata={"snapshot_exclude": True})
    revision: int = dataclass_field(metadata={"sql": Column(check="revision > 0")})
    last_progress_at_ms: int | None
    reason_code: str | None = dataclass_field(
        metadata={
            "sql": Column(check="reason_code IS NULL OR length(reason_code) BETWEEN 1 AND 64")
        }
    )
    created_at_ms: int = dataclass_field(metadata={"sql": Column(check="created_at_ms >= 0")})
    updated_at_ms: int = dataclass_field(
        metadata={"sql": Column(check="updated_at_ms >= created_at_ms")}
    )

    @projected(view="snapshot", name="phase")
    def snapshot_phase(self):
        return self.lifecycle.declared_name

    @projected(view="snapshot", name="lease_expires_at_ms")
    def snapshot_lease_expires_at_ms(self):
        return self.lifecycle.lease_expires_at_ms

    @projected(view="snapshot", name="backend_done")
    def snapshot_backend_done(self):
        return self.lifecycle.backend_done

    @projected(view="snapshot", name="process_dead")
    def snapshot_process_dead(self):
        return self.lifecycle.process_dead

    def __post_init__(self) -> None:
        _validate_execution_id(self.execution_id)
        for field, value in (
            ("owner_lookup", self.owner_lookup),
            ("owner_thread", self.owner_thread),
            ("owner_token_digest", self.owner_token_digest),
        ):
            _nonempty(value, field)
            _bounded(value, field, MAX_IDENTIFIER_CHARS)
        if min(self.attempt_ordinal, self.owner_generation, self.revision) <= 0:
            raise ValueError("attempt identity/generation/revision must be positive")
        _optional_nonempty(self.reason_code, "reason_code", MAX_REASON_CODE_CHARS)
        if self.created_at_ms < 0 or self.updated_at_ms < self.created_at_ms:
            raise ValueError("attempt timestamps are inconsistent")
        if self.last_progress_at_ms is not None and not (
            self.created_at_ms <= self.last_progress_at_ms <= self.updated_at_ms
        ):
            raise ValueError("attempt progress time is inconsistent")

    phase: str = dataclass_field(
        init=False,
        compare=False,
        repr=False,
        default=None,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                generated="json_extract(lifecycle, '$.kind')", check="phase IN ({attempt_names})"
            ),
        },
    )
    lease_expires_at_ms: int | None = dataclass_field(
        init=False,
        compare=False,
        repr=False,
        default=None,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                generated="json_extract(lifecycle, '$.lease_expires_at_ms')",
                check="lease_expires_at_ms >= 0",
            ),
        },
    )
    backend_done: bool = dataclass_field(
        init=False,
        compare=False,
        repr=False,
        default=None,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                generated="CASE WHEN json_extract(lifecycle, '$.kind') IN ({terminal_attempt_names}) THEN 1 ELSE json_extract(lifecycle, '$.backend_done') END",
                check="backend_done IN (0,1)",
            ),
        },
    )
    process_dead: bool = dataclass_field(
        init=False,
        compare=False,
        repr=False,
        default=None,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                generated="CASE WHEN json_extract(lifecycle, '$.kind') IN ({terminal_attempt_names}) THEN 1 ELSE json_extract(lifecycle, '$.process_dead') END",
                check="process_dead IN (0,1)",
            ),
        },
    )
    phase_kind: str | None = dataclass_field(
        init=False,
        default=None,
        compare=False,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                generated=(
                    "CASE\n"
                    "       WHEN phase IN ({terminal_attempt_names}) THEN phase ELSE "
                    "'active' END"
                )
            ),
        },
    )
    active_required_status: str | None = dataclass_field(
        init=False,
        default=None,
        compare=False,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(generated="CASE WHEN phase_kind = 'active' THEN 'active' END"),
        },
    )
    checks = (
        (
            "last_progress_at_ms IS NULL OR\n"
            "           last_progress_at_ms BETWEEN created_at_ms AND updated"
            "_at_ms"
        ),
        (
            "(phase_kind = 'active' AND lease_expires_at_ms IS NOT NULL)\n"
            "       OR (phase_kind != 'active' AND lease_expires_at_ms IS NUL"
            "L\n"
            "           AND backend_done = 1 AND process_dead = 1)"
        ),
    )
    unique = (("execution_id", "attempt_ordinal", "owner_lookup", "phase_kind"),)

    @classmethod
    def references(cls):
        return (
            ForeignKey(
                ("execution_id",),
                ExecutionRecord,
                ("execution_id",),
                deferred=False,
                on_delete=None,
            ),
            ForeignKey(
                ("owner_lookup",),
                Participants,
                ("participant_lookup",),
                deferred=False,
                on_delete=None,
            ),
            ForeignKey(
                ("execution_id", "attempt_ordinal", "owner_lookup", "active_required_status"),
                ExecutionRecord,
                ("execution_id", "current_attempt_ordinal", "owner_lookup", "status"),
                deferred=True,
                on_delete=None,
            ),
        )

    @classmethod
    def triggers(cls):
        return {
            "attempt_insert_authorized": (
                """CREATE TRIGGER attempt_insert_authorized BEFORE INSERT ON attempts BEGIN
    SELECT RAISE(ABORT, 'attempt must be contiguous and within execution budget')
    WHERE NOT EXISTS (
        SELECT 1 FROM executions e WHERE e.execution_id = NEW.execution_id
        AND e.owner_lookup = NEW.owner_lookup AND NEW.attempt_ordinal <= e.max_attempts
        AND ((NEW.attempt_ordinal = 1 AND
              (e.status = 'pending' OR
               (e.status = 'active' AND e.current_attempt_ordinal = 1)))
          OR (NEW.attempt_ordinal > 1 AND
              (e.status = 'deferred' OR
               (e.status = 'active' AND e.current_attempt_ordinal = NEW.attempt_ordinal))))
        AND NEW.attempt_ordinal = 1 + coalesce(
            (SELECT max(a.attempt_ordinal) FROM attempts a
              WHERE a.execution_id = e.execution_id), 0));
    SELECT RAISE(ABORT, 'attempt generation must match owner counter')
    WHERE NOT EXISTS (SELECT 1 FROM owner_generations g
      WHERE g.owner_lookup = NEW.owner_lookup AND g.owner_thread = NEW.owner_thread
        AND g.generation = NEW.owner_generation);
    SELECT RAISE(ABORT, 'new attempt requires a fresh active fence')
    WHERE (json_extract(NEW.lifecycle, '$.kind')) != 'prompt_starting' OR NEW.revision != 1
       OR (CASE WHEN json_extract(NEW.lifecycle, '$.kind') IN ({terminal_attempt_names}) THEN 1 ELSE json_extract(NEW.lifecycle, '$.backend_done') END) != 0 OR (CASE WHEN json_extract(NEW.lifecycle, '$.kind') IN ({terminal_attempt_names}) THEN 1 ELSE json_extract(NEW.lifecycle, '$.process_dead') END) != 0;
    SELECT RAISE(ABORT, 'retry requires derived authorization and no current owner')
    WHERE NEW.attempt_ordinal > 1 AND (
      NOT EXISTS (SELECT 1 FROM retry_disposition_basis b
        WHERE b.execution_id = NEW.execution_id AND b.authorized = 1)
      OR EXISTS (SELECT 1 FROM current_executions p
        WHERE p.owner_lookup = NEW.owner_lookup AND p.execution_id IS NOT NULL));
    SELECT RAISE(ABORT, 'previous attempt must be dead with an older distinct fence')
    WHERE NEW.attempt_ordinal > 1 AND NOT EXISTS (
      SELECT 1 FROM attempts a WHERE a.execution_id = NEW.execution_id
      AND a.attempt_ordinal = NEW.attempt_ordinal - 1
      AND a.phase = 'attempt_failed' AND a.backend_done = 1 AND a.process_dead = 1
      AND NEW.owner_generation > a.owner_generation
      AND NEW.owner_token_digest != a.owner_token_digest);
END"""
            ),
            "attempt_phase_edge": (
                """CREATE TRIGGER attempt_phase_edge BEFORE UPDATE OF lifecycle ON attempts
WHEN (json_extract(NEW.lifecycle, '$.kind')) != (json_extract(OLD.lifecycle, '$.kind')) AND NOT ({attempt_edges})
BEGIN SELECT RAISE(ABORT, 'attempt phase transition is not declared'); END"""
            ),
            "attempt_frozen_facts": (
                """CREATE TRIGGER attempt_frozen_facts BEFORE UPDATE ON attempts
WHEN NEW.execution_id IS NOT OLD.execution_id
 OR NEW.attempt_ordinal != OLD.attempt_ordinal
 OR NEW.owner_lookup IS NOT OLD.owner_lookup OR NEW.owner_thread IS NOT OLD.owner_thread
 OR NEW.owner_generation != OLD.owner_generation
 OR NEW.owner_token_digest IS NOT OLD.owner_token_digest
 OR NEW.created_at_ms != OLD.created_at_ms
 OR NEW.revision != OLD.revision + 1
 OR NEW.updated_at_ms < OLD.updated_at_ms
 OR ((CASE WHEN json_extract(OLD.lifecycle, '$.kind') IN ({terminal_attempt_names}) THEN 1 ELSE json_extract(OLD.lifecycle, '$.backend_done') END) = 1 AND (CASE WHEN json_extract(NEW.lifecycle, '$.kind') IN ({terminal_attempt_names}) THEN 1 ELSE json_extract(NEW.lifecycle, '$.backend_done') END) = 0)
 OR ((CASE WHEN json_extract(OLD.lifecycle, '$.kind') IN ({terminal_attempt_names}) THEN 1 ELSE json_extract(OLD.lifecycle, '$.process_dead') END) = 1 AND (CASE WHEN json_extract(NEW.lifecycle, '$.kind') IN ({terminal_attempt_names}) THEN 1 ELSE json_extract(NEW.lifecycle, '$.process_dead') END) = 0)
 OR (OLD.last_progress_at_ms IS NOT NULL AND
      (NEW.last_progress_at_ms IS NULL OR NEW.last_progress_at_ms < OLD.last_progress_at_ms))
 OR ((json_extract(OLD.lifecycle, '$.lease_expires_at_ms')) IS NOT NULL AND (json_extract(NEW.lifecycle, '$.lease_expires_at_ms')) IS NOT NULL
      AND (json_extract(NEW.lifecycle, '$.lease_expires_at_ms')) < (json_extract(OLD.lifecycle, '$.lease_expires_at_ms')))
 OR ((json_extract(OLD.lifecycle, '$.kind')) IN ({terminal_attempt_names}) AND (json_extract(NEW.lifecycle, '$.kind')) = (json_extract(OLD.lifecycle, '$.kind')))
BEGIN SELECT RAISE(ABORT, 'attempt transition rewrites frozen facts'); END"""
            ),
            "attempt_delete_frozen": (
                """CREATE TRIGGER attempt_delete_frozen BEFORE DELETE ON attempts BEGIN
    SELECT RAISE(ABORT, 'attempt cannot be deleted');
END"""
            ),
        }


def attempt_retry_identity_allowed(
    prior: AttemptRecord,
    fresh: AttemptRecord,
    *,
    current_owner_generation: int,
    current_owner_thread: str,
    issued_token_digests: frozenset[str],
) -> bool:
    """Pure causal/new-attempt fence relation; SQL owns global uniqueness."""
    return (
        prior.lifecycle.failed
        and prior.lifecycle.backend_done
        and prior.lifecycle.process_dead
        and prior.lifecycle.lease_expires_at_ms is None
        and (prior.execution_id, prior.owner_lookup) == (fresh.execution_id, fresh.owner_lookup)
        and fresh.attempt_ordinal == prior.attempt_ordinal + 1
        and fresh.lifecycle.starting
        and fresh.revision == 1
        and not fresh.lifecycle.backend_done
        and not fresh.lifecycle.process_dead
        and fresh.owner_generation > prior.owner_generation
        and fresh.owner_generation == current_owner_generation
        and fresh.owner_thread == current_owner_thread
        and fresh.owner_token_digest != prior.owner_token_digest
        and fresh.owner_token_digest not in issued_token_digests
    )


@dataclass(frozen=True, slots=True)
class ReplayAssessments(CoordinatorTable, TypedTable):
    execution_id: str = dataclass_field(
        metadata={"snapshot_exclude": True, "sql": Column(primary_key=True)}
    )
    facts: ReplayFact = dataclass_field(
        metadata={"sql": Column(check="facts >= 0 AND facts <= 1023")}
    )
    replay_safe: bool = dataclass_field(metadata={"sql": Column()})
    side_effects_possible: bool = dataclass_field(metadata={"sql": Column()})
    revision: int = dataclass_field(metadata={"sql": Column(check="revision > 0")})

    def __post_init__(self) -> None:
        _validate_execution_id(self.execution_id)
        if self.revision <= 0:
            raise ValueError("replay revision must be positive")
        object.__setattr__(self, "facts", ReplayFact(self.facts))
        if int(self.facts) < 0 or int(self.facts) & ~APPROVED_REPLAY_FACT_MASK:
            raise ValueError("unrecognized replay fact bits")
        if self.facts != ReplayFact.NONE and self.replay_safe:
            raise IntegrityViolationError("an execution with ambiguity facts cannot be replay-safe")

    retry_authorized: int | None = dataclass_field(
        init=False,
        default=None,
        compare=False,
        metadata={
            "sql": Column(
                generated=(
                    "CASE WHEN facts = 0 AND replay_safe = 1 AND side_effects_possibl"
                    "e = 0\n"
                    "              THEN 1 ELSE 0 END"
                )
            )
        },
    )

    checks = ("facts = 0 OR replay_safe = 0",)

    unique = (("execution_id", "retry_authorized"),)

    @classmethod
    def references(cls):
        return (
            ForeignKey(
                ("execution_id",),
                ExecutionRecord,
                ("execution_id",),
                deferred=False,
                on_delete="RESTRICT",
            ),
        )

    @classmethod
    def triggers(cls):
        return {
            "replay_assessment_monotonic": (
                """CREATE TRIGGER replay_assessment_monotonic
BEFORE UPDATE ON replay_assessments
WHEN NEW.execution_id IS NOT OLD.execution_id
 OR NEW.revision != OLD.revision + 1
 OR (NEW.facts | OLD.facts) != NEW.facts
 OR (OLD.replay_safe = 0 AND NEW.replay_safe = 1)
 OR (OLD.side_effects_possible = 1 AND NEW.side_effects_possible = 0)
BEGIN
    SELECT RAISE(ABORT, 'replay assessment cannot erase ambiguity');
END"""
            ),
            "replay_assessment_delete_frozen": (
                """CREATE TRIGGER replay_assessment_delete_frozen BEFORE DELETE ON
replay_assessments BEGIN
    SELECT RAISE(ABORT, 'replay assessment cannot be deleted' );
END"""
            ),
            "failed_retry_partition_replay_insert": (
                """CREATE TRIGGER failed_retry_partition_replay_insert AFTER INSERT ON
replay_assessments
WHEN EXISTS (SELECT 1 FROM retry_disposition_basis b JOIN executions e
             ON e.execution_id = b.execution_id
             WHERE b.execution_id = NEW.execution_id AND b.authorized
             = 1
               AND e.status = 'failed' AND e.current_attempt_ordinal
               IS NOT NULL)
BEGIN SELECT RAISE(ABORT, 'authorized retry cannot settle failed' );
END"""
            ),
            "failed_retry_partition_replay_update": (
                """CREATE TRIGGER failed_retry_partition_replay_update AFTER UPDATE ON
replay_assessments
WHEN EXISTS (SELECT 1 FROM retry_disposition_basis b JOIN executions e
             ON e.execution_id = b.execution_id
             WHERE b.execution_id = NEW.execution_id AND b.authorized
             = 1
               AND e.status = 'failed' AND e.current_attempt_ordinal
               IS NOT NULL)
BEGIN SELECT RAISE(ABORT, 'authorized retry cannot settle failed' );
END"""
            ),
        }


def replay_transition_allowed(before: ReplayAssessments, after: ReplayAssessments) -> bool:
    """Facts accumulate; safety only decreases and ambiguity only increases."""
    return (
        before.execution_id == after.execution_id
        and after.revision == before.revision + 1
        and (after.facts | before.facts) == after.facts
        and (not after.replay_safe or before.replay_safe)
        and (not before.side_effects_possible or after.side_effects_possible)
    )


@dataclass(frozen=True, slots=True)
class ResponseObligation(CoordinatorTable, TypedTable, declared_name="obligations"):
    """One-to-one response obligation whose identity is its execution ID."""

    execution_id: str = dataclass_field(
        metadata={"snapshot_exclude": True, "sql": Column(primary_key=True)}
    )
    exact_target: str = dataclass_field(
        metadata={"sql": Column(check="length(exact_target) BETWEEN 1 AND 256")}
    )
    lifecycle: ResponseState = dataclass_field(metadata={"snapshot_exclude": True})
    reason_code: str | None = dataclass_field(
        metadata={
            "sql": Column(check="reason_code IS NULL OR length(reason_code) BETWEEN 1 AND 64")
        }
    )
    created_at_ms: int = dataclass_field(metadata={"sql": Column(check="created_at_ms >= 0")})
    updated_at_ms: int = dataclass_field(
        metadata={"sql": Column(check="updated_at_ms >= created_at_ms")}
    )
    revision: int = dataclass_field(metadata={"sql": Column(check="revision > 0")})

    @projected(view="snapshot", name="state")
    def snapshot_state(self):
        return self.lifecycle.declared_name

    @projected(view="snapshot", name="receipt_message_id")
    def snapshot_receipt_message_id(self):
        return self.lifecycle.receipt_message_id

    @projected(view="snapshot", name="receipt_seq")
    def snapshot_receipt_seq(self):
        return self.lifecycle.receipt_seq

    def __post_init__(self) -> None:
        _validate_execution_id(self.execution_id)
        _nonempty(self.exact_target, "exact_target")
        _bounded(self.exact_target, "exact_target", MAX_IDENTIFIER_CHARS)
        _optional_nonempty(self.reason_code, "reason_code", MAX_REASON_CODE_CHARS)
        _optional_nonempty(
            self.lifecycle.receipt_message_id, "receipt_message_id", MAX_IDENTIFIER_CHARS
        )
        if self.revision <= 0:
            raise ValueError("revision must be positive")
        if self.created_at_ms < 0 or self.updated_at_ms < self.created_at_ms:
            raise ValueError("obligation timestamps are inconsistent")

    state: str = dataclass_field(
        init=False,
        compare=False,
        repr=False,
        default=None,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                generated="json_extract(lifecycle, '$.kind')", check="state IN ({response_names})"
            ),
        },
    )
    receipt_message_id: str | None = dataclass_field(
        init=False,
        compare=False,
        repr=False,
        default=None,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                generated="json_extract(lifecycle, '$.message_id')",
                check=(
                    "\n"
                    "        receipt_message_id IS NULL OR length(receipt_message_id)"
                    " BETWEEN 1 AND 256\n"
                    "    "
                ),
            ),
        },
    )
    receipt_seq: int | None = dataclass_field(
        init=False,
        compare=False,
        repr=False,
        default=None,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                generated="json_extract(lifecycle, '$.seq')",
                check="receipt_seq IS NULL OR receipt_seq > 0",
            ),
        },
    )
    success_terminal: int | None = dataclass_field(
        init=False,
        default=None,
        compare=False,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                generated="CASE WHEN state IN ({successful_response_names}) THEN 1 ELSE 0 END"
            ),
        },
    )
    retryable: int | None = dataclass_field(
        init=False,
        default=None,
        compare=False,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                generated="CASE WHEN state IN ({retryable_response_names}) THEN 1 ELSE 0 END"
            ),
        },
    )
    intent_settled: int | None = dataclass_field(
        init=False,
        default=None,
        compare=False,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                generated="CASE WHEN state IN ({intent_response_names}) THEN 1 ELSE 0 END"
            ),
        },
    )
    receipt_settled: int | None = dataclass_field(
        init=False,
        default=None,
        compare=False,
        metadata={
            "snapshot_exclude": True,
            "sql": Column(generated="CASE WHEN state = 'published' THEN 1 ELSE 0 END"),
        },
    )
    checks = (
        "(receipt_message_id IS NULL) = (receipt_seq IS NULL)",
        (
            "\n"
            "        (state = 'published' AND receipt_message_id IS NOT NULL)"
            "\n"
            "        OR (state != 'published' AND receipt_message_id IS NULL)"
            "\n"
            "    "
        ),
    )
    unique = (
        ("execution_id", "success_terminal"),
        ("execution_id", "retryable"),
        ("execution_id", "intent_settled"),
        ("execution_id", "receipt_settled"),
    )

    @classmethod
    def references(cls):
        return (
            ForeignKey(
                ("execution_id",),
                ExecutionRecord,
                ("execution_id",),
                deferred=False,
                on_delete="RESTRICT",
            ),
        )

    @classmethod
    def triggers(cls):
        return {
            "failed_retry_partition_obligation_insert": (
                """CREATE TRIGGER failed_retry_partition_obligation_insert AFTER INSERT
ON obligations
WHEN EXISTS (SELECT 1 FROM retry_disposition_basis b JOIN executions e
             ON e.execution_id = b.execution_id
             WHERE b.execution_id = NEW.execution_id AND b.authorized
             = 1
               AND e.status = 'failed' AND e.current_attempt_ordinal
               IS NOT NULL)
BEGIN SELECT RAISE(ABORT, 'authorized retry cannot settle failed' );
END"""
            ),
            "failed_retry_partition_obligation_update": (
                """CREATE TRIGGER failed_retry_partition_obligation_update AFTER UPDATE
ON obligations
WHEN EXISTS (SELECT 1 FROM retry_disposition_basis b JOIN executions e
             ON e.execution_id = b.execution_id
             WHERE b.execution_id = NEW.execution_id AND b.authorized
             = 1
               AND e.status = 'failed' AND e.current_attempt_ordinal
               IS NOT NULL)
BEGIN SELECT RAISE(ABORT, 'authorized retry cannot settle failed' );
END"""
            ),
            "obligation_same_state_frozen": (
                """CREATE TRIGGER obligation_same_state_frozen BEFORE UPDATE ON obligations
WHEN json_extract(NEW.lifecycle, '$.kind') = json_extract(OLD.lifecycle, '$.kind')
BEGIN SELECT RAISE(ABORT, 'obligation disposition must change'); END"""
            ),
            "obligation_declared_edge": (
                """CREATE TRIGGER obligation_declared_edge
BEFORE UPDATE OF lifecycle ON obligations
WHEN json_extract(OLD.lifecycle, '$.kind') != json_extract(NEW.lifecycle, '$.kind') AND NOT ({obligation_edges}
)
BEGIN
    SELECT RAISE(ABORT, 'obligation state transition is not declared');
END"""
            ),
            "obligation_frozen_facts": (
                """CREATE TRIGGER obligation_frozen_facts
BEFORE UPDATE ON obligations
WHEN NEW.execution_id IS NOT OLD.execution_id
 OR NEW.exact_target IS NOT OLD.exact_target
 OR NEW.created_at_ms != OLD.created_at_ms
 OR NEW.revision != OLD.revision + 1
 OR NEW.updated_at_ms < OLD.updated_at_ms
BEGIN
    SELECT RAISE(ABORT, 'obligation transition rewrites frozen facts');
END"""
            ),
            "obligation_target_matches_execution_insert": (
                """CREATE TRIGGER obligation_target_matches_execution_insert
BEFORE INSERT ON obligations
WHEN NOT EXISTS (
    SELECT 1 FROM executions WHERE execution_id = NEW.execution_id
      AND origin = 'wire' AND exact_target = NEW.exact_target
)
BEGIN
    SELECT RAISE(ABORT, 'obligation target does not match wire execution');
END"""
            ),
            "obligation_target_matches_execution_update": (
                """CREATE TRIGGER obligation_target_matches_execution_update
BEFORE UPDATE OF exact_target ON obligations
WHEN NOT EXISTS (
    SELECT 1 FROM executions WHERE execution_id = NEW.execution_id
      AND origin = 'wire' AND exact_target = NEW.exact_target
)
BEGIN
    SELECT RAISE(ABORT, 'obligation target does not match wire execution');
END"""
            ),
            "published_obligation_receipt_is_frozen": (
                """CREATE TRIGGER published_obligation_receipt_is_frozen
BEFORE UPDATE OF lifecycle ON obligations
WHEN json_extract(OLD.lifecycle, '$.kind') = 'published' AND (
    json_extract(NEW.lifecycle, '$.message_id') IS NOT json_extract(OLD.lifecycle, '$.message_id')
    OR json_extract(NEW.lifecycle, '$.seq') IS NOT json_extract(OLD.lifecycle, '$.seq')
)
BEGIN
    SELECT RAISE(ABORT, 'published obligation receipt is frozen');
END"""
            ),
            "obligation_publication_transition": (
                """CREATE TRIGGER obligation_publication_transition
BEFORE UPDATE ON obligations
WHEN json_extract(NEW.lifecycle, '$.kind') IN ({required_intent_response_names})
BEGIN
    SELECT RAISE(ABORT, 'publishing obligation requires intent')
    WHERE NOT EXISTS (
        SELECT 1 FROM publication_intents
        WHERE execution_id = NEW.execution_id AND exact_target = NEW.exact_target
    );
    SELECT RAISE(ABORT, 'published obligation requires matching receipt')
    WHERE json_extract(NEW.lifecycle, '$.kind') = 'published' AND NOT EXISTS (
        SELECT 1 FROM publication_receipts
        WHERE execution_id = NEW.execution_id
          AND message_id = json_extract(NEW.lifecycle, '$.message_id') AND seq = json_extract(NEW.lifecycle, '$.seq')
    );
END"""
            ),
            "obligation_publication_insert": (
                """CREATE TRIGGER obligation_publication_insert
BEFORE INSERT ON obligations
WHEN json_extract(NEW.lifecycle, '$.kind') IN ({required_intent_response_names})
BEGIN
    SELECT RAISE(ABORT, 'publication obligation starts pending');
END"""
            ),
            "obligation_receipt_requires_published": (
                """CREATE TRIGGER obligation_receipt_requires_published
BEFORE UPDATE OF lifecycle ON obligations
WHEN EXISTS (SELECT 1 FROM publication_receipts WHERE execution_id = OLD.execution_id)
 AND json_extract(NEW.lifecycle, '$.kind') != 'published'
BEGIN
    SELECT RAISE(ABORT, 'frozen receipt requires published obligation');
END"""
            ),
            "obligation_state_with_intent": (
                """CREATE TRIGGER obligation_state_with_intent
BEFORE UPDATE OF lifecycle ON obligations
WHEN EXISTS (SELECT 1 FROM publication_intents WHERE execution_id = OLD.execution_id)
  AND json_extract(NEW.lifecycle, '$.kind') NOT IN ({intent_response_names})
BEGIN
    SELECT RAISE(ABORT, 'frozen intent cannot return to pending obligation');
END"""
            ),
            "obligation_target_frozen": (
                """CREATE TRIGGER obligation_target_frozen
BEFORE UPDATE OF exact_target ON obligations
WHEN EXISTS (SELECT 1 FROM publication_intents WHERE execution_id = OLD.execution_id)
BEGIN
    SELECT RAISE(ABORT, 'publication target is frozen');
END"""
            ),
            "obligation_delete_frozen": (
                """CREATE TRIGGER obligation_delete_frozen BEFORE DELETE ON obligations BEGIN
    SELECT RAISE(ABORT, 'obligation cannot be deleted');
END"""
            ),
        }


@dataclass(frozen=True, slots=True)
class PublicationIntents(CoordinatorTable, TypedTable):
    execution_id: str = dataclass_field(
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                primary_key=True,
                storage=ExactStorage,
                check="""typeof(execution_id) = 'text' AND length(execution_id) BETWEEN 1 AND 256
             AND instr(execution_id, ':' ) = 0""",
            ),
        }
    )
    sender: str = dataclass_field(
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                storage=ExactStorage,
                check="typeof(sender) = 'text' AND length(sender) BETWEEN 1 AND 256",
            ),
        }
    )
    exact_target: str = dataclass_field(
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                storage=ExactStorage,
                check="""
      typeof(exact_target) = 'text' AND length(exact_target) BETWEEN 1 AND 256""",
            ),
        }
    )
    message_type: MessageType = dataclass_field(
        metadata={
            "snapshot_exclude": True,
            "sql": Column(storage=ExactStorage, check="typeof(message_type) = 'text'"),
        }
    )
    notice: bool = dataclass_field(metadata={"snapshot_exclude": True, "sql": Column()})
    timestamp: float = dataclass_field(
        metadata={
            "snapshot_exclude": True,
            "sql": Column(check="timestamp >= 0 AND timestamp < 1.0e999"),
        }
    )
    payload: str = dataclass_field(
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                storage=ExactStorage,
                check="""
        typeof(payload) = 'text' AND length(CAST(payload AS BLOB)) BETWEEN 1 AND 120000
    """,
            ),
        }
    )
    payload_digest: str = dataclass_field(
        metadata={
            "sql": Column(
                storage=ExactStorage,
                check="""
      typeof(payload_digest) = 'text' AND length(payload_digest) BETWEEN 1 AND 256""",
            )
        }
    )
    publication_key: str = dataclass_field(
        metadata={
            "sql": Column(
                storage=ExactStorage,
                unique=True,
                check="""
      typeof(publication_key) = 'text' AND length(publication_key) BETWEEN 1 AND 256
    """,
            )
        }
    )
    expected_message_id: str = dataclass_field(
        metadata={
            "snapshot_exclude": True,
            "sql": Column(
                storage=ExactStorage,
                unique=True,
                check="""
      typeof(expected_message_id) = 'text' AND length(expected_message_id) BETWEEN 1 AND
      256
    """,
            ),
        }
    )

    def __post_init__(self) -> None:
        for field, value in (
            ("execution_id", self.execution_id),
            ("sender", self.sender),
            ("exact_target", self.exact_target),
            ("payload", self.payload),
            ("payload_digest", self.payload_digest),
            ("publication_key", self.publication_key),
            ("expected_message_id", self.expected_message_id),
        ):
            _nonempty(value, field)
        _validate_execution_id(self.execution_id)
        for field, value in (
            ("sender", self.sender),
            ("payload_digest", self.payload_digest),
            ("expected_message_id", self.expected_message_id),
        ):
            _bounded(value, field, MAX_IDENTIFIER_CHARS)
        object.__setattr__(self, "message_type", MessageType(self.message_type))
        timestamp = float(self.timestamp)
        object.__setattr__(self, "timestamp", 0.0 if timestamp == 0.0 else timestamp)
        if not math.isfinite(self.timestamp) or self.timestamp < 0:
            raise ValueError("publication timestamp must be finite and non-negative")
        object.__setattr__(self, "notice", bool(self.notice))
        if len(self.payload.encode("utf-8")) > MAX_PUBLICATION_PAYLOAD_BYTES:
            raise ValueError("publication payload exceeds byte limit")
        if hashlib.sha256(self.payload.encode()).hexdigest() != self.payload_digest:
            raise IntegrityViolationError("publication payload digest does not match")
        if self.publication_key != canonical_publication_key(self.execution_id, self.exact_target):
            raise IntegrityViolationError("publication key is not canonical")
        if self.expected_message.message_id != self.expected_message_id:
            raise IntegrityViolationError("expected message id does not match Message authority")

    @projected(view="snapshot")
    def payload_utf8_bytes(self):
        return len(self.payload.encode("utf-8"))

    @property
    def expected_message(self) -> Message:
        """Reconstruct through the existing message identity authority."""
        return Message(
            sender=self.sender,
            target=self.exact_target,
            body=self.payload,
            type=self.message_type,
            timestamp=self.timestamp,
            notice=self.notice,
        )

    obligation_intent_required: int | None = dataclass_field(
        init=False, compare=False, metadata={"sql": Column(generated="1")}
    )

    checks = ("publication_key = 'publication:v1:' || execution_id || ':' || exact_target",)

    @classmethod
    def references(cls):
        return (
            ForeignKey(
                ("execution_id",),
                ExecutionRecord,
                ("execution_id",),
                deferred=False,
                on_delete="RESTRICT",
            ),
            ForeignKey(
                ("execution_id", "obligation_intent_required"),
                ResponseObligation,
                ("execution_id", "intent_settled"),
                deferred=True,
                on_delete=None,
            ),
        )

    @classmethod
    def triggers(cls):
        return {
            "publication_intent_envelope_authority": (
                """CREATE TRIGGER publication_intent_envelope_authority BEFORE INSERT ON
publication_intents
WHEN coordination_validate_publication_intent(
  NEW.execution_id, NEW.sender, NEW.exact_target, NEW.message_type,
  NEW.notice,
  NEW.timestamp, NEW.payload, NEW.payload_digest, NEW.publication_key,
  NEW.expected_message_id) != 1
BEGIN SELECT RAISE(ABORT, 'publication intent envelope is invalid' );
END"""
            ),
            "publication_intent_requires_obligation": (
                """CREATE TRIGGER publication_intent_requires_obligation
BEFORE INSERT ON publication_intents
WHEN NOT EXISTS (
    SELECT 1 FROM obligations WHERE execution_id = NEW.execution_id
      AND exact_target = NEW.exact_target AND state = 'pending'
)
BEGIN
    SELECT RAISE(ABORT, 'publication intent requires matching pending obligation');
END"""
            ),
            "publication_intent_update_frozen": (
                """CREATE TRIGGER publication_intent_update_frozen
BEFORE UPDATE ON publication_intents
BEGIN
    SELECT RAISE(ABORT, 'publication intent is frozen');
END"""
            ),
            "publication_intent_delete_frozen": (
                """CREATE TRIGGER publication_intent_delete_frozen
BEFORE DELETE ON publication_intents
BEGIN
    SELECT RAISE(ABORT, 'publication intent is frozen');
END"""
            ),
        }


@dataclass(frozen=True, slots=True)
class PublicationReceipt(TypedRow):
    execution_id: str = dataclass_field(metadata={"snapshot_exclude": True})
    publication_key: str = dataclass_field(metadata={"snapshot_exclude": True})
    seq: int
    message_id: str
    sender: str = dataclass_field(metadata={"snapshot_exclude": True})
    exact_target: str = dataclass_field(metadata={"snapshot_exclude": True})
    message_type: MessageType = dataclass_field(metadata={"snapshot_exclude": True})
    notice: bool = dataclass_field(metadata={"snapshot_exclude": True})
    timestamp: float = dataclass_field(metadata={"snapshot_exclude": True})
    payload_digest: str = dataclass_field(metadata={"snapshot_exclude": True})

    def __post_init__(self) -> None:
        for field, value in (
            ("execution_id", self.execution_id),
            ("publication_key", self.publication_key),
            ("message_id", self.message_id),
            ("sender", self.sender),
            ("exact_target", self.exact_target),
            ("payload_digest", self.payload_digest),
        ):
            _nonempty(value, field)
        _validate_execution_id(self.execution_id)
        for field, value in (
            ("sender", self.sender),
            ("message_id", self.message_id),
            ("payload_digest", self.payload_digest),
        ):
            _bounded(value, field, MAX_IDENTIFIER_CHARS)
        object.__setattr__(self, "message_type", MessageType(self.message_type))
        if self.publication_key != canonical_publication_key(self.execution_id, self.exact_target):
            raise IntegrityViolationError("receipt publication key is not canonical")
        timestamp = float(self.timestamp)
        object.__setattr__(self, "timestamp", 0.0 if timestamp == 0.0 else timestamp)
        if not math.isfinite(self.timestamp) or self.timestamp < 0:
            raise ValueError("receipt timestamp must be finite and non-negative")
        object.__setattr__(self, "notice", bool(self.notice))
        if self.seq <= 0:
            raise ValueError("receipt seq must be positive")


@dataclass(frozen=True, slots=True)
class ConnectivityFacet(CoordinatorTable, TypedTable, declared_name="connectivity"):
    execution_id: str = dataclass_field(
        metadata={"snapshot_exclude": True, "sql": Column(primary_key=True)}
    )
    owner: OwnerConnectivity
    acp_client: ACPClientConnectivity
    revision: int = dataclass_field(metadata={"sql": Column(check="revision>0")})
    observed_at_ms: int = dataclass_field(metadata={"sql": Column(check="observed_at_ms>=0")})

    def __post_init__(self) -> None:
        _validate_execution_id(self.execution_id)
        object.__setattr__(self, "owner", OwnerConnectivity(self.owner))
        object.__setattr__(self, "acp_client", ACPClientConnectivity(self.acp_client))
        if self.revision <= 0 or self.observed_at_ms < 0:
            raise ValueError("revision must be positive and observed_at_ms non-negative")

    @classmethod
    def references(cls):
        return (
            ForeignKey(
                ("execution_id",),
                ExecutionRecord,
                ("execution_id",),
                deferred=False,
                on_delete="RESTRICT",
            ),
        )

    @classmethod
    def triggers(cls):
        return {
            "connectivity_observation_monotonic": (
                """CREATE TRIGGER connectivity_observation_monotonic
    BEFORE UPDATE ON connectivity
    WHEN NEW.execution_id IS NOT OLD.execution_id
     OR NEW.revision != OLD.revision + 1
     OR NEW.observed_at_ms < OLD.observed_at_ms
    BEGIN
        SELECT RAISE(ABORT, 'connectivity revision or observation regressed');
    END"""
            ),
            "connectivity_delete_frozen": (
                """CREATE TRIGGER connectivity_delete_frozen BEFORE DELETE ON connectivity BEGIN
        SELECT RAISE(ABORT, 'connectivity cannot be deleted');
    END"""
            ),
        }


@dataclass(frozen=True, slots=True)
class RecoveryAudit(CoordinatorTable, TypedTable):
    execution_id: str = dataclass_field(metadata={"snapshot_exclude": True})
    kind: type[RecoveryCondition] = dataclass_field(
        metadata={"sql": Column(check="kind IN ({recovery_names})")}
    )
    reason_code: str = dataclass_field(
        metadata={"sql": Column(check="length(reason_code) BETWEEN 1 AND 64")}
    )
    sanitized_detail: str | None = dataclass_field(
        metadata={
            "sql": Column(
                check=(
                    "sanitized_detail IS NULL OR length(sanitized_detail)<="
                    f"{MAX_SANITIZED_DETAIL_CHARS}"
                )
            )
        }
    )
    attempt: int = dataclass_field(metadata={"sql": Column(check="attempt>0")})
    elapsed_ms: int = dataclass_field(metadata={"sql": Column(check="elapsed_ms>=0")})
    observed_at_ms: int = dataclass_field(metadata={"sql": Column(check="observed_at_ms>=0")})
    audit_id: int | None = dataclass_field(
        default=None,
        metadata={"sql": Column(primary_key=True, auto_increment=True), "snapshot_exclude": True},
    )
    indexes = (Index(("execution_id", "audit_id")),)

    @classmethod
    def references(cls):
        return (
            ForeignKey(
                ("execution_id",),
                ExecutionRecord,
                ("execution_id",),
                deferred=False,
                on_delete="RESTRICT",
            ),
            ForeignKey(
                ("execution_id", "attempt"),
                AttemptRecord,
                ("execution_id", "attempt_ordinal"),
                deferred=False,
                on_delete=None,
            ),
        )

    @classmethod
    def triggers(cls):
        return {
            "recovery_audit_update_frozen": (
                """CREATE TRIGGER recovery_audit_update_frozen
BEFORE UPDATE ON recovery_audit
BEGIN
    SELECT RAISE(ABORT, 'recovery audit is append-only');
END"""
            ),
            "recovery_audit_delete_frozen": (
                """CREATE TRIGGER recovery_audit_delete_frozen
BEFORE DELETE ON recovery_audit
BEGIN
    SELECT RAISE(ABORT, 'recovery audit is append-only');
END"""
            ),
        }

    def __post_init__(self) -> None:
        _validate_execution_id(self.execution_id)
        _nonempty(self.reason_code, "reason_code")
        _bounded(self.reason_code, "reason_code", MAX_REASON_CODE_CHARS)
        _bounded(self.sanitized_detail, "sanitized_detail", MAX_SANITIZED_DETAIL_CHARS)
        if self.attempt <= 0 or self.elapsed_ms < 0 or self.observed_at_ms < 0:
            raise ValueError("recovery attempt/timestamps are invalid")


def retry_disposition_authorized(
    execution: ExecutionRecord,
    replay: ReplayAssessments | None,
    obligation: ResponseObligation | None,
) -> bool:
    """One derived terminal-failure partition; SQL view owns the same relation."""
    return (
        execution.lifecycle.current_attempt_ordinal is not None
        and execution.lifecycle.current_attempt_ordinal < execution.max_attempts
        and replay is not None
        and replay.replay_safe
        and replay.facts == ReplayFact.NONE
        and not replay.side_effects_possible
        and (
            execution.origin is not ExecutionOrigin.WIRE
            or (obligation is not None and obligation.lifecycle.retryable)
        )
    )


@dataclass(frozen=True, slots=True)
class RecoverySnapshot:
    """Deterministic, token-free primitive projection of one execution."""

    execution: ExecutionRecord
    attempt: AttemptRecord | None
    assignments: tuple[WakeAssignment, ...] = dataclass_field(metadata={"wire_name": "claims"})
    links: tuple[ExecutionAssignmentLink, ...] = dataclass_field(
        metadata={"snapshot_name": "execution_claims"}
    )
    replay: ReplayAssessments | None
    obligation: ResponseObligation | None
    publication_intent: PublicationIntents | None
    publication_receipt: PublicationReceipt | None
    connectivity: ConnectivityFacet | None
    last_recovery: RecoveryAudit | None
    current_execution_id: str | None
    current_attempt_ordinal: int | None
    pointer_revision: int
    is_current: bool
    snapshot_version: int = COORDINATION_SNAPSHOT_VERSION

    def __post_init__(self) -> None:
        if self.pointer_revision < 0:
            raise ValueError("pointer_revision cannot be negative")
        if self.snapshot_version != COORDINATION_SNAPSHOT_VERSION:
            raise ValueError("unsupported snapshot version")
        self.validate_membership()
        self.validate_attempt_identity()
        self.validate_current_pointer()
        self.validate_response_route()
        self.validate_publication_receipt()
        self.execution.lifecycle.validate_snapshot(
            self, retry_disposition_authorized(self.execution, self.replay, self.obligation)
        )

    def validate_membership(self) -> None:
        execution = self.execution
        execution_id = execution.execution_id
        related = (
            self.replay,
            self.obligation,
            self.publication_intent,
            self.publication_receipt,
            self.connectivity,
            self.last_recovery,
        )
        if any(
            record is not None and record.execution_id != execution_id for record in related
        ) or any(
            assignment.lifecycle.execution_id != execution_id for assignment in self.assignments
        ):
            raise IntegrityViolationError("snapshot records belong to another execution")
        assignment_ids = tuple(assignment.assignment_id for assignment in self.assignments)
        if (
            tuple(link.assignment_id for link in self.links) != assignment_ids
            or tuple(link.ordinal for link in self.links) != tuple(range(len(self.assignments)))
            or any(link.execution_id != execution_id for link in self.links)
        ):
            raise IntegrityViolationError(
                "snapshot execution-claim links disagree with ordered claims"
            )
        if len(assignment_ids) != len(set(assignment_ids)) or any(
            assignment.recipient_lookup != execution.owner_lookup for assignment in self.assignments
        ):
            raise IntegrityViolationError("snapshot claims have duplicate IDs or wrong owner")
        assignment_kind = execution.lifecycle.assignment_state()
        if any(
            type(assignment.lifecycle) is not assignment_kind for assignment in self.assignments
        ):
            raise IntegrityViolationError("snapshot claims disagree with execution disposition")

    def validate_attempt_identity(self) -> None:
        execution = self.execution
        execution_id = execution.execution_id
        attempt = self.attempt
        if (execution.lifecycle.current_attempt_ordinal is None) != (attempt is None):
            raise IntegrityViolationError("snapshot attempt does not match execution reference")
        if attempt is not None and (
            (attempt.execution_id, attempt.attempt_ordinal, attempt.owner_lookup)
            != (execution_id, execution.lifecycle.current_attempt_ordinal, execution.owner_lookup)
        ):
            raise IntegrityViolationError("snapshot attempt identity/owner mismatch")
        if attempt is not None and not execution.lifecycle.accepts_attempt(attempt.lifecycle):
            raise IntegrityViolationError("snapshot status/attempt phase mismatch")

    def validate_current_pointer(self) -> None:
        execution = self.execution
        execution_id = execution.execution_id
        attempt = self.attempt
        if (self.current_execution_id is None) != (self.current_attempt_ordinal is None):
            raise IntegrityViolationError("snapshot pointer tuple is incomplete")
        if self.is_current != (
            self.current_execution_id == execution_id
            and self.current_attempt_ordinal == execution.lifecycle.current_attempt_ordinal
            and execution.lifecycle.active
            and attempt is not None
            and not attempt.lifecycle.terminal
        ):
            raise IntegrityViolationError("snapshot current pointer is inconsistent")

    def validate_response_route(self) -> None:
        execution = self.execution
        if execution.origin is ExecutionOrigin.WIRE:
            if not self.assignments or self.obligation is None:
                raise IntegrityViolationError("wire snapshots require an obligation")
        elif self.obligation is not None or self.assignments:
            raise IntegrityViolationError("claimless snapshots cannot have claims or obligation")
        expected_target = execution.exact_target
        target_records = (
            *(assignment.lifecycle for assignment in self.assignments),
            self.obligation,
            self.publication_intent,
            self.publication_receipt,
        )
        if any(
            record is not None and record.exact_target != expected_target
            for record in target_records
        ):
            raise IntegrityViolationError("snapshot exact targets disagree")

    def validate_publication_receipt(self) -> None:
        obligation = self.obligation
        intent = self.publication_intent
        receipt = self.publication_receipt
        if obligation is not None:
            obligation.lifecycle.validate_publication(intent, receipt)
        elif intent is not None or receipt is not None:
            raise IntegrityViolationError("publication requires an obligation")
        if receipt is not None:
            if intent is None or obligation is None:
                raise IntegrityViolationError("published receipt requires intent and obligation")
            if (obligation.lifecycle.receipt_message_id, obligation.lifecycle.receipt_seq) != (
                receipt.message_id,
                receipt.seq,
            ):
                raise IntegrityViolationError("obligation receipt does not match publication")
            expected_envelope = (
                intent.publication_key,
                intent.expected_message_id,
                intent.sender,
                intent.exact_target,
                intent.message_type,
                intent.notice,
                intent.timestamp,
                intent.payload_digest,
            )
            actual_envelope = (
                receipt.publication_key,
                receipt.message_id,
                receipt.sender,
                receipt.exact_target,
                receipt.message_type,
                receipt.notice,
                receipt.timestamp,
                receipt.payload_digest,
            )
            if actual_envelope != expected_envelope:
                raise IntegrityViolationError("receipt envelope does not match frozen intent")

    @projected(view="snapshot")
    def can_retry(self) -> bool:
        execution = self.execution
        attempt = self.attempt
        return (
            retry_disposition_authorized(execution, self.replay, self.obligation)
            and execution.lifecycle.retry
            and attempt is not None
            and attempt.lifecycle.failed
            and attempt.lifecycle.backend_done
            and attempt.lifecycle.process_dead
            and attempt.lifecycle.lease_expires_at_ms is None
            and not self.is_current
        )


@dataclass(frozen=True, kw_only=True)
class SchemaMeta(CoordinatorTable, TypedTable):
    singleton: int = dataclass_field(
        metadata={"sql": Column(primary_key=True, check="singleton = 1")}
    )
    schema_version: int
    snapshot_version: int

    @classmethod
    def triggers(cls):
        return {
            "schema_meta_update_frozen": (
                """CREATE TRIGGER schema_meta_update_frozen BEFORE UPDATE ON schema_meta BEGIN
    SELECT RAISE(ABORT, 'schema metadata is immutable');
END"""
            ),
            "schema_meta_delete_frozen": (
                """CREATE TRIGGER schema_meta_delete_frozen BEFORE DELETE ON schema_meta BEGIN
    SELECT RAISE(ABORT, 'schema metadata cannot be deleted');
END"""
            ),
        }


@dataclass(frozen=True, kw_only=True)
class Participants(CoordinatorTable, TypedTable):
    participant_lookup: str = dataclass_field(
        metadata={
            "sql": Column(
                primary_key=True,
                check="\n        length(participant_lookup) BETWEEN 1 AND 256\n    ",
            )
        }
    )
    display_name: str = dataclass_field(
        metadata={"sql": Column(check="length(display_name) BETWEEN 1 AND 256")}
    )
    committed: bool

    @classmethod
    def triggers(cls):
        return {
            "participant_identity_immutable": (
                """CREATE TRIGGER participant_identity_immutable
BEFORE UPDATE OF participant_lookup ON participants
BEGIN
    SELECT RAISE(ABORT, 'participant lookup identity is immutable');
END"""
            ),
            "participant_commit_monotonic": (
                """CREATE TRIGGER participant_commit_monotonic
BEFORE UPDATE OF committed ON participants
WHEN OLD.committed = 1 AND NEW.committed = 0
BEGIN
    SELECT RAISE(ABORT, 'participant commitment cannot be revoked');
END"""
            ),
            "participant_identity_delete_frozen": (
                """CREATE TRIGGER participant_identity_delete_frozen
BEFORE DELETE ON participants
BEGIN
    SELECT RAISE(ABORT, 'participant lookup identity cannot be deleted');
END"""
            ),
        }


@dataclass(frozen=True, kw_only=True)
class ParticipantAliases(CoordinatorTable, TypedTable):
    alias: str = dataclass_field(
        metadata={"sql": Column(primary_key=True, check="length(alias) BETWEEN 1 AND 256")}
    )
    participant_lookup: str
    renamed_at_ms: int = dataclass_field(metadata={"sql": Column(check="renamed_at_ms >= 0")})

    @classmethod
    def references(cls):
        return (
            ForeignKey(
                ("participant_lookup",),
                Participants,
                ("participant_lookup",),
                deferred=False,
                on_delete=None,
            ),
        )

    @classmethod
    def triggers(cls):
        return {
            "participant_alias_identity_immutable": (
                """CREATE TRIGGER participant_alias_identity_immutable
BEFORE UPDATE ON participant_aliases
BEGIN
    SELECT RAISE(ABORT, 'participant alias identity is immutable');
END"""
            ),
            "participant_alias_delete_frozen": (
                """CREATE TRIGGER participant_alias_delete_frozen
BEFORE DELETE ON participant_aliases
BEGIN
    SELECT RAISE(ABORT, 'participant alias cannot be deleted');
END"""
            ),
        }


@dataclass(frozen=True, kw_only=True)
class OwnerGenerations(CoordinatorTable, TypedTable):
    owner_lookup: str = dataclass_field(metadata={"sql": Column(primary_key=True)})
    owner_thread: str = dataclass_field(
        metadata={"sql": Column(check="length(owner_thread) BETWEEN 1 AND 256")}
    )
    generation: int = dataclass_field(metadata={"sql": Column(check="generation > 0")})

    @classmethod
    def references(cls):
        return (
            ForeignKey(
                ("owner_lookup",),
                Participants,
                ("participant_lookup",),
                deferred=False,
                on_delete=None,
            ),
        )

    @classmethod
    def triggers(cls):
        return {
            "owner_generation_delete_frozen": (
                """CREATE TRIGGER owner_generation_delete_frozen BEFORE DELETE ON
owner_generations BEGIN
    SELECT RAISE(ABORT, 'owner generation counter cannot be deleted'
    );
END"""
            ),
            "owner_generation_monotonic": (
                """CREATE TRIGGER owner_generation_monotonic BEFORE UPDATE ON owner_generations
WHEN NEW.owner_lookup IS NOT OLD.owner_lookup
 OR NEW.generation != OLD.generation + 1
 OR EXISTS (SELECT 1 FROM attempts WHERE owner_lookup = OLD.owner_lookup
            AND owner_generation = OLD.generation
            AND phase NOT IN ({terminal_attempt_names}))
BEGIN SELECT RAISE(ABORT, 'owner generation cannot advance with active attempts'); END"""
            ),
        }


@dataclass(frozen=True, kw_only=True)
class WakeClaims(CoordinatorTable, TypedTable):
    claim_id: str = dataclass_field(
        metadata={"sql": Column(primary_key=True, check="length(claim_id) BETWEEN 1 AND 256")}
    )
    recipient: str = dataclass_field(
        metadata={"sql": Column(check="length(recipient) BETWEEN 1 AND 256")}
    )
    recipient_lookup: str
    wire_seq: int = dataclass_field(metadata={"sql": Column(check="wire_seq > 0")})
    message_id: str = dataclass_field(
        metadata={"sql": Column(check="length(message_id) BETWEEN 1 AND 256")}
    )
    exact_target: str | None = dataclass_field(
        metadata={
            "sql": Column(check="exact_target IS NULL OR length(exact_target) BETWEEN 1 AND 256")
        }
    )
    audience: str = dataclass_field(
        metadata={"sql": Column(check="audience IN ('direct', 'mentioned', 'collective')")}
    )
    wake_mode: str = dataclass_field(metadata={"sql": Column(check="wake_mode IN ({wake_names})")})
    triage_verdict: str | None = dataclass_field(
        metadata={
            "sql": Column(check="triage_verdict IS NULL OR triage_verdict IN ('ignore', 'engage')")
        }
    )
    disposition: str = dataclass_field(
        metadata={"sql": Column(check="disposition IN ({assignment_names})")}
    )
    resolver_version: str = dataclass_field(
        metadata={"sql": Column(check="length(resolver_version) BETWEEN 1 AND 256")}
    )
    policy_version: str = dataclass_field(
        metadata={"sql": Column(check="length(policy_version) BETWEEN 1 AND 256")}
    )
    accepted_at_ms: int = dataclass_field(metadata={"sql": Column(check="accepted_at_ms >= 0")})
    updated_at_ms: int = dataclass_field(
        metadata={"sql": Column(check="updated_at_ms >= accepted_at_ms")}
    )
    revision: int = dataclass_field(metadata={"sql": Column(check="revision > 0")})
    execution_id: str | None
    claim_status_kind: str | None = dataclass_field(
        init=False,
        default=None,
        compare=False,
        metadata={
            "sql": Column(generated="CASE WHEN execution_id IS NOT NULL THEN disposition END")
        },
    )
    checks = (
        (
            "(execution_id IS NULL AND exact_target IS NULL)\n"
            "        OR (execution_id IS NOT NULL AND exact_target IS NOT NUL"
            "L)"
        ),
        (
            "COALESCE((\n"
            "        (disposition = 'passive' AND wake_mode = 'passive'\n"
            "         AND triage_verdict IS NULL AND execution_id IS NULL)\n"
            "        OR\n"
            "        (disposition = 'triage_pending' AND wake_mode = 'bounded"
            "_triage'\n"
            "         AND triage_verdict IS NULL AND execution_id IS NULL)\n"
            "        OR\n"
            "        (disposition = 'ignored' AND wake_mode = 'bounded_triage"
            "'\n"
            "         AND triage_verdict = 'ignore' AND execution_id IS NULL)"
            "\n"
            "        OR\n"
            "        (disposition = 'full_pending' AND wake_mode = 'full'\n"
            "         AND triage_verdict IS NULL AND execution_id IS NULL)\n"
            "        OR\n"
            "        (disposition = 'engaged' AND execution_id IS NOT NULL AN"
            "D (\n"
            "            (wake_mode = 'bounded_triage' AND triage_verdict = '"
            "engage')\n"
            "            OR (wake_mode = 'full' AND triage_verdict IS NULL)\n"
            "        ))\n"
            "        OR\n"
            "        (disposition IN ('deferred', 'failed') AND wake_mode != "
            "'passive' AND (\n"
            "            (execution_id IS NULL AND triage_verdict IS NULL)\n"
            "            OR (execution_id IS NOT NULL AND (\n"
            "                (wake_mode = 'bounded_triage' AND triage_verdict"
            " = 'engage')\n"
            "                OR (wake_mode = 'full' AND triage_verdict IS NUL"
            "L)\n"
            "            ))\n"
            "        ))\n"
            "        OR\n"
            "        (disposition = 'completed' AND execution_id IS NOT NULL "
            "AND (\n"
            "            (wake_mode = 'bounded_triage' AND triage_verdict = '"
            "engage')\n"
            "            OR (wake_mode = 'full' AND triage_verdict IS NULL)\n"
            "        ))\n"
            "    ), 0)"
        ),
    )
    unique = (("recipient_lookup", "wire_seq"), ("claim_id", "execution_id"))
    indexes = (Index(("execution_id",), unique=False, where=None),)

    @classmethod
    def references(cls):
        return (
            ForeignKey(
                ("recipient_lookup",),
                Participants,
                ("participant_lookup",),
                deferred=False,
                on_delete=None,
            ),
            ForeignKey(
                ("execution_id",),
                ExecutionRecord,
                ("execution_id",),
                deferred=False,
                on_delete=None,
            ),
            ForeignKey(
                ("execution_id", "claim_id"),
                ExecutionAssignmentLink,
                ("execution_id", "assignment_id"),
                deferred=True,
                on_delete=None,
            ),
            ForeignKey(
                ("execution_id", "claim_status_kind"),
                ExecutionRecord,
                ("execution_id", "claim_status_kind"),
                deferred=True,
                on_delete=None,
            ),
        )

    @classmethod
    def triggers(cls):
        return {
            "claim_transition_frozen_facts": (
                """CREATE TRIGGER claim_transition_frozen_facts
BEFORE UPDATE ON wake_claims
BEGIN
    SELECT RAISE(ABORT, 'claim disposition must change')
    WHERE NEW.disposition = OLD.disposition;
    SELECT RAISE(ABORT, 'claim transition rewrites acceptance')
    WHERE NEW.claim_id IS NOT OLD.claim_id
       OR NEW.recipient_lookup IS NOT OLD.recipient_lookup
       OR NEW.wire_seq IS NOT OLD.wire_seq
       OR NEW.message_id IS NOT OLD.message_id
       OR NEW.recipient IS NOT OLD.recipient
       OR NEW.audience IS NOT OLD.audience
       OR NEW.wake_mode IS NOT OLD.wake_mode
       OR NEW.resolver_version IS NOT OLD.resolver_version
       OR NEW.policy_version IS NOT OLD.policy_version
       OR NEW.accepted_at_ms IS NOT OLD.accepted_at_ms
       OR NEW.revision != OLD.revision + 1
       OR NEW.updated_at_ms < OLD.updated_at_ms;
    SELECT RAISE(ABORT, 'claim transition erases engagement facts')
    WHERE (OLD.triage_verdict IS NOT NULL
           AND NEW.triage_verdict IS NOT OLD.triage_verdict)
       OR (OLD.execution_id IS NOT NULL AND NEW.execution_id IS NOT OLD.execution_id)
       OR (OLD.exact_target IS NOT NULL AND NEW.exact_target IS NOT OLD.exact_target)
       OR (OLD.exact_target IS NULL AND NEW.exact_target IS NOT NULL
           AND NEW.execution_id IS NULL)
       OR (OLD.execution_id IS NULL AND NEW.execution_id IS NOT NULL
           AND NEW.disposition != 'engaged');
    SELECT RAISE(ABORT, 'claim disposition edge is not realizable')
    WHERE OLD.disposition != NEW.disposition AND NOT ({assignment_edges}
    );
    SELECT RAISE(ABORT, 'pre-engagement failure cannot invent execution')
    WHERE NEW.disposition = 'failed' AND OLD.execution_id IS NULL
      AND NEW.execution_id IS NOT NULL;
    SELECT RAISE(ABORT, 'post-engagement deferral cannot become pending')
    WHERE OLD.disposition = 'deferred' AND OLD.execution_id IS NOT NULL
      AND NEW.disposition IN ('triage_pending', 'full_pending');
END"""
            ),
            "wake_claim_delete_frozen": (
                """CREATE TRIGGER wake_claim_delete_frozen BEFORE DELETE ON wake_claims BEGIN
    SELECT RAISE(ABORT, 'wake claim cannot be deleted');
END"""
            ),
        }


@dataclass(frozen=True, kw_only=True)
class PublicationReceipts(CoordinatorTable, TypedTable):
    execution_id: str = dataclass_field(
        metadata={
            "sql": Column(
                primary_key=True,
                storage=ExactStorage,
                check=(
                    "typeof(execution_id) = 'text' AND length(execution_id) BETWEEN 1"
                    " AND 256\n"
                    "             AND instr(execution_id, ':') = 0"
                ),
            )
        }
    )
    seq: int = dataclass_field(metadata={"sql": Column(unique=True, check="seq > 0")})
    message_id: str = dataclass_field(
        metadata={
            "sql": Column(
                storage=ExactStorage,
                unique=True,
                check=(
                    "\n      typeof(message_id) = 'text' AND length(message_id) BETWEEN 1 AND 256"
                ),
            )
        }
    )
    received_at_ms: int = dataclass_field(metadata={"sql": Column(check="received_at_ms >= 0")})
    obligation_receipt_required: int | None = dataclass_field(
        init=False, compare=False, metadata={"sql": Column(generated="1")}
    )

    @classmethod
    def references(cls):
        return (
            ForeignKey(
                ("execution_id",),
                PublicationIntents,
                ("execution_id",),
                deferred=False,
                on_delete=None,
            ),
            ForeignKey(
                ("execution_id", "obligation_receipt_required"),
                ResponseObligation,
                ("execution_id", "receipt_settled"),
                deferred=True,
                on_delete=None,
            ),
        )

    @classmethod
    def triggers(cls):
        return {
            "publication_receipt_matches_authorities": (
                """CREATE TRIGGER publication_receipt_matches_authorities
BEFORE INSERT ON publication_receipts
BEGIN
    SELECT RAISE(ABORT, 'failed execution cannot accept publication receipt')
    WHERE EXISTS (SELECT 1 FROM executions
                  WHERE execution_id = NEW.execution_id AND status = 'failed');
    SELECT RAISE(ABORT, 'publication receipt message mismatch')
    WHERE NEW.message_id != (
        SELECT expected_message_id FROM publication_intents
        WHERE execution_id = NEW.execution_id
    );
    SELECT RAISE(ABORT, 'publication receipt requires publishing obligation')
    WHERE NOT EXISTS (
        SELECT 1 FROM obligations
        WHERE execution_id = NEW.execution_id AND state = 'publishing'
    );
END"""
            ),
            "publication_receipt_is_frozen": (
                """CREATE TRIGGER publication_receipt_is_frozen
BEFORE UPDATE ON publication_receipts
BEGIN
    SELECT RAISE(ABORT, 'publication receipt is frozen');
END"""
            ),
            "publication_receipt_delete_frozen": (
                """CREATE TRIGGER publication_receipt_delete_frozen
BEFORE DELETE ON publication_receipts
BEGIN
    SELECT RAISE(ABORT, 'publication receipt is frozen');
END"""
            ),
        }


def _sql_values(names):
    return ",".join("'" + name.replace("'", "''") + "'" for name in names)


def _sql_members(family, predicate=lambda member: True):
    return _sql_values(
        member.declared_name for member in family.members_with(family) if predicate(member)
    )


def _sql_edges(family, column):
    return (
        " OR ".join(
            f"({column.format(row='OLD')} = {_sql_values((name,))} "
            f"AND {column.format(row='NEW')} IN ({_sql_values(sorted(edges))}))"
            for name, edges in family.transition_table().items()
            if edges
        )
        or "0"
    )


def _schema_context():
    return dict(
        execution_names=_sql_members(ExecutionState),
        attempt_names=_sql_members(AttemptState),
        assignment_names=_sql_members(AssignmentState),
        wake_names=_sql_members(WakePolicy),
        response_names=_sql_members(ResponseState),
        recovery_names=_sql_members(RecoveryCondition),
        execution_edges=_sql_edges(ExecutionState, "json_extract({row}.lifecycle, '$.kind')"),
        attempt_edges=_sql_edges(AttemptState, "json_extract({row}.lifecycle, '$.kind')"),
        assignment_edges=_sql_edges(AssignmentState, "{row}.disposition"),
        obligation_edges=_sql_edges(ResponseState, "json_extract({row}.lifecycle, '$.kind')"),
        terminal_attempt_names=_sql_members(AttemptState, lambda member: member.terminal),
        engaged_execution_names=_sql_members(
            ExecutionState, lambda member: member.assignment_state().engaged
        ),
        unstarted_execution_names=_sql_members(ExecutionState, lambda member: member.unstarted),
        required_attempt_execution_names=_sql_members(
            ExecutionState, lambda member: member.active or member.completed
        ),
        optional_attempt_execution_names=_sql_members(
            ExecutionState, lambda member: member.retry or member.failed
        ),
        startable_execution_names=_sql_members(
            ExecutionState, lambda member: member.starts_attempt
        ),
        successful_response_names=_sql_members(ResponseState, lambda member: member.successful),
        retryable_response_names=_sql_members(ResponseState, lambda member: member.retryable),
        intent_response_names=_sql_members(ResponseState, lambda member: member.allows_intent),
        required_intent_response_names=_sql_members(
            ResponseState, lambda member: member.requires_intent
        ),
    )


def _schema():
    tables = "\n".join(
        statement + ";"
        for row_type in TypedTable.members_with(CoordinatorTable)
        for statement in row_type.schema_objects().values()
    )
    return (
        tables
        + "\n"
        + (
            """CREATE VIEW retry_disposition_basis AS
SELECT e.execution_id,
  CASE WHEN e.current_attempt_ordinal IS NOT NULL
     AND e.current_attempt_ordinal < e.max_attempts
     AND EXISTS (SELECT 1 FROM replay_assessments r
       WHERE r.execution_id = e.execution_id AND r.retry_authorized = 1)
     AND (e.origin != 'wire' OR EXISTS (SELECT 1 FROM obligations o
       WHERE o.execution_id = e.execution_id AND o.retryable = 1))
  THEN 1 ELSE 0 END AS authorized
FROM executions e;"""
        ).format(**_schema_context())
    )


@dataclass(frozen=True)
class _TableName(TypedRow):
    name: str


class CoordinationStore:
    """Open or initialize the private versioned coordination database."""

    def __init__(self, path: str | os.PathLike[str], *, lock_timeout: float = 5.0) -> None:
        if type(lock_timeout) not in (int, float) or not 0 <= lock_timeout <= 5:
            raise ValueError("coordination lock timeout must be bounded")
        self.path = Path(path)
        self._prepare_private_file()
        self._connection = sqlite3.connect(self.path, isolation_level=None, timeout=lock_timeout)
        self._connection.create_function(
            "coordination_validate_publication_intent",
            10,
            self._validate_publication_intent_sql,
            deterministic=True,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute(f"PRAGMA busy_timeout = {int(lock_timeout * 1000)}")
        try:
            self._initialize_schema()
            # Rollback journal avoids a post-schema WAL-mode race among fresh openers.
            self._connection.execute("PRAGMA synchronous = FULL")
            self._enforce_private_modes()
        except BaseException:
            self._connection.close()
            raise

    @staticmethod
    def _validate_publication_intent_sql(
        execution_id: str,
        sender: str,
        exact_target: str,
        message_type: str,
        notice: int,
        timestamp: float,
        payload: str,
        payload_digest: str,
        publication_key: str,
        expected_message_id: str,
    ) -> int:
        """Fail closed through the existing typed envelope and Message-ID authority."""
        try:
            if not isinstance(payload, str):
                return 0
            PublicationIntents(
                execution_id=execution_id,
                sender=sender,
                exact_target=exact_target,
                message_type=MessageType(message_type),
                notice=bool(notice),
                timestamp=timestamp,
                payload=payload,
                payload_digest=payload_digest,
                publication_key=publication_key,
                expected_message_id=expected_message_id,
            )
        except Exception:
            return 0
        return 1

    def _prepare_private_file(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            # Closing *any* fd for an already-published SQLite inode can release
            # this process's classic fcntl locks, even those held by another
            # connection.  Close the private staging fd before publishing it.
            descriptor, staging = tempfile.mkstemp(prefix=".coord-init-", dir=self.path.parent)
            try:
                os.close(descriptor)
                # Another initializer may win; never overwrite its inode.
                with suppress(FileExistsError):
                    os.link(staging, self.path, follow_symlinks=False)
            finally:
                os.unlink(staging)
            info = self.path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise IntegrityViolationError("coordination database must be a regular file")
        # NTFS ACLs, not POSIX mode bits, define privacy on Windows. The
        # disposable private execution path is Linux-only; keep the schema
        # usable here without pretending chmod supplies a Windows ACL.
        if os.name != "nt" and stat.S_IMODE(info.st_mode) != 0o600:
            os.chmod(self.path, 0o600, follow_symlinks=False)

    def _initialize_schema(self) -> None:
        # Inspect version/tables only after obtaining the write lock.  SQLite's
        # executescript() implicitly commits an existing transaction; execute
        # complete trigger-aware statements individually under this one lock.
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            version = self.schema_version
            tables = {
                row.name
                for row in _TableName.read(
                    self._connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
                )
            }
            if version == 0 and not tables:
                statement = ""
                for line in _schema().splitlines(keepends=True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        self._connection.execute(statement)
                        statement = ""
                if statement.strip():
                    raise SchemaVersionError("coordination schema has an incomplete statement")
                SchemaMeta(
                    singleton=1,
                    schema_version=COORDINATION_SCHEMA_VERSION,
                    snapshot_version=COORDINATION_SNAPSHOT_VERSION,
                ).insert(self._connection)
                self._connection.execute(f"PRAGMA user_version = {COORDINATION_SCHEMA_VERSION}")
            else:
                if version != COORDINATION_SCHEMA_VERSION:
                    raise SchemaVersionError(
                        f"coordination schema {version} is unsupported; "
                        f"expected {COORDINATION_SCHEMA_VERSION}"
                    )
                row = SchemaMeta.one(self._connection, singleton=1)
                expected = SchemaMeta(
                    singleton=1,
                    schema_version=COORDINATION_SCHEMA_VERSION,
                    snapshot_version=COORDINATION_SNAPSHOT_VERSION,
                )
                if row != expected:
                    raise SchemaVersionError("coordination schema metadata is inconsistent")
            self._connection.execute("COMMIT")
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

    def _enforce_private_modes(self) -> None:
        if os.name == "nt":
            return  # POSIX no-follow chmod cannot establish an NTFS ACL.
        for suffix in ("", "-journal", "-wal", "-shm"):
            candidate = Path(f"{self.path}{suffix}")
            # SQLite may unlink a journal between observation and chmod.
            with suppress(FileNotFoundError):
                if stat.S_IMODE(candidate.lstat().st_mode) != 0o600:
                    os.chmod(candidate, 0o600, follow_symlinks=False)

    @property
    def schema_version(self) -> int:
        (version,) = SQLiteUserVersion.read(self._connection.execute("PRAGMA user_version"))
        return version.user_version

    def close(self) -> None:
        self._connection.close()
        self._enforce_private_modes()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
