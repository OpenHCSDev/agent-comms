"""Attempt recovery: owned coordinator state and transitions."""

from __future__ import annotations


from agent_comms.native_session_files import NativeSessionFiles
from .native_failures import NativePiUnavailable

import os
import shutil
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Self
from uuid import uuid4

from agent_comms.reason_codes import GivenReasonCode, NoDetail
from agent_comms.attempt_states import UnknownOutcomeAttempt, UnsuccessfulTerminal
from agent_comms.coordination_errors import (
    IdentityConflict,
    PublicationUncertain,
    RecoveryBlocked,
    StaleFence,
    StaleRevision,
    UnrecordedNativeContext,
)
from agent_comms.coordination_results import Applied
from agent_comms.coordination_snapshot import RecoverySnapshot
from agent_comms.coordination_tables.attempts import AttemptRecord, ReplayFact
from agent_comms.coordination_tables.executions import ReplayAssessments
from agent_comms.coordination_tables.executions import CurrentExecutions
from agent_comms.coordination_tables.recovery import (
    RecoveryAudit,
)
from agent_comms.coordinator import Coordination
from agent_comms.errors import RelationViolationError
from agent_comms.native_runtime_input import NativeRuntimeInput
from agent_comms.recovery_states import DeferredRecovery, FailedRecovery

from .coordination_database import CoordinationStore
from .native_input_record import FullNativeExecution, TriageNativeExecution
from .private_send_stage import TriageNativeSend
from .lifecycle_capability import Deferred
from agent_comms.typed_table import RowNotFoundError
from .field_codec import MalformedRecord
from .input_disposition import InputDispositions
from .locked_store import RegistryFile
from .registration import Registration
from .threads import Thread

if TYPE_CHECKING:
    from agent_comms.coordination_session import NativeBoundary
    from agent_comms.owner_lifecycle import OwnerRelease, OwnerReleaseEvidence, OwnerReleaseStore

_OWNER_LOSS_ISSUER = object()


