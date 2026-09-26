"""Versioned prelaunch expected-prompt binding for coordinated native inputs.

Written to a separate sidecar store BEFORE Pi launches, immediately after the
input reservation commits. The binding records the exact prompt bytes' digest
joined to the source sequence, sealed claim, stage, input ID, and owner
incarnation. It confers no authority by itself: a binding without a recorded
live proof is unproven, and equality is only ever reported after the private
session journal's durable user-message digest matches the binding.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .coordinated_runtime_schema import assert_native_runtime_schema
from .coordination import WakeClaim
from .coordination_cohort import _assert_schema as assert_cohort_schema
from .coordination_store import IdentityConflict, MutationStore
from .declarations import Thread
from .native_pi import (
    _INPUT_ID,
    NativePiUnavailable,
    read_tracked_input_digest,
)
from .private_sidecar import (
    create_sidecar_file,
    native_request_digest,
    sidecar_connection,
)

_DDL = (
    (
        "prompt_binding_meta",
        """CREATE TABLE prompt_binding_meta (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            version INTEGER NOT NULL CHECK (version = 1),
            ddl_digest TEXT NOT NULL CHECK (length(ddl_digest) = 64)
        ) STRICT""",
    ),
    (
        "prompt_bindings",
        """CREATE TABLE prompt_bindings (
            input_id TEXT PRIMARY KEY CHECK (
                length(input_id) = 32 AND input_id NOT GLOB '*[^0-9a-f]*'),
            binding_version INTEGER NOT NULL CHECK (binding_version = 1),
            stage TEXT NOT NULL CHECK (stage IN ('triage','full')),
            claim_id TEXT NOT NULL,
            execution_id TEXT,
            attempt_ordinal INTEGER,
            owner_lookup TEXT NOT NULL CHECK (length(owner_lookup) = 32),
            owner_thread TEXT NOT NULL CHECK (owner_thread <> ''),
            owner_generation INTEGER NOT NULL CHECK (owner_generation > 0),
            wire_root_id TEXT NOT NULL CHECK (
                length(wire_root_id) = 32 AND wire_root_id NOT GLOB '*[^0-9a-f]*'),
            source_seq INTEGER NOT NULL CHECK (source_seq > 0),
            message_id TEXT NOT NULL CHECK (message_id <> ''),
            expected_prompt_digest TEXT NOT NULL CHECK (
                length(expected_prompt_digest) = 64
                AND expected_prompt_digest NOT GLOB '*[^0-9a-f]*'),
            bound_at_ms INTEGER NOT NULL CHECK (bound_at_ms > 0),
            CHECK ((stage='triage' AND execution_id IS NULL AND attempt_ordinal IS NULL)
                OR (stage='full' AND execution_id IS NOT NULL AND attempt_ordinal > 0))
        ) STRICT, WITHOUT ROWID""",
    ),
    (
        "prompt_binding_update_guard",
        """CREATE TRIGGER prompt_binding_update_guard
        BEFORE UPDATE ON prompt_bindings
        BEGIN SELECT RAISE(ABORT,'a prelaunch prompt binding is immutable'); END""",
    ),
    (
        "prompt_binding_delete_guard",
        """CREATE TRIGGER prompt_binding_delete_guard
        BEFORE DELETE ON prompt_bindings
        BEGIN SELECT RAISE(ABORT,'a prelaunch prompt binding cannot be deleted'); END""",
    ),
    (
        "prompt_binding_meta_update_guard",
        """CREATE TRIGGER prompt_binding_meta_update_guard
        BEFORE UPDATE ON prompt_binding_meta
        BEGIN SELECT RAISE(ABORT,'prompt binding schema is frozen'); END""",
    ),
    (
        "prompt_binding_meta_delete_guard",
        """CREATE TRIGGER prompt_binding_meta_delete_guard
        BEFORE DELETE ON prompt_binding_meta
        BEGIN SELECT RAISE(ABORT,'prompt binding schema is frozen'); END""",
    ),
)
_DDL_DIGEST = hashlib.sha256(json.dumps(_DDL, separators=(",", ":")).encode()).hexdigest()

_BINDING_PATH = "native_prompt_bindings.sqlite3"


def binding_store_path(store: MutationStore) -> Path:
    """Sidecar store beside the private coordination database."""
    if type(store) is not MutationStore:
        raise TypeError("prompt binding requires the actual coordinator store")
    return (store.path.parent / _BINDING_PATH).absolute()


@dataclass(frozen=True, slots=True)
class PromptBinding:
    input_id: str
    stage: str
    claim_id: str
    execution_id: str | None
    attempt_ordinal: int | None
    owner_lookup: str
    owner_thread: str
    owner_generation: int
    wire_root_id: str
    source_seq: int
    message_id: str
    expected_prompt_digest: str
    bound_at_ms: int


def install_prompt_binding_schema(store: MutationStore) -> None:
    """Explicit fresh-root install; ordinary callers may rely on _ensure."""
    _ensure_binding_schema(store)


def _ensure_binding_schema(store: MutationStore) -> None:
    """Self-initialize the sidecar like GoalHistoryStore; then verify the
    actual schema objects, permissions, and durable creation."""
    create_sidecar_file(binding_store_path(store), _DDL, _DDL_DIGEST)


def bind_expected_prompt(
    store: MutationStore,
    *,
    input_id: str,
    stage: str,
    claim: WakeClaim,
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
        or type(claim) is not WakeClaim
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
        _require_owner(store, claim.recipient_lookup, owner, generation)
        reserved = db.execute(
            "SELECT stage,claim_id,execution_id,attempt_ordinal,owner_lookup,owner_thread,"
            "owner_generation FROM native_runtime_inputs WHERE input_id=?",
            (input_id,),
        ).fetchone()
        if reserved is None:
            raise IdentityConflict("prompt binding requires an already reserved input")
        if (
            reserved["stage"] != stage
            or reserved["claim_id"] != claim.claim_id
            or reserved["execution_id"] != execution_id
            or reserved["attempt_ordinal"] != attempt_ordinal
            or reserved["owner_lookup"] != claim.recipient_lookup
            or reserved["owner_thread"] != owner.name
            or reserved["owner_generation"] != generation
        ):
            raise IdentityConflict("prompt binding identity differs from its reservation")
        root_row = db.execute(
            "SELECT r.wire_root_id FROM claim_batch_members m "
            "JOIN claim_batch_receipts r ON r.wire_root_id=m.wire_root_id "
            "AND r.wire_seq=m.wire_seq AND r.message_id=? AND r.sealed=1 "
            "WHERE m.claim_id=? AND m.recipient_lookup=?",
            (claim.message_id, claim.claim_id, claim.recipient_lookup),
        ).fetchone()
        if root_row is None:
            raise IdentityConflict("prompt binding requires a sealed claim receipt")
        wire_root_id = str(root_row["wire_root_id"])
        path = binding_store_path(store)
        with sidecar_connection(path, _DDL, _DDL_DIGEST) as sidecar:
            existing = sidecar.execute(
                "SELECT 1 FROM prompt_bindings WHERE input_id=?", (input_id,)
            ).fetchone()
            if existing is not None:
                raise IdentityConflict("this input already has a prelaunch prompt binding")
            sidecar.execute(
                "INSERT INTO prompt_bindings"
                "(input_id,binding_version,stage,claim_id,execution_id,attempt_ordinal,"
                "owner_lookup,owner_thread,owner_generation,wire_root_id,source_seq,"
                "message_id,expected_prompt_digest,bound_at_ms) "
                "VALUES (?,1,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    input_id,
                    stage,
                    claim.claim_id,
                    execution_id,
                    attempt_ordinal,
                    claim.recipient_lookup,
                    owner.name,
                    generation,
                    wire_root_id,
                    claim.wire_seq,
                    claim.message_id,
                    digest,
                    store._now(0),
                ),
            )
    return digest


def _binding_wire_root(store: MutationStore, claim: WakeClaim) -> str:
    """Resolve the trusted wire root for a sealed claim from the cohort receipt."""
    with store._read_transaction():
        row = store._connection.execute(
            "SELECT r.wire_root_id FROM claim_batch_members m "
            "JOIN claim_batch_receipts r ON r.wire_root_id=m.wire_root_id "
            "AND r.wire_seq=m.wire_seq AND r.message_id=? AND r.sealed=1 "
            "WHERE m.claim_id=? AND m.recipient_lookup=?",
            (claim.message_id, claim.claim_id, claim.recipient_lookup),
        ).fetchone()
    if row is None:
        raise IdentityConflict("prompt binding requires a sealed claim receipt")
    resolved = str(row["wire_root_id"])
    return resolved


def read_expected_prompt_binding(store: MutationStore, input_id: str) -> PromptBinding | None:
    """Return the immutable binding, or None when none was durably written."""
    if type(store) is not MutationStore or type(input_id) is not str:
        raise ValueError("prompt binding lookup requires the coordinator store and input ID")
    path = binding_store_path(store)
    if not path.exists():
        return None
    with sidecar_connection(path, _DDL, _DDL_DIGEST) as db:
        binding = db.execute(
            "SELECT * FROM prompt_bindings WHERE input_id=?", (input_id,)
        ).fetchone()
    if binding is None:
        return None
    return PromptBinding(
        binding["input_id"],
        binding["stage"],
        binding["claim_id"],
        binding["execution_id"],
        binding["attempt_ordinal"],
        binding["owner_lookup"],
        binding["owner_thread"],
        binding["owner_generation"],
        binding["wire_root_id"],
        binding["source_seq"],
        binding["message_id"],
        binding["expected_prompt_digest"],
        binding["bound_at_ms"],
    )


def expected_prompt_matches_journal(session_file: Path, binding: PromptBinding) -> bool:
    """Join the durable journal digest to the prelaunch binding digest.

    A mismatch, absence, or malformed journal is NOT equality: fail closed.
    """
    try:
        observed = read_tracked_input_digest(session_file, binding.input_id)
    except (OSError, ValueError, NativePiUnavailable):
        return False
    return observed == binding.expected_prompt_digest
