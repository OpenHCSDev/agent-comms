"""Attempt recovery: owned coordinator state and transitions."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from agent_comms.attempt_states import UnknownOutcomeAttempt, UnsuccessfulTerminal
from agent_comms.errors import RelationViolationError
from agent_comms.coordination_errors import (
    IdentityConflict,
    PublicationUncertain,
    RecoveryBlocked,
    StaleFence,
    StaleRevision,
)
from agent_comms.coordination_results import Applied
from agent_comms.coordination_snapshot import RecoverySnapshot
from agent_comms.coordination_tables.attempts import AttemptRecord, ReplayAssessments, ReplayFact
from agent_comms.coordination_tables.recovery import (
    RecoveryAudit,
)
from agent_comms.coordinator import Coordination
from agent_comms.native_runtime_input import NativeRuntimeInput
from agent_comms.recovery_states import DeferredRecovery, FailedRecovery

if TYPE_CHECKING:
    from agent_comms.owner_lifecycle import OwnerReleaseReceipt
    from agent_comms.coordination_session import NativeBoundary

_OWNER_LOSS_ISSUER = object()


@dataclass(frozen=True, slots=True, init=False)
class VerifiedOwnerLoss:
    """A native owner's attested release, valid only inside its registry lock."""

    attempt: AttemptRecord
    native_input: NativeRuntimeInput
    release: OwnerReleaseReceipt
    _issuer: object = field(repr=False)
    _active: bool = field(repr=False)
    _store: Coordination = field(repr=False)
    boundary: NativeBoundary = field(repr=False)

    def __init__(self, **_unsupported: object) -> None:
        raise RecoveryBlocked("owner-loss proof requires an observed native owner release")

    @classmethod
    @contextmanager
    def observe_native_release(
        cls, store: Coordination, execution_id: str
    ) -> Iterator[VerifiedOwnerLoss]:
        """Join the existing native admission, release receipt and live registry.

        Keep canonical wire/bus/registry exclusion through monitor settlement.
        A later attested release also fences earlier admission generations of the same
        incarnation. If send admission was never recorded, require the exact
        current owner to be attested stopped and dead: absence of an epoch is
        not proof that the input was unsent. Lease expiry or a replaced PID alone
        is insufficient.
        """
        from agent_comms.comms import Comms
        from agent_comms.coordinated_runtime_schema import assert_native_runtime_schema

        if store.session.path.name != "coordination.sqlite3":
            raise RecoveryBlocked("native recovery requires the canonical coordination store")
        comms = Comms(store.session.path.parent)
        with store.session.boundary(comms.bus) as boundary:
            registry = boundary.registry
            assert_native_runtime_schema(store.session._connection)
            snapshot = store.snapshots.get(execution_id)
            attempt = snapshot.attempt
            if attempt is None or not snapshot.is_current:
                raise RecoveryBlocked("native recovery requires the current attempted execution")
            source = NativeRuntimeInput.one(
                store.session._connection,
                execution_id=execution_id,
                attempt_ordinal=attempt.attempt_ordinal,
            )
            if source is None or source.owner_identity != attempt.owner_identity:
                raise RecoveryBlocked("native attempt has no matching dispatched owner")
            try:
                release = comms.owners.releases.read().get(attempt.owner_thread)
            except (OSError, ValueError, TypeError) as error:
                raise RecoveryBlocked("native owner release receipt is invalid") from error
            if release is None:
                raise RecoveryBlocked("native owner release receipt is missing")
            try:
                release.require_native_loss(registry, source)
            except (ValueError, IdentityConflict, RelationViolationError) as error:
                raise RecoveryBlocked(f"native owner release does not prove loss of this admission: {error}") from error
            proof = object.__new__(cls)
            for name, value in (
                ("attempt", attempt),
                ("native_input", source),
                ("release", release),
                ("_issuer", _OWNER_LOSS_ISSUER),
                ("_active", True),
                ("_store", store),
                ("boundary", boundary),
            ):
                object.__setattr__(proof, name, value)
            try:
                yield proof
            finally:
                object.__setattr__(proof, "_active", False)


    def issued_for(self, store: Coordination) -> bool:
        """Whether this live proof was issued while excluding this store's owner."""
        return self._issuer is _OWNER_LOSS_ISSUER and self._active and self._store is store

    def owns(self, store: Coordination, attempt: AttemptRecord) -> bool:
        """Only this acquired, unexpired observer attests the original attempt."""
        return (
            self.issued_for(store)
            and self.attempt.authority == attempt.authority
            and self.attempt.owner_identity == attempt.owner_identity
        )

    def require_native_exit(self) -> None:
        """Inspect the original launch resources while owner exclusion is held.

        A recorded selected journal supplements the original allocated directory
        check. Original directory-bound admissions recorded identity only with
        the later context receipt. Before admission the exact stopped declaration
        supplies any saved selection instead. Missing admission never establishes
        that a native child did not launch.
        """
        if not self.owns(self._store, self.attempt):
            raise RecoveryBlocked("native exit observation requires acquired owner loss")
        session_files = self.native_input.sent_owner_admission_generation.recovery_session_files(
            self.native_input, self.release
        )
        session_dir = (
            self._store.session.path.parent / "native-sessions" / self.native_input.owner_lookup
        )
        proc = Path("/proc")
        if not proc.is_dir():
            raise RecoveryBlocked("native recovery requires Linux process observation")
        for entry in proc.iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                if entry.stat().st_uid != os.getuid():
                    continue
                args = (entry / "cmdline").read_bytes().split(b"\0")
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError as error:
                raise RecoveryBlocked("native process observation was denied") from error
            for index, argument in enumerate(args[:-1]):
                selected = argument == b"--session" and any(
                    args[index + 1] == os.fsencode(path) for path in session_files
                )
                allocated = (
                    argument == b"--session-dir" and args[index + 1] == os.fsencode(session_dir)
                )
                if selected or allocated:
                    raise RecoveryBlocked("native session subprocess is still running")


    def finish(
        self,
        expected: RecoverySnapshot,
        *,
        outcome: UnsuccessfulTerminal,
        reason_code: str,
        replay_facts: ReplayFact = ReplayFact.NONE,
    ) -> Applied[RecoverySnapshot]:
        """The acquired release owns local exit checks and exact-attempt settlement."""
        self.require_native_exit()
        store = self._store
        execution_id = expected.execution.execution_id
        expected_attempt = expected.require_current_attempt()
        with store.session.settlement(self.boundary) as boundary:
            db = store.session._connection
            snapshot = store.snapshots.get(execution_id)
            attempt = snapshot.attempt
            if (
                not snapshot.is_current
                or attempt is None
                or (attempt.attempt_ordinal, attempt.owner_generation)
                != (expected_attempt.attempt_ordinal, expected_attempt.owner_generation)
            ):
                raise StaleFence("recovery must name the exact old active attempt")
            if (attempt.revision, snapshot.execution.revision, snapshot.pointer_revision) != (
                expected_attempt.revision, expected.execution.revision, expected.pointer_revision
            ):
                raise StaleRevision("recovery CAS is stale")
            if not self.owns(store, attempt):
                raise RecoveryBlocked("recovery requires an observed release of this exact owner")
            now = store.session.now(max(attempt.updated_at_ms, int(time.time() * 1000)))
            ReplayAssessments.accumulate(
                db, execution_id, ReplayFact(replay_facts) | outcome.replay_contribution
            )
            AttemptRecord.update(
                db,
                where="execution_id=? AND attempt_ordinal=?",
                parameters=(execution_id, attempt.attempt_ordinal),
                lifecycle=replace(attempt.lifecycle, process_dead=True, backend_done=True),
                revision=attempt.revision + 1,
                updated_at_ms=now,
                reason_code=reason_code,
            )
            if not snapshot.publication_intents:
                store.snapshots.get(execution_id).settle(
                    store.session, outcome, reason_code, boundary
                )
                settled = store.snapshots.get(execution_id)
                RecoveryAudit(
                    execution_id=execution_id,
                    kind=DeferredRecovery if settled.execution.lifecycle.retry else FailedRecovery,
                    reason_code=reason_code,
                    sanitized_detail=None,
                    attempt=attempt.attempt_ordinal,
                    elapsed_ms=0,
                    observed_at_ms=now,
                ).insert(db)
                return Applied(settled)
        # Local exit commits; unresolved publication still requires its original bus proof.
        raise PublicationUncertain("frozen publication requires bus-keyed proof")


