"""Owner-scoped commit/journal bridge for isolated native integration.

ACP calls this bridge after capturing its pending original input. Only the
trusted owner process may call it; model/tool JSON cannot supply an attestation
or child command. The bridge commits before issuing a one-use input admission.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import struct
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from .backend import _session_revision
from .channels import ChannelCatalog
from .compaction_journal import (
    CompactionJournal,
    CompactionJournalError,
    CompactionOperation,
    SelectedSummaryAttempt,
)
from .declarations import (
    DeliveryScope,
    Message,
    RelationViolationError,
    Thread,
    _store_lock,
    unique_wire_object,
)
from .input_disposition import FutureInputQueue, InputDispositions
from .native_package import COMPACTION_HELPER, verify_native_package
from .owner_compaction_gate import OwnerCompactionAttestation
from .owner_compaction_prepare import NativePreparation, prepare_native_source
from .owner_compaction_process import (
    CompactionTransportUnknownError,
    require_deadline_support,
    run_authority_child,
)
from .owner_compaction_provider import valid_native_usage
from .registration import Registration
from .selected_summary_admission import SelectedAdmissionIdentity, SelectedSummaryAdmission
from .session_fence import idle_session_writer_fence


@dataclass(frozen=True)
class CompactionSource:
    """Pre-summary observations, never authority or an accepted JSON receipt."""

    native_json: str
    wire_root: str
    thread: str
    owner_epoch: int
    turn_id: str
    goal_id: str | None
    goal_revision: int | None
    bus_revision: str
    input_revision: str
    pending_input_key: str | None = None
    settings_paths: tuple[str, ...] | None = None
    settings_revision: tuple[str, ...] | None = None


class OwnerCompactionCommit:
    """Trusted owner bridge. A returned UNKNOWN never grants another dispatch."""

    def __init__(
        self,
        registry_path: Path,
        package_dir: Path,
        *,
        future_queue: FutureInputQueue | None = None,
    ):
        require_deadline_support()
        self.root = registry_path.parent.resolve(strict=True)
        self.registry = Registration(registry_path)
        self.inputs = InputDispositions(self.root)
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
    def _guard_arguments(owner: Thread, witness: dict) -> dict:
        if owner.active_turn is None or owner.session_file is None:
            raise ValueError("Claimed owner with canonical session required")
        keys = {"sessionId", "sessionFile", "leafId", "firstKeptEntryId", "revision"}
        if set(witness) != keys or any(
            type(value) is not str or not value for value in witness.values()
        ):
            raise ValueError("Exact native witness required")
        session = str(Path(owner.session_file).resolve(strict=True))
        if witness["sessionFile"] != session:
            raise ValueError("Native witness does not identify owner's canonical session")
        return dict(
            turn_id=owner.active_turn.id,
            expected_goal_id=owner.goal.id if owner.goal is not None else None,
            expected_goal_revision=owner.goal.revision if owner.goal is not None else None,
            # Legacy receipt field only, not authority. CompactionSource binds
            # actual native history plus canonical bus/input revisions below.
            correction_revision=0,
            session_file=session,
            session_leaf=witness["leafId"],
            session_revision=witness["revision"],
        )

    @contextmanager
    def _boundary(
        self,
        owner: Thread,
        epoch: int,
        witness: dict,
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
            self.registry.guard_owner_compaction(owner, epoch, **arguments) as (receipt, fd),
            _store_lock(self.inputs.path) as input_fd,
        ):
            if settled:
                self.inputs._compaction_rows_unlocked(owner, pending_input_key, self.future_queue)
            yield receipt, fd, (executor_fd, wire_fd, bus_fd, input_fd)

    @staticmethod
    def _ingress_revision(path: Path, *, bus: bool = False, delivery=None) -> str:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return hashlib.sha256(b"").hexdigest() if bus else "missing"
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 256 * 1024**2:
            raise RelationViolationError("Compaction ingress must be bounded regular storage")
        raw = path.read_bytes()
        selected = []
        if bus and raw:
            try:
                if not raw.endswith(b"\n"):
                    raise ValueError("Incomplete bus row")
                previous = 0
                for line in raw.splitlines():
                    message = Message.from_wire(
                        json.loads(line, object_pairs_hook=unique_wire_object)
                    )
                    if message.seq <= previous:
                        raise ValueError("Bus sequence is not increasing")
                    previous = message.seq
                    if delivery is None or delivery.delivers(message.sender, message.target):
                        selected.append(line)
            except (ValueError, KeyError, TypeError, AttributeError) as error:
                raise RelationViolationError("Invalid compaction ingress bus") from error
        if bus:
            return hashlib.sha256(b"\n".join(selected)).hexdigest()
        digest = hashlib.sha256(raw).hexdigest()
        return (
            f"{info.st_dev}:{info.st_ino}:{info.st_size}:"
            f"{info.st_mtime_ns}:{info.st_ctime_ns}:{digest}"
        )

    @classmethod
    def _settings_source(cls, paths: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if paths is None:
            return None
        if (
            type(paths) is not tuple
            or not paths
            or any(type(path) is not str or not Path(path).is_absolute() for path in paths)
        ):
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
        witness: dict,
        pending_input_key: str | None,
        settings_paths: tuple[str, ...] | None,
    ) -> CompactionSource:
        root = self.root.stat()
        snapshot = self.registry.store._read_unlocked().snapshot()
        owner = snapshot.threads[receipt.thread]
        delivery = DeliveryScope(
            owner.name,
            snapshot.aliases,
            ChannelCatalog(self.root / "channels.json", self.registry).targets_for(owner.tags),
        )
        rows = self.inputs._compaction_rows_unlocked(owner, pending_input_key, self.future_queue)
        return CompactionSource(
            json.dumps(witness, sort_keys=True, separators=(",", ":")),
            f"{self.root}:{root.st_dev}:{root.st_ino}",
            receipt.thread,
            receipt.owner_epoch,
            receipt.turn_id,
            receipt.goal_id,
            receipt.goal_revision,
            self._ingress_revision(self.root / "bus.jsonl", bus=True, delivery=delivery),
            hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest(),
            pending_input_key,
            settings_paths,
            self._settings_source(settings_paths),
        )

    def capture_source(
        self,
        owner: Thread,
        epoch: int,
        witness: dict,
        *,
        pending_input_key: str | None = None,
        settings_paths: tuple[str, ...] | None = None,
    ) -> CompactionSource:
        """Capture BEFORE generating a summary; no provider work under these locks.

        Owner-relevant ingress remains fenced. Exact live future queue receipts
        may wait through the summary; no UNKNOWN input is replayed or resolved.
        """
        witness = dict(witness)
        with self._boundary(owner, epoch, witness, pending_input_key=pending_input_key) as (
            receipt,
            _,
            _retained,
        ):
            if self.journal.unresolved(witness["sessionFile"]):
                raise CompactionJournalError(
                    "Unresolved native commit; reconcile before preparation"
                )
            return self._source(receipt, witness, pending_input_key, settings_paths)

    def prepare_source(
        self,
        owner: Thread,
        epoch: int,
        *,
        keep_recent_tokens: int | None = None,
        pending_input_key: str | None = None,
        settings_paths: tuple[str, ...] | None = None,
    ) -> tuple[NativePreparation, CompactionSource] | None:
        """Read Pi's saved cut point, then capture owner/ingress source before summarizing.

        The bounded recent-window override is for isolated tests. Preparation
        never invokes a provider or mutates a session, and does not grant a
        commit: the writer must still CAS against the saved native witness.
        """
        if owner.session_file is None:
            raise ValueError("Canonical saved session required")
        prepared = prepare_native_source(
            self.package_dir, owner.session_file, keep_recent_tokens=keep_recent_tokens
        )
        if prepared is None:
            return None
        source = self.capture_source(
            owner,
            epoch,
            prepared.witness,
            pending_input_key=pending_input_key,
            settings_paths=settings_paths,
        )
        return prepared, source

    def _call(
        self, fd: int, request: dict, timeout: float, retained_fds: tuple[int, ...] = ()
    ) -> dict:
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
        result = run_authority_child(
            [
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
            ],
            json.dumps(request, ensure_ascii=False, allow_nan=False).encode(),
            authority_fd=fd,
            timeout=timeout,
            retained_fds=retained_fds,
        )
        try:
            evidence = json.loads(result.stdout)
        except (ValueError, UnicodeError) as error:
            raise CompactionTransportUnknownError(
                "Unparseable native outcome; never replay"
            ) from error
        if not isinstance(evidence, dict) or evidence.get("status") not in {
            "committed",
            "aborted-no-write",
            "unknown",
        }:
            raise CompactionTransportUnknownError("Invalid native outcome; never replay")
        fields = {
            "committed": {"entryId", "revision", "leafId", "metadataDigest"},
            "aborted-no-write": {"revision", "leafId"},
            "unknown": {"reason"},
        }[evidence["status"]]
        if set(evidence) != fields | {"status"} or any(
            type(evidence.get(key)) is not str or not evidence[key] for key in fields
        ):
            raise CompactionTransportUnknownError("Incomplete native outcome; never replay")
        if result.returncode != 0 and evidence["status"] != "unknown":
            raise CompactionTransportUnknownError("Inconsistent native outcome; never replay")
        if evidence["status"] == "committed" and (
            len(evidence["metadataDigest"]) != 64
            or any(c not in "0123456789abcdef" for c in evidence["metadataDigest"])
        ):
            raise CompactionTransportUnknownError("Invalid native metadata receipt; never replay")
        return evidence

    def commit(
        self,
        owner: Thread,
        epoch: int,
        witness: dict,
        summary: str,
        tokens_before: int,
        *,
        source: CompactionSource,
        details: dict[str, list[str]] | None = None,
        usage: dict | None = None,
        selected_attempt: SelectedSummaryAttempt | None = None,
        timeout: float = 5,
    ) -> CompactionOperation:
        """Journal intent under authority, dispatch once, persist observed outcome."""
        witness = dict(witness)
        if type(source) is not CompactionSource:
            raise ValueError("Owner-captured pre-summary source required")
        if (
            type(summary) is not str
            or len(summary.encode()) > 262144
            or type(tokens_before) is not int
            or not 0 <= tokens_before <= 2**53 - 1
        ):
            raise ValueError("Bounded native compaction payload required")
        if details is not None and (
            type(details) is not dict
            or set(details) != {"readFiles", "modifiedFiles"}
            or any(
                type(paths) is not list
                or len(paths) > 256
                or any(type(path) is not str or not path or "\\0" in path for path in paths)
                for paths in details.values()
            )
        ):
            raise ValueError("Bounded native file operations required")
        try:
            encoded_details = (
                json.dumps(details, ensure_ascii=False, separators=(",", ":")).encode()
                if details is not None
                else b""
            )
        except UnicodeError as error:
            raise ValueError("Invalid native file operation encoding") from error
        if len(encoded_details) > 65536 or any(
            len(path.encode()) > 4096 for paths in (details or {}).values() for path in paths
        ):
            raise ValueError("Bounded native file operations required")
        if usage is not None and not valid_native_usage(usage):
            raise ValueError("Bounded native usage required")
        # Preserve the summary/cut digest and bind fileOps/usage separately
        # through the native marker, writer CAS and exact-ID reconciliation.
        # Hex-encoded UTF-8 paths and IEEE-754 big-endian costs avoid divergent
        # Python/JS JSON string escaping and floating-point formatting.
        metadata = [
            (
                [
                    [path.encode("utf-8").hex() for path in details["readFiles"]],
                    [path.encode("utf-8").hex() for path in details["modifiedFiles"]],
                ]
                if details is not None
                else None
            ),
            (
                [
                    *[
                        usage[key]
                        for key in ("input", "output", "cacheRead", "cacheWrite", "totalTokens")
                    ],
                    usage.get("reasoning"),
                    usage.get("cacheWrite1h"),
                    *[
                        struct.pack(">d", float(usage["cost"][key])).hex()
                        for key in ("input", "output", "cacheRead", "cacheWrite", "total")
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
            [summary, witness["firstKeptEntryId"], tokens_before],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with self._boundary(owner, epoch, witness, pending_input_key=source.pending_input_key) as (
            receipt,
            fd,
            retained,
        ):
            if source != self._source(
                receipt, witness, source.pending_input_key, source.settings_paths
            ):
                raise RelationViolationError("Compaction source changed; derive fresh evidence")
            intent = dict(
                witness=witness,
                payloadDigest=digest,
                metadataDigest=metadata_digest,
                owner=asdict(receipt),
                source=asdict(source),
            )
            if selected_attempt is not None:
                if (
                    self.journal.selected_summary(selected_attempt.operation_id) != selected_attempt
                    or selected_attempt.status != "reserved"
                    or selected_attempt.session_file != witness["sessionFile"]
                ):
                    raise CompactionJournalError(
                        "Selected summary reservation changed before commit"
                    )
                selected_source = json.loads(selected_attempt.source_json)["source"]
                if (
                    selected_source.get("ownerName") != owner.name
                    or selected_source.get("ownerPid") != owner.pid
                    or selected_source.get("ownerCreatedAt") != float(owner.created_at).hex()
                    or selected_source.get("turnId") != source.turn_id
                    or selected_source.get("ingressKey") != source.pending_input_key
                    or selected_source.get("reservedRevision")
                    != json.loads(json.dumps(_session_revision(witness["sessionFile"])))
                ):
                    raise CompactionJournalError("Selected summary owner or saved source differs")
                intent.update(
                    selectedSummaryOperationId=selected_attempt.operation_id,
                    selectedSummarySourceDigest=hashlib.sha256(
                        selected_attempt.source_json.encode()
                    ).hexdigest(),
                )
            commit_id = self.journal.begin(witness["sessionFile"], intent)
            request = dict(
                action="commit",
                witness=witness,
                summary=summary,
                tokensBefore=tokens_before,
                commit=dict(
                    commitId=commit_id, payloadDigest=digest, metadataDigest=metadata_digest
                ),
                **({"details": details} if details is not None else {}),
                **({"usage": usage} if usage is not None else {}),
            )
            try:
                evidence = self._call(fd, request, timeout, retained)
            except Exception as error:
                # Includes launch/protocol errors: conservative even where no
                # write probably occurred. Cancellation leaves durable intent.
                evidence = dict(status="unknown", reason=str(error)[:1024])
            if (
                evidence["status"] == "committed"
                and evidence.get("metadataDigest") != metadata_digest
            ):
                evidence = {"status": "unknown", "reason": "native-metadata-mismatch"}
            self.journal.resolve(
                commit_id,
                evidence["status"],
                evidence,
                publication=evidence["status"] == "committed",
            )
            return self.journal.get(commit_id)

    def admit_selected_decline(
        self,
        owner: Thread,
        epoch: int,
        attempt: SelectedSummaryAttempt,
        source: CompactionSource,
        identity: SelectedAdmissionIdentity,
        reason: str,
    ) -> SelectedSummaryAdmission:
        """Continue one original after a correlated, unchanged prestart decline."""
        witness = json.loads(source.native_json)
        with self._boundary(owner, epoch, witness, pending_input_key=source.pending_input_key) as (
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
                or attempt.session_file != witness["sessionFile"]
                or _session_revision(witness["sessionFile"]) != identity.reserved_revision
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
        epoch: int,
        operation: CompactionOperation,
        source: CompactionSource,
        identity: SelectedAdmissionIdentity,
    ) -> SelectedSummaryAdmission:
        """Link one committed result after rechecking the same owner and ingress."""
        if operation.status != "committed":
            raise CompactionJournalError("Selected native commit is not complete")
        intent = json.loads(operation.intent_json)
        witness = intent["witness"]
        with self._boundary(owner, epoch, witness, pending_input_key=source.pending_input_key) as (
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
            revision = _session_revision(witness["sessionFile"])
            evidence = json.loads(operation.evidence_json or "null")
            if (
                revision is None
                or not isinstance(evidence, dict)
                or evidence.get("revision") != ":".join(map(str, revision[0]))
                or revision[1] != identity.reserved_revision[1]
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
        self, owner: Thread, epoch: int, commit_id: str, *, timeout: float = 5
    ) -> CompactionOperation:
        """Explicit exact-ID observation. NEVER sends summary or a commit action."""
        operation = self.journal.get(commit_id)
        if operation.status not in {"intent", "unknown"}:
            return operation
        intent = json.loads(operation.intent_json)
        witness = intent["witness"]
        with self._boundary(owner, epoch, witness, settled=False) as (_, fd, retained):
            # Re-read after acquiring authority; a prior resolver may have won.
            current = self.journal.get(commit_id)
            if current.status not in {"intent", "unknown"}:
                return current
            if current.intent_json != operation.intent_json:
                raise CompactionJournalError("Compaction intent changed")
            request = dict(
                action="reconcile",
                witness=witness,
                commit=dict(
                    commitId=commit_id,
                    payloadDigest=intent["payloadDigest"],
                    metadataDigest=intent["metadataDigest"],
                ),
            )
            try:
                evidence = self._call(fd, request, timeout, retained)
            except Exception as error:
                evidence = dict(status="unknown", reason=str(error)[:1024])
            if (
                evidence["status"] == "committed"
                and evidence.get("metadataDigest") != intent["metadataDigest"]
            ):
                evidence = {"status": "unknown", "reason": "native-metadata-mismatch"}
            self.journal.resolve(
                commit_id,
                evidence["status"],
                evidence,
                publication=evidence["status"] == "committed",
            )
            return self.journal.get(commit_id)
