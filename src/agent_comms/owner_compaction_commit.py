"""Owner-scoped commit/journal bridge for isolated native integration.

ACP calls this bridge after capturing its pending original input. Only the
trusted owner process may call it; model/tool JSON cannot supply an attestation
or child command. The bridge commits before issuing a one-use input admission.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import stat
import struct
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path

from .backend import _session_revision
from .catalog_store import ChannelCatalog
from .child_process import BoundedRun, TimedOutOutcome
from .compaction_journal import (
    CompactionJournal,
    CompactionJournalError,
    CompactionOperation,
    SelectedSummaryAttempt,
)
from .compaction_states import CommittedNativeOutcome, NativeOutcome, UnknownNativeOutcome
from .errors import RelationViolationError
from .field_codec import FieldCodec, projected
from .input_disposition import FutureInputQueue, InputDispositions
from .native_package import COMPACTION_HELPER, verify_native_package
from .owner_compaction_gate import OwnerCompactionAttestation
from .owner_compaction_prepare import NativePreparation, NativeWitness, prepare_native_source
from .owner_compaction_settings import PiCompactionSettings
from .pi_summary_payloads import SummaryFiles, SummaryUsage
from .registration import Registration
from .reservation_rules import CommitReservationCheck
from .routing import DeliveryScope
from .selected_source import SelectedSource
from .selected_summary_admission import SelectedAdmissionIdentity, SelectedSummaryAdmission
from .session_fence import idle_session_writer_fence
from .store_files import _store_lock
from .text_digest import TextDigest
from .thread_identity import TurnId
from .threads import Thread
from .wire_log import WireLog


class CompactionTransportUnknownError(RuntimeError):
    """Native mutation may have occurred; only exact reconciliation can settle it."""


@dataclass(frozen=True)
class CompactionSource:
    """Pre-summary observations, never authority or an accepted JSON receipt."""

    native: NativeWitness = field(metadata={"journal_exclude": True})
    wire_root: str
    thread: str
    owner_generation: int = field(metadata={"wire_name": "owner_epoch"})
    turn_id: str
    goal_id: str | None
    goal_revision: int | None
    bus_revision: str
    input_revision: str
    pending_input_key: str | None = None
    settings_paths: tuple[str, ...] | None = None
    settings_revision: tuple[str, ...] | None = None

    @projected(view="journal", name="native_json")
    def encoded_native(self) -> str:
        return json.dumps(FieldCodec.encode(self.native), sort_keys=True, separators=(",", ":"))


class OwnerCompactionCommit:
    """Trusted owner bridge. A returned UNKNOWN never grants another dispatch."""

    def __init__(
        self,
        registry_path: Path,
        package_dir: Path,
        *,
        future_queue: FutureInputQueue | None = None,
    ):
        BoundedRun.require_inherited_deadline()
        self.root = registry_path.parent.resolve(strict=True)
        self.registry = Registration(registry_path)
        self.inputs = InputDispositions(self.root / InputDispositions.filename)
        self.future_queue = future_queue
        self.package_dir = package_dir.resolve(strict=True)
        self.helper = self.package_dir / "dist/agent-comms-compaction-commit-child.mjs"
        self.import_fence = self.package_dir / "dist/agent-comms-import-fence.mjs"
        node = shutil.which("node")
        if node is None:
            raise ValueError("Node executable unavailable")
        self.node = node
        environment_launcher = shutil.which("env")
        if environment_launcher is None:
            raise ValueError("Isolated native environment launcher unavailable")
        self.environment_launcher = environment_launcher
        self._verify_native()
        self.journal = CompactionJournal(registry_path.with_name("compaction-commits.sqlite3"))

    def _verify_native(self) -> None:
        verify_native_package(self.package_dir)
        copied_helper = self.package_dir / "dist/agent-comms-compaction-commit-child.mjs"
        if (
            not copied_helper.is_file()
            or copied_helper.read_bytes() != COMPACTION_HELPER.read_bytes()
        ):
            raise ValueError("Trusted compaction helper differs from packaged resource")
        if not self.import_fence.is_file():
            raise ValueError("Native import boundary unavailable")

    @staticmethod
    def _guard_arguments(owner: Thread, witness: NativeWitness) -> dict:
        if owner.active_turn is None or owner.session_file is None:
            raise ValueError("Claimed owner with canonical session required")
        session = str(Path(owner.session_file).resolve(strict=True))
        if witness.session_file != session:
            raise ValueError("Native witness does not identify owner's canonical session")
        return dict(
            turn_id=owner.active_turn.id,
            expected_goal_id=owner.goal.id if owner.goal is not None else None,
            expected_goal_revision=owner.goal.revision if owner.goal is not None else None,
            session_file=session,
            session_leaf=witness.leaf_id,
            session_revision=witness.revision,
        )

    @contextmanager
    def _boundary(
        self,
        owner: Thread,
        owner_generation: int,
        witness: NativeWitness,
        *,
        settled: bool = True,
        pending_input_key: str | None = None,
    ) -> Iterator[tuple[OwnerCompactionAttestation, int, tuple[int, ...]]]:
        arguments = self._guard_arguments(owner, witness)
        # Existing bus publication acquires bus BEFORE registry. Never invert
        # that edge, even though this scope does not yet publish an outcome.
        with (
            idle_session_writer_fence(arguments["session_file"]) as executor_fd,
            _store_lock(self.root / "wire") as wire_fd,
            _store_lock(self.root / "bus.jsonl") as bus_fd,
            self.registry.guard_owner_compaction(owner, owner_generation, **arguments) as (
                receipt,
                fd,
            ),
            self.inputs.locked() as input_fd,
        ):
            if settled:
                self.inputs._read_unlocked().compaction_rows(
                    owner, pending_input_key, self.future_queue
                )
            yield receipt, fd, (executor_fd, wire_fd, bus_fd, input_fd)

    @staticmethod
    def _ingress_revision(path: Path) -> str:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return "missing"
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RelationViolationError("Compaction ingress must be regular storage")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        return (
            f"{info.st_dev}:{info.st_ino}:{info.st_size}:"
            f"{info.st_mtime_ns}:{info.st_ctime_ns}:{digest}"
        )

    @classmethod
    def _settings_source(cls, paths: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if paths is None:
            return None
        if not paths or any(not Path(path).is_absolute() for path in paths):
            raise RelationViolationError("Exact effective settings paths required")
        states = []
        for path in paths:
            file = Path(path)
            try:
                info = file.lstat()
            except FileNotFoundError:
                states.append("missing")
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1048576:
                raise RelationViolationError("Unsafe effective settings source")
            states.append(cls._ingress_revision(file))
        return tuple(states)

    def _source(
        self,
        receipt: OwnerCompactionAttestation,
        witness: NativeWitness,
        pending_input_key: str | None,
        settings_paths: tuple[str, ...] | None,
    ) -> CompactionSource:
        root = self.root.stat()
        snapshot = self.registry.store._read_unlocked().snapshot()
        owner = snapshot.threads[receipt.thread]
        delivery = DeliveryScope(
            owner.name,
            snapshot.aliases,
            ChannelCatalog(self.root / ChannelCatalog.filename).read().targets_for(owner.tags),
        )
        rows = self.inputs._read_unlocked().compaction_rows(
            owner, pending_input_key, self.future_queue
        )
        return CompactionSource(
            witness,
            f"{self.root}:{root.st_dev}:{root.st_ino}",
            receipt.thread,
            receipt.owner_generation,
            receipt.turn_id,
            receipt.goal_id,
            receipt.goal_revision,
            WireLog(self.root / "bus.jsonl").delivery_revision_unlocked(delivery),
            TextDigest.of(json.dumps(FieldCodec.encode(rows), sort_keys=True)).value,
            pending_input_key,
            settings_paths,
            self._settings_source(settings_paths),
        )

    def capture_source(
        self,
        owner: Thread,
        owner_generation: int,
        witness: NativeWitness,
        *,
        pending_input_key: str | None = None,
        settings_paths: tuple[str, ...] | None = None,
    ) -> CompactionSource:
        """Capture BEFORE generating a summary; no provider work under these locks.

        Owner-relevant ingress remains fenced. Exact live future queue receipts
        may wait through the summary; no UNKNOWN input is replayed or resolved.
        """
        with self._boundary(
            owner, owner_generation, witness, pending_input_key=pending_input_key
        ) as (
            receipt,
            _,
            _retained,
        ):
            if self.journal.unresolved(witness.session_file):
                raise CompactionJournalError(
                    "Unresolved native commit; reconcile before preparation"
                )
            return self._source(receipt, witness, pending_input_key, settings_paths)

    def prepare_source(
        self,
        owner: Thread,
        owner_generation: int,
        *,
        settings: PiCompactionSettings,
        context_window: int,
        pending_input_key: str | None = None,
        settings_paths: tuple[str, ...] | None = None,
    ) -> tuple[NativePreparation, CompactionSource] | None:
        """Read Pi's saved cut point, then capture owner/ingress source before summarizing.

        Preparation uses exact selected settings/window, never invokes a
        provider or mutates a session, and does not grant a
        commit: the writer must still CAS against the saved native witness.
        """
        if owner.session_file is None:
            raise ValueError("Canonical saved session required")
        prepared = prepare_native_source(
            self.package_dir, owner.session_file, settings=settings, context_window=context_window
        )
        if prepared is None:
            return None
        self.reconcile_interrupted_summaries(owner, owner_generation, prepared.witness)
        source = self.capture_source(
            owner,
            owner_generation,
            prepared.witness,
            pending_input_key=pending_input_key,
            settings_paths=settings_paths,
        )
        return prepared, source

    def reconcile_interrupted_summaries(
        self, owner: Thread, owner_generation: int, witness: NativeWitness
    ) -> None:
        """Retire an interrupted summary only when no original input or write occurred.

        The provider outcome remains UNKNOWN. A later explicit input may request
        a new summary; neither this method nor the retired row replays anything.
        """
        with self._boundary(owner, owner_generation, witness, settled=False):
            for attempt in self.journal.selected_summaries(witness.session_file):
                if not attempt.state.reconcile_unchanged_source:
                    continue
                source = FieldCodec.decode(
                    SelectedSource, json.loads(attempt.source_json)["source"]
                )
                source.interrupted_check(
                    _session_revision(witness.session_file),
                    self.inputs._read_unlocked(),
                    owner.incarnation,
                    TurnId(owner.active_turn.id),
                ).require_valid()
                self.journal.retire_unchanged_summary(attempt)

    def _call(
        self, fd: int, request: dict, timeout: float, retained_fds: tuple[int, ...] = ()
    ) -> NativeOutcome:
        self._verify_native()
        held = os.fstat(fd)
        request = dict(
            request,
            authority=dict(
                parentPid=os.getpid(),
                device=str(held.st_dev),
                inode=str(held.st_ino),
            ),
        )
        if not math.isfinite(timeout) or not 0 < timeout <= 30:
            raise ValueError("Compaction child deadline must be in (0, 30] seconds")
        payload = json.dumps(request, ensure_ascii=False, allow_nan=False).encode("utf-8")
        try:
            result = BoundedRun.run_inherited(
                (
                    self.environment_launcher,
                    "-u",
                    "NODE_OPTIONS",
                    "-u",
                    "NODE_PATH",
                    "-u",
                    "NODE_COMPILE_CACHE",
                    "NODE_DISABLE_COMPILE_CACHE=1",
                    self.node,
                    "--no-global-search-paths",
                    "--import",
                    str(self.import_fence),
                    str(self.helper),
                    str(self.package_dir),
                    str(fd),
                    str(len(payload)),
                ),
                input=payload,
                pass_fds=tuple(dict.fromkeys((fd, *retained_fds))),
                deadline=time.monotonic() + timeout,
            )
        except (OSError, RuntimeError) as error:
            raise CompactionTransportUnknownError(
                f"Native commit transport UNKNOWN: {error}; never replay"
            ) from error
        if isinstance(result.outcome, TimedOutOutcome):
            raise CompactionTransportUnknownError("Native commit timed out; never replay")
        try:
            evidence = json.loads(result.stdout)
        except (ValueError, UnicodeError) as error:
            raise CompactionTransportUnknownError(
                "Unparseable native outcome; never replay"
            ) from error
        try:
            return FieldCodec.decode(NativeOutcome, evidence).checked_child(result.outcome)
        except (ValueError, TypeError) as error:
            raise CompactionTransportUnknownError(str(error)) from error

    def commit(
        self,
        owner: Thread,
        owner_generation: int,
        witness: NativeWitness,
        summary: str,
        tokens_before: int,
        *,
        source: CompactionSource,
        details: SummaryFiles | None = None,
        usage: SummaryUsage | None = None,
        selected_attempt: SelectedSummaryAttempt | None = None,
        timeout: float = 5,
    ) -> CompactionOperation:
        """Journal intent under authority, dispatch once, persist observed outcome."""
        if not summary.strip() or not 0 <= tokens_before <= 2**53 - 1:
            raise ValueError("Bounded native compaction payload required")
        # Preserve the summary/cut digest and bind fileOps/usage separately
        # through the native marker, writer CAS and exact-ID reconciliation.
        # Hex-encoded UTF-8 paths and IEEE-754 big-endian costs avoid divergent
        # Python/JS JSON string escaping and floating-point formatting.
        metadata = [
            (
                [
                    [path.encode("utf-8").hex() for path in details.read_files],
                    [path.encode("utf-8").hex() for path in details.modified_files],
                ]
                if details is not None
                else None
            ),
            (
                [
                    usage.input,
                    usage.output,
                    usage.cache_read,
                    usage.cache_write,
                    usage.total_tokens,
                    usage.reasoning,
                    usage.cache_write_1h,
                    *[
                        struct.pack(">d", float(amount)).hex()
                        for amount in (
                            usage.cost.input,
                            usage.cost.output,
                            usage.cost.cache_read,
                            usage.cost.cache_write,
                            usage.cost.total,
                        )
                    ],
                ]
                if usage is not None
                else None
            ),
        ]
        metadata_digest = hashlib.sha256(
            b"agent-comms-metadata-v1\n"
            + json.dumps(metadata, separators=(",", ":")).encode("ascii")
        ).hexdigest()
        payload = json.dumps(
            [summary, witness.first_kept_entry_id, tokens_before],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        digest = TextDigest.of(payload).value
        with self._boundary(
            owner, owner_generation, witness, pending_input_key=source.pending_input_key
        ) as (
            receipt,
            fd,
            retained,
        ):
            if source != self._source(
                receipt, witness, source.pending_input_key, source.settings_paths
            ):
                raise RelationViolationError("Compaction source changed; derive fresh evidence")
            intent = dict(
                witness=FieldCodec.encode(witness),
                payloadDigest=digest,
                metadataDigest=metadata_digest,
                owner=FieldCodec.encode(receipt),
                source=FieldCodec.project(source, "journal"),
            )
            if selected_attempt is not None:
                selected_attempt.state.require_commit_reservation()
                if (
                    self.journal.selected_summary(selected_attempt.operation_id) != selected_attempt
                    or selected_attempt.session_file != witness.session_file
                ):
                    raise CompactionJournalError(
                        "Selected summary reservation changed before commit"
                    )
                selected_source = FieldCodec.decode(
                    SelectedSource, json.loads(selected_attempt.source_json)["source"]
                )
                CommitReservationCheck(
                    source=selected_source,
                    revision=_session_revision(witness.session_file),
                    incarnation=owner.incarnation,
                    owner=owner.process_identity,
                    turn=TurnId(source.turn_id),
                    pending_input_key=source.pending_input_key,
                ).require_valid()
                intent.update(
                    selectedSummaryOperationId=selected_attempt.operation_id,
                    selectedSummarySourceDigest=TextDigest.of(selected_attempt.source_json).value,
                )
            commit_id = self.journal.begin(
                witness.session_file, intent, inputs=self.inputs._read_unlocked()
            )
            request = dict(
                action="commit",
                witness=FieldCodec.encode(witness),
                summary=summary,
                tokensBefore=tokens_before,
                commit=dict(
                    commitId=commit_id, payloadDigest=digest, metadataDigest=metadata_digest
                ),
                **({"details": FieldCodec.encode(details)} if details is not None else {}),
                **({"usage": FieldCodec.encode(usage)} if usage is not None else {}),
            )
            try:
                outcome = self._call(fd, request, timeout, retained)
            except Exception as error:
                # Includes launch/protocol errors: conservative even where no
                # write probably occurred. Cancellation leaves durable intent.
                outcome = UnknownNativeOutcome(str(error)[:1024])
            outcome = outcome.bind_metadata(metadata_digest)
            self.journal.resolve(
                commit_id,
                outcome.state,
                FieldCodec.encode(outcome),
                publication=outcome.state.committed,
            )
            return self.journal.get(commit_id)

    def admit_selected_decline(
        self,
        owner: Thread,
        owner_generation: int,
        attempt: SelectedSummaryAttempt,
        source: CompactionSource,
        identity: SelectedAdmissionIdentity,
        reason: str,
    ) -> SelectedSummaryAdmission:
        """Continue one original after a correlated, unchanged prestart decline."""
        witness = source.native
        with self._boundary(
            owner, owner_generation, witness, pending_input_key=source.pending_input_key
        ) as (
            receipt,
            _fd,
            _retained,
        ):
            if source != self._source(
                receipt, witness, source.pending_input_key, source.settings_paths
            ):
                raise RelationViolationError("Selected source changed before decline admission")
            if (
                self.journal.selected_summary(attempt.operation_id) != attempt
                or attempt.session_file != witness.session_file
                or _session_revision(witness.session_file) != identity.source.reserved_revision
            ):
                raise CompactionJournalError("Selected decline source changed")
            admission = self.journal.decline_selected_summary_prestart(
                attempt.operation_id, reason, admission=identity
            )
            assert admission is not None
            return admission

    def admit_selected_original(
        self,
        owner: Thread,
        owner_generation: int,
        operation: CompactionOperation,
        source: CompactionSource,
        identity: SelectedAdmissionIdentity,
    ) -> SelectedSummaryAdmission:
        """Link one committed result after rechecking the same owner and ingress."""
        if not operation.state.committed:
            raise CompactionJournalError("Selected native commit is not complete")
        intent = json.loads(operation.intent_json)
        witness = FieldCodec.decode(NativeWitness, intent["witness"])
        with self._boundary(
            owner, owner_generation, witness, pending_input_key=source.pending_input_key
        ) as (
            receipt,
            _fd,
            _retained,
        ):
            if source != self._source(
                receipt, witness, source.pending_input_key, source.settings_paths
            ):
                raise RelationViolationError("Selected source changed before original admission")
            current = self.journal.get(operation.commit_id)
            if current != operation:
                raise CompactionJournalError("Selected native commit changed")
            revision = _session_revision(witness.session_file)
            evidence = FieldCodec.decode(
                CommittedNativeOutcome, json.loads(operation.evidence_json or "null")
            )
            if (
                revision is None
                or evidence.revision != ":".join(map(str, revision[0]))
                or revision[1] != identity.source.reserved_revision[1]
            ):
                raise CompactionJournalError("Selected native result is unavailable")
            admission = self.journal.link_selected_summary_commit(
                intent["selectedSummaryOperationId"],
                operation.commit_id,
                admission=replace(identity, session_revision=revision),
            )
            assert admission is not None
            return admission

    def reconcile(
        self, owner: Thread, owner_generation: int, commit_id: str, *, timeout: float = 5
    ) -> CompactionOperation:
        """Explicit exact-ID observation. NEVER sends summary or a commit action."""
        operation = self.journal.get(commit_id)
        if operation.state.terminal:
            return operation
        intent = json.loads(operation.intent_json)
        witness = FieldCodec.decode(NativeWitness, intent["witness"])
        with self._boundary(owner, owner_generation, witness, settled=False) as (_, fd, retained):
            # Re-read after acquiring authority; a prior resolver may have won.
            current = self.journal.get(commit_id)
            if current.state.terminal:
                return current
            if current.intent_json != operation.intent_json:
                raise CompactionJournalError("Compaction intent changed")
            request = dict(
                action="reconcile",
                witness=FieldCodec.encode(witness),
                commit=dict(
                    commitId=commit_id,
                    payloadDigest=intent["payloadDigest"],
                    metadataDigest=intent["metadataDigest"],
                ),
            )
            try:
                outcome = self._call(fd, request, timeout, retained)
            except Exception as error:
                outcome = UnknownNativeOutcome(str(error)[:1024])
            outcome = outcome.bind_metadata(intent["metadataDigest"])
            self.journal.resolve(
                commit_id,
                outcome.state,
                FieldCodec.encode(outcome),
                publication=outcome.state.committed,
            )
            return self.journal.get(commit_id)