class AttemptRecovery:
    """Resolve released owners without reconstructing acceptance or replaying effects."""

    @classmethod
    def settle_stopped_owner(cls, root: Path, owner) -> None:
        """An explicit stop retires only the exited owner's original local attempt."""
        from .comms import Comms

        if not (root / "coordination.sqlite3").exists():
            return  # No native coordinator, hence no selected attempt claims.
        receipt = Comms(root).owners.releases.read().get(owner.name)
        if receipt is None or receipt.thread.process_identity != owner.process_identity:
            raise RecoveryBlocked("stopped attempt requires the exact exited owner release")
        cls.recover_owner(root, owner)

    @classmethod
    def recover_owner(cls, root: Path, owner) -> None:
        """Resolve an exited predecessor once, before this worker begins draining."""
        from .coordination_response import PublicationAppendDispatches
        from .coordination_tables.executions import CurrentExecutions

        path = root / "coordination.sqlite3"
        if not path.exists():
            return
        with Coordination(str(path)) as store:
            lookup = owner.incarnation.lookup
            pointer = CurrentExecutions.one(store.session._connection, owner_lookup=lookup)
            if pointer is None or not pointer.unresolved:
                return
            # The released owner and exact keyed dispatch prove an existing
            # frozen reply before any attempt is marked uncertain.
            with VerifiedOwnerLoss.observe_native_release(store, pointer.execution_id) as loss:
                loss.require_native_exit()
                PublicationAppendDispatches.resolve_retained(
                    store, loss.boundary.bus, loss.boundary,
                    owner_lookup=lookup, owner_thread=owner.name,
                    execution_id=pointer.execution_id,
                )
            pointer = CurrentExecutions.one(store.session._connection, owner_lookup=lookup)
            if pointer is not None and pointer.unresolved:
                try:
                    cls.recover_released_native_attempt(store, pointer.execution_id)
                except RecoveryBlocked:
                    # Stop cannot reconstruct absent or broken provider proof.
                    # The existing monitor still demands the same exact release
                    # and native exit before recording UNKNOWN, never replay.
                    cls.abandon_released_native_attempt(store, pointer.execution_id)

    @classmethod
    def recover_released_native_attempt(cls, store, execution_id):
        """Settle the old input without re-running it or fabricating delivery."""
        from .native_entries import NativeEntry
        from .native_pi import NativeContextProof, NativePiUnavailable
        from .native_prompt_binding import expected_prompt_matches_journal, read_expected_prompt_binding
        from .pi_vocabulary import PendingStopReason
        from .private_path import FileRevision

        snapshot = store.snapshots.get(execution_id)
        attempt = snapshot.require_current_attempt()
        source = NativeRuntimeInput.one(
            store.session._connection, execution_id=execution_id,
            attempt_ordinal=attempt.attempt_ordinal,
        )
        if source is None:
            raise RecoveryBlocked("native attempt has no original input")
        terminal = PendingStopReason
        revision = None
        session = source.selected_session
        if session is not None:
            binding = read_expected_prompt_binding(store, source.input_id)
            if binding is None or binding.identity != source.identity:
                raise RecoveryBlocked("native recovery lacks its bound original input")
            revision = FileRevision.capture(session.path)
            with NativeEntry.open_evidence(session.path) as evidence:
                if not expected_prompt_matches_journal(session.path, binding, evidence=evidence):
                    raise RecoveryBlocked("native recovery input does not match its binding")
                try:
                    proof = NativeContextProof.read_evidence(
                        session.path, source.input_id, evidence=evidence,
                    )
                except NativePiUnavailable:
                    # No conclusive context proof never means the input was unsent.
                    terminal = PendingStopReason
                else:
                    session.require_context(proof)
                    terminal = evidence.stopped_input(source.input_id)
        with VerifiedOwnerLoss.observe_native_release(store, execution_id) as loss:
            if loss.native_input != source:
                raise StaleRevision("native input changed during recovery")
            if revision is not None and FileRevision.capture(session.path) != revision:
                raise StaleRevision("native journal changed during recovery")
            current = store.snapshots.get(execution_id)
            outcome, replay_facts = terminal.unpublished_attempt()
            return loss.finish(
                current, outcome=outcome, replay_facts=replay_facts,
                reason_code="released_native_" + terminal.declared_name,
            )

    @classmethod
    def abandon_released_native_attempt(
        cls, store: Coordination, execution_id: str
    ) -> Applied[RecoverySnapshot]:
        """Close a released, dead native attempt while retaining UNKNOWN effects.

        This explicit operator action abandons the old local backend; it does
        not reconstruct a provider terminal, acceptance, or an unsent outcome.
        Owner exclusion and native-process absence prevent further local work.
        The existing monitor atomically fails the attempt, records replay-unsafe
        UNKNOWN effects and releases its slot. Input receipts/cursors are never
        changed. A frozen publication still requires its receipt resolver.

        Without a recorded admission epoch, stop the current owner normally
        before calling, then restart it normally for unrelated new work.
        """
        with VerifiedOwnerLoss.observe_native_release(store, execution_id) as loss:
            snapshot = store.snapshots.get(execution_id)
            if bool(snapshot.publication_intents):
                raise PublicationUncertain("UNKNOWN abandonment cannot resolve frozen publication")
            return loss.finish(
                snapshot, outcome=UnknownOutcomeAttempt(), reason_code="released_native_unknown"
            )
