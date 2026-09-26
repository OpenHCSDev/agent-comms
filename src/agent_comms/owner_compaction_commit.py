"""Owner-scoped commit/journal bridge for isolated native integration.

Not wired into ACP or the deployed package. Only the trusted owner process may
call this API; model/tool JSON cannot supply an attestation or child command.
Runtime ingress integration, publication and canonical deployment remain activation gates.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from .compaction_journal import CompactionJournal, CompactionJournalError, CompactionOperation
from .declarations import (
    Message,
    RelationViolationError,
    Thread,
    ThreadRegistry,
    _store_lock,
    unique_wire_object,
)
from .input_disposition import InputDispositions
from .native_package import COMPACTION_HELPER, verify_native_package
from .owner_compaction_gate import OwnerCompactionAttestation
from .owner_compaction_prepare import NativePreparation, prepare_native_source
from .owner_compaction_process import (
    CompactionTransportUnknownError,
    require_deadline_support,
    run_authority_child,
)
from .session_fence import idle_session_writer_fence


@dataclass(frozen=True)
class CompactionSource:
    """Pre-summary observations, never authority or an accepted JSON receipt."""

    native_json: str
    wire_root: str
    thread: str
    owner_epoch: int
    turn_id: str
    goal_id: str
    goal_revision: int
    bus_revision: str
    input_revision: str
    pending_input_key: str | None = None
    settings_paths: tuple[str, ...] | None = None
    settings_revision: tuple[str, ...] | None = None


class OwnerCompactionCommit:
    """Trusted owner bridge. A returned UNKNOWN never grants another dispatch."""

    def __init__(self, registry_path: Path, package_dir: Path):
        require_deadline_support()
        self.root = registry_path.parent.resolve(strict=True)
        self.registry = ThreadRegistry(registry_path)
        self.inputs = InputDispositions(self.root)
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
        if owner.goal is None or owner.active_turn is None or owner.session_file is None:
            raise ValueError("Claimed goal owner with canonical session required")
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
            expected_goal_id=owner.goal.id,
            expected_goal_revision=owner.goal.revision,
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
                assert owner.active_turn is not None
                admission = owner.active_turn.admission_generation
                rows = self.inputs._read()
                pending = rows.get(pending_input_key) if pending_input_key else None
                if pending_input_key is not None and (
                    not pending_input_key.startswith("acp:")
                    or pending is None
                    or pending["sequence"] is not None
                    or pending["target"] != owner.name
                    or pending["owner"] != owner.name
                    or pending["admission"] != admission
                    or pending["status"] != "unknown"
                    or pending["turn_id"] is not None
                    or pending["native_id"] is not None
                    or pending["sent_text"] is not None
                ):
                    raise RelationViolationError("Original owner input already attempted")
                if admission is None or any(
                    row["owner"] == owner.name
                    and row["admission"] == admission
                    and row["status"] == "unknown"
                    and key != pending_input_key
                    for key, row in rows.items()
                ):
                    raise RelationViolationError("Unsettled owner input; compaction not dispatched")
            yield receipt, fd, (executor_fd, wire_fd, bus_fd, input_fd)

    @staticmethod
    def _ingress_revision(path: Path, *, bus: bool = False) -> str:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return "missing"
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 256 * 1024**2:
            raise RelationViolationError("Compaction ingress must be bounded regular storage")
        raw = path.read_bytes()
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
            except (ValueError, KeyError, TypeError, AttributeError) as error:
                raise RelationViolationError("Invalid compaction ingress bus") from error
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
            or len(paths) not in (2, 4)
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
        return CompactionSource(
            json.dumps(witness, sort_keys=True, separators=(",", ":")),
            f"{self.root}:{root.st_dev}:{root.st_ino}",
            receipt.thread,
            receipt.owner_epoch,
            receipt.turn_id,
            receipt.goal_id,
            receipt.goal_revision,
            self._ingress_revision(self.root / "bus.jsonl", bus=True),
            self._ingress_revision(self.inputs.path),
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

        Whole-store ingress revisions are conservative: even unrelated bus
        movement declines a candidate. No UNKNOWN input is replayed or resolved.
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
            "committed": {"entryId", "revision", "leafId"},
            "aborted-no-write": {"revision", "leafId"},
            "unknown": {"reason"},
        }[evidence["status"]]
        if set(evidence) != fields | {"status"} or any(
            type(evidence.get(key)) is not str or not evidence[key] for key in fields
        ):
            raise CompactionTransportUnknownError("Incomplete native outcome; never replay")
        if result.returncode != 0 and evidence["status"] != "unknown":
            raise CompactionTransportUnknownError("Inconsistent native outcome; never replay")
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
                witness=witness, payloadDigest=digest, owner=asdict(receipt), source=asdict(source)
            )
            commit_id = self.journal.begin(witness["sessionFile"], intent)
            request = dict(
                action="commit",
                witness=witness,
                summary=summary,
                tokensBefore=tokens_before,
                commit=dict(commitId=commit_id, payloadDigest=digest),
            )
            try:
                evidence = self._call(fd, request, timeout, retained)
            except Exception as error:
                # Includes launch/protocol errors: conservative even where no
                # write probably occurred. Cancellation leaves durable intent.
                evidence = dict(status="unknown", reason=str(error)[:1024])
            self.journal.resolve(
                commit_id,
                evidence["status"],
                evidence,
                publication=evidence["status"] == "committed",
            )
            return self.journal.get(commit_id)

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
                commit=dict(commitId=commit_id, payloadDigest=intent["payloadDigest"]),
            )
            try:
                evidence = self._call(fd, request, timeout, retained)
            except Exception as error:
                evidence = dict(status="unknown", reason=str(error)[:1024])
            self.journal.resolve(
                commit_id,
                evidence["status"],
                evidence,
                publication=evidence["status"] == "committed",
            )
            return self.journal.get(commit_id)
