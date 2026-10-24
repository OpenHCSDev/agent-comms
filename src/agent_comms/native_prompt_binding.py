"""Record which prompt Pi should receive before it launches.

After reserving input, save the request hash with the send stage, input ID and
process identity in a separate file. The execution record determines which
messages belong to the input; this saved hash does not grant permission to run.
Confirm a match only when the private session journal contains the same saved
user-message hash and the required execution evidence has been recorded.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from agent_comms.coordination_errors import IdentityConflict
from agent_comms.coordination_tables.assignments import WakeAssignment
from agent_comms.coordinator import Coordination

from .cohort_schema import CohortTable
from .field_codec import FieldCodec, Id128Text, InputId, MalformedRecord, RootId, ThreadLookup
from .locked_store import DurableFile, SQLiteFile
from .native_entries import NativeEvidenceRead
from .native_input_owner import ParticipantOwner
from .native_input_record import NativeColumn, NativeInputExecution, NativeInputRecord
from .native_pi import read_tracked_input_commitment
from .native_runtime_input import NativeRuntimeInput, NativeRuntimeTable
from .private_send_rules import NativeIdentityCheck
from .private_sidecar import create_sidecar_file, native_request_digest, sidecar_connection
from .reservation_rules import ReservationViolationError
from .threads import Thread
from .typed_table import (
    Column,
    DurableTables,
    ImmutableRows,
    IntegerStorage,
    TextStorage,
    TypedRow,
    WithoutRowid,
)

if TYPE_CHECKING:
    from .private_send_stage import NativeSendStage


def binding_store_path(store: Coordination) -> Path:
    """Sidecar store beside the private coordination database."""
    return PromptBinding.path_in(store.session.path.parent).absolute()


@dataclass(frozen=True, slots=True)
class PromptBinding(
    ImmutableRows, DurableFile, SQLiteFile, NativeInputRecord, WithoutRowid, DurableTables
):
    filename = "native_prompt_bindings.sqlite3"

    input_id: Annotated[
        InputId,
        Column(primary_key=True, check="length(input_id)=32 AND input_id NOT GLOB '*[^0-9a-f]*'"),
    ]
    stage: type[NativeInputExecution]
    execution_id: Annotated[NativeColumn, Column(storage=TextStorage, nullable=True)]
    attempt_ordinal: Annotated[NativeColumn, Column(storage=IntegerStorage, nullable=True)]
    owner_lookup: ThreadLookup
    owner_generation: Annotated[int, Column(check="owner_generation>0")]
    wire_root_id: RootId
    expected_prompt_digest: str
    bound_at_ms: Annotated[int, Column(check="bound_at_ms>0")]

    def __post_init__(self):
        # Original durable SQL is validated on acquisition, not at each caller.
        self.execution

    @property
    def execution(self) -> NativeInputExecution:
        try:
            return self.stage.from_columns(self.execution_id, self.attempt_ordinal)
        except MalformedRecord as error:
            raise IdentityConflict("Prompt binding execution columns conflict with their stage") from error

    checks = (
        "(stage='triage' AND execution_id IS NULL AND attempt_ordinal IS NULL) OR "
        "(stage='full' AND execution_id IS NOT NULL AND attempt_ordinal>0)",
        "length(owner_lookup)=32 AND length(wire_root_id)=32 "
        "AND wire_root_id NOT GLOB '*[^0-9a-f]*' "
        "AND length(expected_prompt_digest)=64 AND expected_prompt_digest NOT GLOB '*[^0-9a-f]*'",
    )

@dataclass(frozen=True)
class _BindingRoot(TypedRow):
    wire_root_id: RootId


def _ensure_binding_schema(store: Coordination) -> None:
    """Serialized snapshot installation; never repair an uncertain commit."""
    create_sidecar_file(binding_store_path(store), PromptBinding)


def bind_expected_prompt(
    store: Coordination,
    *,
    input_id: InputId,
    stage: NativeSendStage,
    owner: Thread,
    generation: int,
    prompt: str,
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
    Id128Text.decode(input_id)
    if not FieldCodec.decode(str, prompt):
        raise ValueError("prompt binding requires nonempty original text")
    digest = native_request_digest(prompt)

    # Installation grants no owner authority. Recheck ownership after it, then
    # hold SQL generation writers through insertion. Registry lifecycle writers
    # are excluded separately by the final native-send boundary, not this SQL lock.
    _ensure_binding_schema(store)
    path = binding_store_path(store)
    with sidecar_connection(path, PromptBinding) as sidecar:
        with store.session.writing() as db:
            NativeRuntimeTable.require_installed(db)
            CohortTable.require_installed(db)
            ParticipantOwner(owner, generation).require(store, stage.recipient_lookup)
            reserved = NativeRuntimeInput.one(db, input_id=input_id)
            if reserved is None:
                raise IdentityConflict("prompt binding requires an already reserved input")
            try:
                NativeIdentityCheck(
                    row=reserved,
                    stage=stage,
                    owner=ParticipantOwner(owner, generation).coordinator_identity(
                        stage.recipient_lookup
                    ),
                ).require_valid()
            except ReservationViolationError as error:
                raise IdentityConflict(
                    f"prompt binding identity differs from its reservation: {error}"
                ) from error
            stage.require_sources(db, input_id)
            roots = {_binding_wire_root(store, original) for original in stage.assignments}
            if len(roots) != 1:
                raise IdentityConflict("Native sources belong to different canonical wire roots")
            wire_root_id = next(iter(roots))
            if PromptBinding.one(sidecar, input_id=input_id) is not None:
                raise IdentityConflict("this input already has a prelaunch prompt binding")
            expected = PromptBinding(
                input_id=input_id,
                stage=type(stage.execution),
                **stage.execution.binding_fields(),
                owner_lookup=stage.recipient_lookup,
                owner_generation=generation,
                wire_root_id=wire_root_id,
                expected_prompt_digest=digest,
                bound_at_ms=store.session.now(0),
            )
            inserted = expected.insert(sidecar)
            if inserted.rowcount != 1 or PromptBinding.one(sidecar, input_id=input_id) != expected:
                raise IdentityConflict("prelaunch binding insert did not preserve exact identity")
    return digest


def _binding_wire_root(store: Coordination, assignment: WakeAssignment) -> RootId:
    """Resolve the trusted wire root for a sealed claim from the cohort acknowledgement."""
    with store.session.read():
        rows = _BindingRoot.read(
            store.session._connection.execute(
                "SELECT r.wire_root_id FROM claim_batch_members m "
                "JOIN claim_batch_receipts r ON r.wire_root_id=m.wire_root_id "
                "AND r.wire_seq=m.wire_seq AND r.message_id=? AND r.sealed=1 "
                "WHERE m.claim_id=? AND m.recipient_lookup=?",
                (assignment.message_id, assignment.assignment_id, assignment.recipient_lookup),
            )
        )
    if len(rows) != 1:
        raise IdentityConflict("prompt binding requires a sealed claim acknowledgement")
    return rows[0].wire_root_id


@contextmanager
def expected_prompt_bindings(
    store: Coordination, input_ids: Iterable[InputId]
) -> Iterator[dict[InputId, PromptBinding]]:
    """Keep one immutable binding snapshot open while validating input."""
    input_ids = tuple(input_ids)
    path = binding_store_path(store)
    if not input_ids or (not path.exists() and not path.is_symlink()):
        yield {}
        return
    with sidecar_connection(path, PromptBinding, read_only=True) as db:
        yield {row.input_id: row for row in PromptBinding.select(
            db, where=f"input_id IN ({','.join('?' for _ in input_ids)})", parameters=input_ids,
        )}


def expected_prompt_matches_journal(
    session_file: Path, binding: PromptBinding, *, evidence: NativeEvidenceRead | None = None
) -> bool:
    """Join the durable journal digest to the prelaunch binding digest.

    A mismatch or an uncommitted input is NOT equality. An unreadable or
    malformed journal is a defect, not inequality, and raises.
    """
    observed = read_tracked_input_commitment(session_file, binding.input_id, evidence=evidence)
    return observed.matches(binding.expected_prompt_digest)
