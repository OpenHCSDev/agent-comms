"""Attempt recovery: owned coordinator state and transitions."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from agent_comms.coordination_contracts import (
    MAX_REASON_CODE_CHARS,
)
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
        from agent_comms.coordination_response import _response_boundary

        if store.session.path.name != "coordination.sqlite3":
            raise RecoveryBlocked("native recovery requires the canonical coordination store")
        comms = Comms(store.session.path.parent)
        with _response_boundary(comms.bus) as registry:
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
            ):
                object.__setattr__(proof, name, value)
            try:
                yield proof
            finally:
                object.__setattr__(proof, "_active", False)


    def owns(self, store: Coordination, attempt: AttemptRecord) -> bool:
        """Only this acquired, unexpired observer attests the original attempt."""
        try:
            if self._issuer is not _OWNER_LOSS_ISSUER or not self._active or self._store is not store:
                return False
            return self.attempt.authority == attempt.authority and self.attempt.owner_identity == attempt.owner_identity
        except AttributeError:
            return False  # Uninitialized objects have no acquired issuer/custody.

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


@dataclass(frozen=True, slots=True, kw_only=True)
class MonitorEvidence:
    """Observed Pi RPC child exit and backend finality, NOT owner-loss proof."""

    subprocess_dead: bool
    backend_done: bool
    reason_code: str
    observed_at_ms: int
    unknown_effects: bool = True

    def __post_init__(self) -> None:
        if any(
            type(value) is not bool
            for value in (self.subprocess_dead, self.backend_done, self.unknown_effects)
        ):
            raise ValueError("monitor observations must be explicit booleans")
        if not 1 <= len(self.reason_code) <= MAX_REASON_CODE_CHARS:
            raise ValueError("monitor reason must be bounded and nonempty")
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
                    unknown_effects=True,
                    reason_code="released_native_unknown",
                    observed_at_ms=int(time.time() * 1000),
                ),
            )

    @classmethod
    def recover_native_failure(
        cls, store: Coordination, execution_id: str, session_file: Path
    ) -> Applied[RecoverySnapshot]:
        """Retire a released owner's failed native input without recovering acceptance.

        A saved error is evidence of failure only: no live context receipt is
        reconstructed, no cursor advances, and UNKNOWN effects remain unsafe
        to replay. An unfinished journal or unresolved publication is refused.
        """
        from agent_comms.native_entries import MessageEntry, NativeEntry
        from agent_comms.native_pi import NativeContextProof
        from agent_comms.native_prompt_binding import (
            expected_prompt_matches_journal,
            read_expected_prompt_binding,
        )

        with VerifiedOwnerLoss.observe_native_release(store, execution_id) as loss:
            snapshot = store.snapshots.get(execution_id)
            attempt = snapshot.attempt
            assert attempt is not None  # The release observer requires an attempt.
            if bool(snapshot.publication_intents):
                raise PublicationUncertain("native failure cannot resolve frozen publication")
            reserved = loss.native_input
            original_session = reserved.require_session_identity()
            session_file = Path(session_file).absolute()
            original_session.require_session(str(session_file))
            binding = read_expected_prompt_binding(store, reserved.input_id)
            if binding is None or binding.identity != reserved.identity:
                raise RecoveryBlocked("native failure lacks its bound original input")
            with NativeEntry.open_evidence(session_file) as evidence:
                if not expected_prompt_matches_journal(session_file, binding, evidence=evidence):
                    raise RecoveryBlocked("native failure lacks its bound original input")
                proof = NativeContextProof.read_evidence(
                    session_file, reserved.input_id, evidence=evidence
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
                if not isinstance(terminal, MessageEntry):
                    raise RecoveryBlocked("native recovery requires an unambiguous failed terminal")
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
                        unknown_effects=True,
                        reason_code="released_native_failure",
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
        owner_loss: VerifiedOwnerLoss | None = None,
        replay_facts: ReplayFact = ReplayFact.NONE,
    ) -> Applied[RecoverySnapshot]:
        if not evidence.subprocess_dead:
            raise RecoveryBlocked("monitor cannot record a live Pi RPC subprocess as dead")
        store = self._store
        replay_facts = ReplayFact(replay_facts)
        with store.session.transaction() as db:
            snapshot = store.snapshots.get(execution_id)
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
            if not isinstance(owner_loss, VerifiedOwnerLoss) or not owner_loss.owns(store, attempt):
                raise RecoveryBlocked("monitor requires an observed release of this exact owner")
            now = store.session.now(max(attempt.updated_at_ms, evidence.observed_at_ms))
            self._record_replay_observation(snapshot, evidence, replay_facts)
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
                from .attempt_states import AttemptFailedAttempt

                store.snapshots.get(execution_id).settle(
                    store.session, AttemptFailedAttempt(), evidence.reason_code
                )
                settled = store.snapshots.get(execution_id)
                kind = DeferredRecovery if settled.execution.lifecycle.retry else FailedRecovery
                RecoveryAudit(
                    execution_id=execution_id,
                    kind=kind,
                    reason_code=evidence.reason_code,
                    sanitized_detail=None,
                    attempt=ordinal,
                    elapsed_ms=0,
                    observed_at_ms=now,
                ).insert(db)
                return Applied(store.snapshots.get(execution_id))
            # Death and genuinely observed backend completion commit even when
            # publication remains unresolved.  Neither state clears the pointer.
        if unresolved and (attempt.lifecycle.backend_done or evidence.backend_done):
            raise PublicationUncertain("frozen publication requires bus-keyed receipt")
        raise RecoveryBlocked("process death alone is not backend-final proof")

    def _record_replay_observation(
        self, snapshot: RecoverySnapshot, evidence: MonitorEvidence, facts: ReplayFact
    ) -> None:
        """Project observations monotonically; the attempt owner performs the write."""
        observed = facts | (
            ReplayFact.UNKNOWN_EFFECTS if evidence.unknown_effects else ReplayFact.NONE
        )
        ReplayAssessments.accumulate(
            self._store.session._connection, snapshot.execution.execution_id, observed
        )
