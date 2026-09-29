"""Versioned prelaunch expected-prompt binding for coordinated native inputs.

Written to a separate sidecar store BEFORE Pi launches, immediately after the
input reservation commits. The binding records the native request-envelope digest
joined to the source sequence, sealed claim, stage, input ID, and owner
incarnation. It confers no authority by itself: a binding without a recorded
live proof is unproven, and equality is only ever reported after the private
session journal's durable user-message digest matches the binding.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from agent_comms.coordination_tables.assignments import WakeAssignment
from agent_comms.private_runtime_schema import PrivateRuntimeSchema

from .cohort_schema import assert_cohort_schema
from .coordinated_runtime_schema import assert_native_runtime_schema
from .coordination_store import IdentityConflict, MutationStore
from .native_pi import _INPUT_ID, NativePiUnavailable, read_tracked_input_digest
from .native_runtime_input import NativeRuntimeInput
from .private_sidecar import create_sidecar_file, native_request_digest, sidecar_connection
from .threads import Thread
from .typed_table import Column, TypedRow, TypedTable

_BINDING_PATH = "native_prompt_bindings.sqlite3"


def binding_store_path(store: MutationStore) -> Path:
    """Sidecar store beside the private coordination database."""
    if type(store) is not MutationStore:
        raise TypeError("prompt binding requires the actual coordinator store")
    return (store.path.parent / _BINDING_PATH).absolute()


@dataclass(frozen=True, slots=True)
class PromptBinding(TypedTable, PrivateRuntimeSchema):
    @classmethod
    def install(cls, store) -> None:
        install_prompt_binding_schema(store)

    input_id: str = field(
        metadata={
            "sql": Column(
                primary_key=True, check="length(input_id)=32 AND input_id NOT GLOB '*[^0-9a-f]*'"
            )
        }
    )
    stage: str
    assignment_id: str
    execution_id: str | None
    attempt_ordinal: int | None
    owner_lookup: str
    owner_thread: str
    owner_generation: int = field(metadata={"sql": Column(check="owner_generation>0")})
    wire_root_id: str
    source_seq: int
    message_id: str
    expected_prompt_digest: str
    bound_at_ms: int = field(metadata={"sql": Column(check="bound_at_ms>0")})

    without_rowid = True
    checks = (
        "(stage='triage' AND execution_id IS NULL AND attempt_ordinal IS NULL) OR "
        "(stage='full' AND execution_id IS NOT NULL AND attempt_ordinal>0)",
        "length(owner_lookup)=32 AND owner_thread<>'' AND length(wire_root_id)=32 "
        "AND wire_root_id NOT GLOB '*[^0-9a-f]*' AND source_seq>0 AND message_id<>'' "
        "AND length(expected_prompt_digest)=64 AND expected_prompt_digest NOT GLOB '*[^0-9a-f]*'",
    )

    @classmethod
    def triggers(cls) -> dict[str, str]:
        return {
            f"{cls.declared_name}_{operation.lower()}_guard": f"CREATE TRIGGER {cls.declared_name}_"
            f"{operation.lower()}_guard "
            f"BEFORE {operation} ON {cls.declared_name} "
            "BEGIN SELECT RAISE(ABORT,'prelaunch binding is immutable'); END"
            for operation in ("UPDATE", "DELETE")
        }


@dataclass(frozen=True)
class _BindingRoot(TypedRow):
    wire_root_id: str


def install_prompt_binding_schema(store: MutationStore) -> None:
    """Explicit fresh-root install; ordinary callers may rely on _ensure."""
    _ensure_binding_schema(store)


def _ensure_binding_schema(store: MutationStore) -> None:
    """Serialized snapshot installation; never repair an uncertain commit."""
    create_sidecar_file(binding_store_path(store), PromptBinding)


def bind_expected_prompt(
    store: MutationStore,
    *,
    input_id: str,
    stage: str,
    assignment: WakeAssignment,
    owner: Thread,
    generation: int,
    prompt: str,
    execution_id: str | None = None,
    attempt_ordinal: int | None = None,
) -> str:
    """Commit the prelaunch native-request digest under a serialized fence.

    The coordination store WRITE transaction is held from the live owner
    recheck through the sidecar insert, so an owner-generation advance cannot
    interleave between validation and the durable binding (no
    precheck/write/postcheck window). Must run after the input reservation and
    before Pi launches; a crash in between leaves a reserved input without a
    binding and nothing may advance on that input.

    The digest covers the pinned native ``pi-input-request-v1`` request
    envelope exactly as ``AgentSession._claimNativeInput`` computes it — NOT
    the bare prompt bytes.
    """
    if (
        type(store) is not MutationStore
        or type(input_id) is not str
        or _INPUT_ID.fullmatch(input_id) is None
        or stage not in {"triage", "full"}
        or type(assignment) is not WakeAssignment
        or type(owner) is not Thread
        or type(generation) is not int
        or generation <= 0
        or type(prompt) is not str
        or not prompt
        or (stage == "triage" and (execution_id is not None or attempt_ordinal is not None))
        or (stage == "full" and (execution_id is None or attempt_ordinal is None))
    ):
        raise ValueError("prompt binding requires bounded exact prelaunch identities")
    digest = native_request_digest(prompt)
    from .coordinated_runtime import _require_owner

    # Installation grants no owner authority. Recheck ownership after it, then
    # hold SQL generation writers through insertion. Registry lifecycle writers
    # are excluded separately by the final native-send boundary, not this SQL lock.
    _ensure_binding_schema(store)
    with store._transaction() as db:
        assert_native_runtime_schema(db)
        assert_cohort_schema(db)
        _require_owner(store, assignment.recipient_lookup, owner, generation)
        reserved = NativeRuntimeInput.one(db, input_id=input_id)
        if reserved is None:
            raise IdentityConflict("prompt binding requires an already reserved input")
        if (
            reserved.stage != stage
            or reserved.assignment_id != assignment.assignment_id
            or reserved.execution_id != execution_id
            or reserved.attempt_ordinal != attempt_ordinal
            or reserved.owner_lookup != assignment.recipient_lookup
            or reserved.owner_thread != owner.name
            or reserved.owner_generation != generation
        ):
            raise IdentityConflict("prompt binding identity differs from its reservation")
        wire_root_id = _binding_wire_root(store, assignment)
        path = binding_store_path(store)
        with sidecar_connection(path, PromptBinding) as sidecar:
            if PromptBinding.one(sidecar, input_id=input_id) is not None:
                raise IdentityConflict("this input already has a prelaunch prompt binding")
            expected = PromptBinding(
                input_id=input_id,
                stage=stage,
                assignment_id=assignment.assignment_id,
                execution_id=execution_id,
                attempt_ordinal=attempt_ordinal,
                owner_lookup=assignment.recipient_lookup,
                owner_thread=owner.name,
                owner_generation=generation,
                wire_root_id=wire_root_id,
                source_seq=assignment.wire_seq,
                message_id=assignment.message_id,
                expected_prompt_digest=digest,
                bound_at_ms=store._now(0),
            )
            inserted = expected.insert(sidecar)
            if inserted.rowcount != 1 or PromptBinding.one(sidecar, input_id=input_id) != expected:
                raise IdentityConflict("prelaunch binding insert did not preserve exact identity")
    return digest


def _binding_wire_root(store: MutationStore, assignment: WakeAssignment) -> str:
    """Resolve the trusted wire root for a sealed claim from the cohort receipt."""
    with store._read_transaction():
        rows = _BindingRoot.read(
            store._connection.execute(
                "SELECT r.wire_root_id FROM claim_batch_members m "
                "JOIN claim_batch_receipts r ON r.wire_root_id=m.wire_root_id "
                "AND r.wire_seq=m.wire_seq AND r.message_id=? AND r.sealed=1 "
                "WHERE m.claim_id=? AND m.recipient_lookup=?",
                (assignment.message_id, assignment.assignment_id, assignment.recipient_lookup),
            )
        )
    if len(rows) != 1:
        raise IdentityConflict("prompt binding requires a sealed claim receipt")
    return rows[0].wire_root_id


def read_expected_prompt_binding(
    store: MutationStore, input_id: str, *, blocking: bool = True
) -> PromptBinding | None:
    """Return the immutable binding, or None when none was durably written."""
    if type(store) is not MutationStore or type(input_id) is not str:
        raise ValueError("prompt binding lookup requires the coordinator store and input ID")
    path = binding_store_path(store)
    if not path.exists() and not path.is_symlink():
        return None
    with sidecar_connection(path, PromptBinding, blocking=blocking) as db:
        return PromptBinding.one(db, input_id=input_id)


def expected_prompt_matches_journal(session_file: Path, binding: PromptBinding) -> bool:
    """Join the durable journal digest to the prelaunch binding digest.

    A mismatch, absence, or malformed journal is NOT equality: fail closed.
    """
    try:
        observed = read_tracked_input_digest(session_file, binding.input_id)
    except (OSError, ValueError, NativePiUnavailable):
        return False
    return observed == binding.expected_prompt_digest