@dataclass(frozen=True, slots=True)
class VerifiedOwnerLoss(ABC):
    """A native owner's attested release, valid only inside its registry lock."""

    release: OwnerReleaseEvidence
    _store: Coordination = field(repr=False)
    boundary: NativeBoundary = field(repr=False)
    _issuer: object = field(default=_OWNER_LOSS_ISSUER, init=False, repr=False)
    _active: bool = field(default=True, init=False, repr=False)

    @classmethod
    @contextmanager
    def _observing(cls, store: Coordination) -> Iterator[tuple[OwnerReleaseStore, NativeBoundary]]:
        from agent_comms.comms import wire
        from agent_comms.native_runtime_input import NativeRuntimeTable

        if store.session.path.name != CoordinationStore.filename:
            raise RecoveryBlocked("native recovery requires the canonical coordination store")
        comms = wire(store.session.path.parent)
        with store.session.boundary(comms.bus) as boundary:
            NativeRuntimeTable.require_installed(store.session._connection)
            yield comms.owners.releases, boundary

    @staticmethod
    def _released(
        releases: OwnerReleaseStore, boundary: NativeBoundary, lookup: str,
    ) -> OwnerRelease:
        try:
            return releases.get(boundary.registry.by_lookup[lookup].name)
        except (OSError, RelationViolationError, MalformedRecord) as error:
            raise RecoveryBlocked("native owner release acknowledgement is invalid") from error

    @contextmanager
    def observed(self) -> Iterator[Self]:
        try:
            self.require_native_exit()
            yield self
        finally:
            object.__setattr__(self, "_active", False)

    @classmethod
    @contextmanager
    def observe_owner_release(cls, store: Coordination, owner: Thread) -> Iterator[ReleasedOwnerLoss]:
        with cls._observing(store) as (releases, boundary):
            identity = store.participants.get(owner.incarnation.lookup).owner_identity
            release = cls._released(releases, boundary, identity.owner_lookup)
            if not release.matches(owner):
                raise RecoveryBlocked("triage recovery requires this exact released owner")
            if not release.matches(boundary.registry.require(release.name)):
                raise RecoveryBlocked("triage recovery must precede a successor's registration")
            release = release.require_owner_loss(boundary.registry, identity)
            proof = ReleasedOwnerLoss(release, store, boundary)
            with proof.observed():
                yield proof

    @classmethod
    @contextmanager
    def observe_native_release(
        cls, store: Coordination, execution_id: str
    ) -> Iterator[VerifiedAttemptOwnerLoss]:
        """Compare the recorded native send, released owner and live registry.

        Keep canonical wire/bus/registry exclusion through monitor settlement.
        The release records the original process identity (pid and start_time).
        That process must be dead and no longer own the registry entry, and its
        native subprocesses must have exited. A successor's claim counter or
        active turn does not establish the predecessor's lifetime. Missing
        input still settles UNKNOWN, never unsent or safe to replay.
        """
        with cls._observing(store) as (releases, boundary):
            registry = boundary.registry
            snapshot = store.get(execution_id)
            attempt = snapshot.attempt
            if attempt is None or not snapshot.is_current:
                raise RecoveryBlocked("native recovery requires the current attempted execution")
            release = cls._released(releases, boundary, attempt.owner_lookup)
            try:
                try:
                    source = NativeRuntimeInput.require(
                        store.session._connection,
                        **FullNativeExecution(execution_id, attempt.attempt_ordinal).binding_fields(),
                    )
                except RowNotFoundError:
                    release = release.require_owner_loss(registry, attempt.owner_identity)
                    proof = UndispatchedOwnerLoss(release, store, boundary, attempt=attempt)
                else:
                    if source.owner_identity != attempt.owner_identity:
                        raise RecoveryBlocked("native attempt has a conflicting dispatched owner")
                    release = release.require_native_loss(registry, source)
                    proof = DispatchedOwnerLoss(release, store, boundary, attempt=attempt, native_input=source)
            except (ValueError, IdentityConflict, RelationViolationError) as error:
                raise RecoveryBlocked(f"native owner release does not prove loss of this claim_scope: {error}") from error
            with proof.observed():
                yield proof


    def issued_for(self, store: Coordination) -> bool:
        """Whether this live proof was issued while excluding this store's owner."""
        return self._issuer is _OWNER_LOSS_ISSUER and self._active and self._store is store

    def require_native_exit(self) -> None:
        self.require_session_exit(self.recovery_session_files())

    def require_session_exit(self, session_files: tuple[Path, ...]) -> None:
        """Every released input excludes the same native child resources."""
        if not self.issued_for(self._store):
            raise RecoveryBlocked("native exit observation requires acquired owner loss")
        session_dir = (
            self._store.session.path.parent / "native-sessions" / self.release.incarnation().lookup
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

    def recovery_session_files(self) -> tuple[Path, ...]:
        owner = self.boundary.registry.require(self.release.name)
        return tuple(files.session for files in owner.runtime.storage_files())


class ReleasedOwnerLoss(VerifiedOwnerLoss):
    def require_input_loss(self, source: NativeRuntimeInput) -> None:
        self.release.require_native_loss(self.boundary.registry, source)
        current = self.boundary.registry.require(self.release.name)
        self.require_session_exit(
            source.sent_owner_claimed_generation.recovery_session_files(source, current)
        )


@dataclass(frozen=True, slots=True)
class VerifiedAttemptOwnerLoss(VerifiedOwnerLoss):
    attempt: AttemptRecord = field(kw_only=True)

    def owns(self, store: Coordination, attempt: AttemptRecord) -> bool:
        """Only this acquired, unexpired observer attests the original attempt."""
        return (
            self.issued_for(store)
            and self.attempt.authority == attempt.authority
            and self.attempt.owner_identity == attempt.owner_identity
        )

    @abstractmethod
    def settle(self, recovery_cls, store: Coordination, execution_id: str, owner) -> None: ...

    @abstractmethod
    def require_native_input(self) -> NativeRuntimeInput: ...


@dataclass(frozen=True, slots=True)
class UndispatchedOwnerLoss(VerifiedAttemptOwnerLoss):
    """No input row was recorded; original process loss still leaves UNKNOWN."""

    def require_native_input(self) -> NativeRuntimeInput:
        raise RecoveryBlocked("native attempt has no original input")

    def settle(self, recovery_cls, store: Coordination, execution_id: str, owner) -> None:
        recovery_cls.abandon_released_native_attempt(store, execution_id)


@dataclass(frozen=True, slots=True)
class DispatchedOwnerLoss(VerifiedAttemptOwnerLoss):
    """The recorded input owns generation and original session observations."""

    native_input: NativeRuntimeInput = field(kw_only=True)

    def require_native_input(self) -> NativeRuntimeInput:
        return self.native_input

    def settle(self, recovery_cls, store: Coordination, execution_id: str, owner) -> None:
        from agent_comms.coordination_response import PublicationAppendDispatches

        self.require_native_exit()
        PublicationAppendDispatches.resolve_retained(
            store, self.boundary.bus, self.boundary,
            owner_lookup=owner.incarnation.lookup,
            execution_id=execution_id,
        )
        pointer = CurrentExecutions.require(store.session._connection, owner_lookup=owner.incarnation.lookup)
        if pointer.unresolved:
            execution_id = pointer.execution_id.require("the owner pointer names no execution")
            try:
                recovery_cls.recover_released_native_attempt(store, execution_id)
            except RecoveryBlocked:
                recovery_cls.abandon_released_native_attempt(store, execution_id)

    def recovery_session_files(self) -> tuple[Path, ...]:
        """A dispatched input retains its original selected session."""
        return self.native_input.sent_owner_claimed_generation.recovery_session_files(
            self.native_input, self.boundary.registry.require(self.release.name)
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class MonitorEvidence:
    """Observed Pi RPC child exit and backend finality, NOT owner-loss proof."""

    subprocess_dead: bool
    backend_done: bool
    reason_code: GivenReasonCode
    observed_at_ms: int
    outcome: UnsuccessfulTerminal

    def __post_init__(self) -> None:
        if any(
            type(value) is not bool
            for value in (self.subprocess_dead, self.backend_done)
        ):
            raise ValueError("monitor observations must be explicit booleans")
        if self.observed_at_ms < 0:
            raise ValueError("negative evidence timestamp")


_MONITOR_GRANT = object()


class RecoveryMonitorCapability:
    """Separate attested dead-attempt authority; never a live owner fence.

    Construction is deliberately unavailable from Coordination's public API.
    Recovery entry points observe owner release and local backend finality
    themselves. Registration, tokens and snapshots cannot mint that grant.
    """

    def __init__(self, store: Coordination, *, _grant: object) -> None:
        if _grant is not _MONITOR_GRANT:
            raise PermissionError("recovery monitor requires trusted construction")
        self._store = store

    @classmethod
    def retire_check_root(cls, root: Path) -> None:
        """Discard a stopped check, but keep uncertain new work for inspection.

        The caller has stopped its private owners through their normal recovery.
        Moving the small directory keeps sessions, diagnostics and SQLite/WAL
        evidence together without copying a store or replaying an input.
        """
        uncertain = False
        if (CoordinationStore.path_in(root)).is_file():
            with Coordination(str(CoordinationStore.path_in(root))) as store:
                uncertain = any(attempt.lifecycle.preserve_check
                                for attempt in AttemptRecord.select(store.session._connection))
        inputs = InputDispositions(root)
        if inputs.path.is_file():
            names = frozenset(Registration(RegistryFile.path_in(root)).snapshot().threads)
            uncertain = uncertain or bool(inputs.unresolved(names).rows)
        if uncertain:
            destination = root.with_name(f"{root.name}.unknown-{uuid4().hex}")
            root.rename(destination)
            raise RecoveryBlocked(
                f"Check directory removed; uncertain work kept at {destination}; do not replay"
            )
        shutil.rmtree(root)

    @classmethod
    def settle_stopped_owner(cls, root: Path, owner) -> None:
        """An explicit stop retires only the exited owner's original local attempt."""
        from .comms import wire

        comms = wire(root)
        acknowledgement = comms.owners.releases.get(owner.name)
        if not acknowledgement.matches(owner):
            raise RecoveryBlocked("stopped attempt requires the exact exited owner release")
        store = comms.goals.open_goal_store()
        owner.goal.stopped_permit(store).stop(store)
        if not (CoordinationStore.path_in(root)).exists():
            return  # Goal claims can exist without a selected native execution.
        cls.recover_owner(root, owner)

    @classmethod
    def recover_owner(cls, root: Path, owner) -> None:
        """Resolve an exited predecessor once, before this worker begins draining."""
        path = CoordinationStore.path_in(root)
        if not path.exists():
            return
        with Coordination(str(path)) as store:
            cls.recover_released_triage(store, owner)
            lookup = owner.incarnation.lookup
            pointer = CurrentExecutions.one(store.session._connection, owner_lookup=lookup)
            if pointer is None or not pointer.unresolved:
                return
            execution_id = pointer.execution_id.require("the owner pointer names no execution")
            with VerifiedOwnerLoss.observe_native_release(store, execution_id) as loss:
                loss.settle(cls, store, execution_id, owner)

    @classmethod
    def recover_released_triage(cls, store: Coordination, owner: Thread) -> None:
        identity = store.participants.get(owner.incarnation.lookup).owner_identity
        inputs = NativeRuntimeInput.select(
            store.session._connection,
            where="stage=? AND owner_lookup=? AND owner_generation<=? AND session_entry_id IS NULL",
            parameters=(TriageNativeExecution.declared_name, identity.owner_lookup, identity.generation),
        )
        pending = []
        for source in inputs:
            assignments = tuple(store.assignments.get(assignment) for assignment in
                                source.execution.source_assignment_ids(store.session._connection, source.input_id))
            if assignments and all(row.lifecycle.items_with(Deferred, row.lifecycle) for row in assignments):
                pending.append((source, TriageNativeSend(assignments)))
        if not pending:
            return
        with VerifiedOwnerLoss.observe_owner_release(store, owner) as loss:
            for source, stage in pending:
                current = NativeRuntimeInput.require(store.session._connection, input_id=source.input_id)
                if current.reference.recorded:
                    continue
                loss.require_input_loss(current)
                with store.session.writing() as db:
                    stage.fail_claims(store, db)

    @classmethod
    def recover_released_native_attempt(cls, store, execution_id):
        """Settle the old input without re-running it or fabricating delivery."""
        from .native_entries import NativeEntry
        from .native_pi import NativeContextProof
        from .native_prompt_binding import expected_prompt_bindings, expected_prompt_matches_journal
        from .pi_vocabulary import PendingStopReason
        from .private_path import FileRevision, MissingFile

        snapshot = store.get(execution_id)
        attempt = snapshot.require_current_attempt()
        source = NativeRuntimeInput.one(
            store.session._connection,
            **FullNativeExecution(execution_id, attempt.attempt_ordinal).binding_fields(),
        )
        if source is None:
            raise RecoveryBlocked("native attempt has no original input")
        terminal = PendingStopReason
        revision = MissingFile()
        session = source.selected_session
        if session is not None:
            with expected_prompt_bindings(store, (source.input_id,)) as bindings:
                binding = bindings.get(source.input_id)
                if binding is None or binding.identity != source.identity:
                    raise RecoveryBlocked("native recovery lacks its bound original input")
            revision = FileRevision.capture(session.path)
            with NativeEntry.open_evidence(session.path) as evidence:
                if not expected_prompt_matches_journal(session.path, binding, evidence=evidence):
                    raise RecoveryBlocked("native recovery input does not match its binding")
                try:
                    proof = NativeContextProof.read_evidence(
                        NativeSessionFiles.for_identity(session, root=store.session.path.parent,
                                           owner=source.owner_identity),
                        source.input_id, evidence=evidence,
                        session_entry_start=source.require_context_proof().session_entry_start,
                    )
                except (NativePiUnavailable, UnrecordedNativeContext):
                    # No conclusive context proof never means the input was unsent.
                    terminal = PendingStopReason
                else:
                    session.require_context(proof)
                    terminal = evidence.stopped_input(source.input_id)
        with VerifiedOwnerLoss.observe_native_release(store, execution_id) as loss:
            if loss.require_native_input() != source:
                raise StaleRevision("native input changed during recovery")
            loss.require_native_exit()
            for kept in revision.revisions():
                if FileRevision.capture(session.path) != kept:
                    raise StaleRevision("native journal changed during recovery")
            current = store.get(execution_id)
            attempt = current.require_current_attempt()
            outcome, replay_facts = terminal.unpublished_attempt()
            return cls(store, _grant=_MONITOR_GRANT).terminalize_dead_attempt(
                execution_id, attempt.attempt_ordinal, attempt.owner_generation,
                expected_attempt_revision=attempt.revision,
                expected_execution_revision=current.execution.revision,
                expected_pointer_revision=current.pointer_revision,
                owner_loss=loss,
                # A recorded terminal is still not permission to replay effects.
                replay_facts=replay_facts,
                evidence=MonitorEvidence(
                    subprocess_dead=True, backend_done=True,
                    outcome=outcome,
                    reason_code=GivenReasonCode("released_native_" + terminal.declared_name),
                    observed_at_ms=int(time.time() * 1000),
                ),
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
        UNKNOWN effects and releases its slot. Input evidences/cursors are never
        changed. A frozen publication still requires its acknowledgement resolver.

        A live successor need not stop: loss belongs to the original process,
        while its UNKNOWN input and all evidence remain untouched.
        """
        with VerifiedOwnerLoss.observe_native_release(store, execution_id) as loss:
            snapshot = store.get(execution_id)
            attempt = snapshot.attempt
            assert attempt is not None  # Required by the release observer.
            if bool(snapshot.publication_intents):
                raise PublicationUncertain("UNKNOWN abandonment cannot resolve frozen publication")
            loss.require_native_exit()
            return cls(store, _grant=_MONITOR_GRANT).terminalize_dead_attempt(
                execution_id,
                attempt.attempt_ordinal,
                attempt.owner_generation,
                expected_attempt_revision=attempt.revision,
                expected_execution_revision=snapshot.execution.revision,
                expected_pointer_revision=snapshot.pointer_revision,
                owner_loss=loss,
                evidence=MonitorEvidence(
                    subprocess_dead=True,
                    # Explicitly abandon the fenced, exited LOCAL backend.
                    # This is not evidence of a provider response or its effects.
                    backend_done=True,
                    outcome=UnknownOutcomeAttempt(),
                    reason_code=GivenReasonCode("released_native_unknown"),
                    observed_at_ms=int(time.time() * 1000),
                ),
            )

    @classmethod
    def recover_native_failure(
        cls, store: Coordination, execution_id: str, session_file: Path
    ) -> Applied[RecoverySnapshot]:
        """Retire a released owner's failed native input without recovering acceptance.

        A saved error is evidence of failure only: no live context acknowledgement is
        reconstructed, no cursor advances, and UNKNOWN effects remain unsafe
        to replay. An unfinished journal or unresolved publication is refused.
        """
        from agent_comms.native_entries import NativeEntry
        from agent_comms.native_pi import NativeContextProof
        from agent_comms.native_prompt_binding import (
            expected_prompt_bindings,
            expected_prompt_matches_journal,
        )

        with VerifiedOwnerLoss.observe_native_release(store, execution_id) as loss:
            snapshot = store.get(execution_id)
            attempt = snapshot.attempt
            assert attempt is not None  # The release observer requires an attempt.
            if bool(snapshot.publication_intents):
                raise PublicationUncertain("native failure cannot resolve frozen publication")
            reserved = loss.require_native_input()
            original_session = reserved.require_session_identity()
            session_file = Path(session_file).absolute()
            original_session.require_session(str(session_file))
            with expected_prompt_bindings(store, (reserved.input_id,)) as bindings:
                binding = bindings.get(reserved.input_id)
                if binding is None or binding.identity != reserved.identity:
                    raise RecoveryBlocked("native failure lacks its bound original input")
            with NativeEntry.open_evidence(session_file) as evidence:
                if not expected_prompt_matches_journal(session_file, binding, evidence=evidence):
                    raise RecoveryBlocked("native failure lacks its bound original input")
                proof = NativeContextProof.read_evidence(
                    NativeSessionFiles.for_identity(original_session, root=store.session.path.parent,
                                       owner=reserved.owner_identity),
                    reserved.input_id, evidence=evidence,
                    session_entry_start=reserved.require_context_proof().session_entry_start,
                )
                original_session.require_context(proof)
                _header, entries = evidence.observe()
                user_index = next(
                    index for index, entry in enumerate(entries) if entry.id == proof.session_entry_id
                )
                following = entries[user_index + 1 :]
                if len(following) != 1:
                    raise RecoveryBlocked("native failure has unfinished or additional session work")
                terminal = following[0]
                try:
                    terminal.require_failed_terminal(proof.session_entry_id)
                except ValueError as error:
                    raise RecoveryBlocked(str(error)) from error
                loss.require_native_exit()
                monitor = cls(store, _grant=_MONITOR_GRANT)
                return monitor.terminalize_dead_attempt(
                    execution_id,
                    attempt.attempt_ordinal,
                    attempt.owner_generation,
                    expected_attempt_revision=attempt.revision,
                    expected_execution_revision=snapshot.execution.revision,
                    expected_pointer_revision=snapshot.pointer_revision,
                    owner_loss=loss,
                    evidence=MonitorEvidence(
                        subprocess_dead=True,
                        backend_done=True,
                        outcome=UnknownOutcomeAttempt(),
                        reason_code=GivenReasonCode("released_native_failure"),
                        observed_at_ms=int(time.time() * 1000),
                    ),
                )

    def terminalize_dead_attempt(
        self,
        execution_id: str,
        ordinal: int,
        owner_generation: int,
        *,
        expected_attempt_revision: int,
        expected_execution_revision: int,
        expected_pointer_revision: int,
        evidence: MonitorEvidence,
        owner_loss: VerifiedAttemptOwnerLoss,
        replay_facts: ReplayFact = ReplayFact.NONE,
    ) -> Applied[RecoverySnapshot]:
        if not evidence.subprocess_dead:
            raise RecoveryBlocked("monitor cannot record a live Pi RPC subprocess as dead")
        store = self._store
        replay_facts = ReplayFact(replay_facts)
        if not owner_loss.issued_for(store):
            raise RecoveryBlocked("monitor requires an observed release of this exact owner")
        with store.session.settlement(owner_loss.boundary) as boundary:
            db = store.session._connection
            snapshot = store.get(execution_id)
            attempt = snapshot.attempt
            if (
                not snapshot.is_current
                or attempt is None
                or attempt.attempt_ordinal != ordinal
                or attempt.owner_generation != owner_generation
            ):
                raise StaleFence("monitor must name the exact old active attempt")
            if (attempt.revision, snapshot.execution.revision, snapshot.pointer_revision) != (
                expected_attempt_revision,
                expected_execution_revision,
                expected_pointer_revision,
            ):
                raise StaleRevision("monitor CAS is stale")
            if not owner_loss.owns(store, attempt):
                raise RecoveryBlocked("monitor requires an observed release of this exact owner")
            now = store.session.now(max(attempt.updated_at_ms, evidence.observed_at_ms))
            ReplayAssessments.accumulate(
                db, execution_id, replay_facts | evidence.outcome.replay_contribution
            )
            AttemptRecord.update(
                db,
                where="execution_id=? AND attempt_ordinal=?",
                parameters=(execution_id, ordinal),
                lifecycle=replace(
                    attempt.lifecycle,
                    process_dead=True,
                    backend_done=attempt.lifecycle.backend_done or evidence.backend_done,
                ),
                revision=attempt.revision + 1,
                updated_at_ms=now,
                reason_code=evidence.reason_code,
            )
            unresolved = bool(snapshot.publication_intents)
            if (attempt.lifecycle.backend_done or evidence.backend_done) and not unresolved:
                store.get(execution_id).settle(
                    store.session, evidence.outcome, evidence.reason_code, boundary
                )
                settled = store.get(execution_id)
                kind = DeferredRecovery if settled.execution.lifecycle.retry else FailedRecovery
                RecoveryAudit(
                    execution_id=execution_id,
                    kind=kind,
                    reason_code=evidence.reason_code,
                    sanitized_detail=NoDetail(),
                    attempt=ordinal,
                    elapsed_ms=0,
                    observed_at_ms=now,
                ).insert(db)
                return Applied(store.get(execution_id))
            # Death and genuinely observed backend completion commit even when
            # publication remains unresolved.  Neither state clears the pointer.
        if unresolved and (attempt.lifecycle.backend_done or evidence.backend_done):
            raise PublicationUncertain("frozen publication requires bus-keyed acknowledgement")
        raise RecoveryBlocked("process death alone is not backend-final proof")
