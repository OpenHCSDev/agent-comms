"""Owner-scoped commit/journal bridge for isolated native integration.

Not wired into ACP or the deployed package. Only the trusted owner process may
call this API; model/tool JSON cannot supply an attestation or child command.
Correction/send barriers and canonical deployment remain activation gates.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import asdict
from pathlib import Path

from .compaction_journal import CompactionJournal, CompactionJournalError, CompactionOperation
from .declarations import Thread, ThreadRegistry
from .owner_compaction_process import CompactionTransportUnknownError, run_authority_child
from .session_fence import idle_session_writer_fence

NATIVE_MANAGER_SHA256 = "8ec0b8f1b62ee6abe3ba3c98e2f64b1efea549b7e27f561fad2516f955b7c49c"


class OwnerCompactionCommit:
    """Trusted owner bridge. A returned UNKNOWN never grants another dispatch."""

    def __init__(self, registry_path: Path, package_dir: Path):
        self.registry = ThreadRegistry(registry_path)
        self.journal = CompactionJournal(registry_path.with_name("compaction-commits.sqlite3"))
        self.package_dir = package_dir.resolve(strict=True)
        self.helper = (
            Path(__file__).resolve().parents[2] / "stack/native-compaction-commit-child.mjs"
        )
        node = shutil.which("node")
        if node is None:
            raise ValueError("Node executable unavailable")
        self.node = node
        self._verify_native()

    def _verify_native(self) -> None:
        manager = self.package_dir / "dist/core/session-manager.js"
        if hashlib.sha256(manager.read_bytes()).hexdigest() != NATIVE_MANAGER_SHA256:
            raise ValueError("Expected pinned native commit/reconciliation artifact")
        if not self.helper.is_file():
            raise ValueError("Trusted compaction helper unavailable")

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
            # No global correction ledger is invented here. Native disk CAS
            # binds persisted changes only; in-flight ingress is a later gate.
            correction_revision=0,
            session_file=session,
            session_leaf=witness["leafId"],
            session_revision=witness["revision"],
        )

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
            [self.node, str(self.helper), str(self.package_dir), str(fd)],
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
        timeout: float = 5,
    ) -> CompactionOperation:
        """Journal intent under authority, dispatch once, persist observed outcome."""
        witness = dict(witness)
        arguments = self._guard_arguments(owner, witness)
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
        with (
            idle_session_writer_fence(arguments["session_file"]) as executor_fd,
            self.registry.guard_owner_compaction(owner, epoch, **arguments) as (receipt, fd),
        ):
            intent = dict(witness=witness, payloadDigest=digest, owner=asdict(receipt))
            commit_id = self.journal.begin(witness["sessionFile"], intent)
            request = dict(
                action="commit",
                witness=witness,
                summary=summary,
                tokensBefore=tokens_before,
                commit=dict(commitId=commit_id, payloadDigest=digest),
            )
            try:
                evidence = self._call(fd, request, timeout, (executor_fd,))
            except Exception as error:
                # Includes launch/protocol errors: conservative even where no
                # write probably occurred. Cancellation leaves durable intent.
                evidence = dict(status="unknown", reason=str(error)[:1024])
            self.journal.resolve(commit_id, evidence["status"], evidence)
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
        arguments = self._guard_arguments(owner, witness)
        with (
            idle_session_writer_fence(arguments["session_file"]) as executor_fd,
            self.registry.guard_owner_compaction(owner, epoch, **arguments) as (_, fd),
        ):
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
                evidence = self._call(fd, request, timeout, (executor_fd,))
            except Exception as error:
                evidence = dict(status="unknown", reason=str(error)[:1024])
            self.journal.resolve(commit_id, evidence["status"], evidence)
            return self.journal.get(commit_id)
