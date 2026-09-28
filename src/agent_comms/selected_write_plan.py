"""Default-OFF ACP owner preplan for one private selected existing-file write.

An ACP operator supplies bytes *before* dispatch. The owner process persists
one intent; the selected runner applies it only after a verified native result.
An accepted or uncertain intent is never automatically retried. This is not
an interceptor for Pi/shell/edit writes or a filesystem security boundary.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import stat
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from .bus_publication import stable_thread_lookup
from .coordination import WakeClaim
from .coordination_cohort import sealed_cohort_claims
from .coordination_store import IdentityConflict, MutationStore
from .declarations import Thread, _store_lock
from .envelope_claim_transitions import normalize_existing_file
from .operations import Comms

_MAX_BYTES = 1024 * 1024


@dataclass(frozen=True)
class PlannedWrite:
    resource: Path
    contents: bytes
    operation_id: str


class SelectedWritePlans:
    def __init__(self, comms: Comms, root_id: str):
        self.comms = comms
        self.root_id = root_id
        self.directory = comms.root / "selected-write-plans"

    def _path(self, source_seq: int, lookup: str) -> Path:
        if type(source_seq) is not int or source_seq <= 0:
            raise IdentityConflict("Selected write requires a positive source sequence")
        return self.directory / f"{source_seq}-{lookup}.json"

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _prepare_dir(self) -> None:
        with suppress(FileExistsError):
            self.directory.mkdir(mode=0o700)
        self._validate_dir()
        # Also on an existing directory: a previous mkdir may have survived
        # while its parent fsync failed. Do not acknowledge a later intent
        # until the directory entry itself is known durable.
        self._fsync_dir(self.comms.root)

    def _validate_dir(self) -> bool:
        try:
            info = self.directory.lstat()
        except FileNotFoundError:
            return False
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise IdentityConflict("Selected write plan directory is not private")
        return True

    def _read(self, path: Path) -> dict | None:
        if not self._validate_dir():
            return None
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > 2 * _MAX_BYTES
            ):
                raise IdentityConflict("Selected write intent has unsafe identity")
            with os.fdopen(fd, "rb") as stream:
                fd = -1
                record = json.loads(stream.read())
                if type(record) is not dict:
                    raise IdentityConflict("Selected write intent is not an object")
                return record
        finally:
            if fd >= 0:
                os.close(fd)

    def submit(
        self,
        *,
        owner_name: str,
        source_seq: int,
        source_message_id: str,
        resource: str,
        contents: str,
    ) -> dict[str, object]:
        """Persist exactly one predispatch operator intent; no native/model call."""
        if (
            type(source_message_id) is not str
            or not source_message_id
            or type(resource) is not str
            or not resource
            or type(contents) is not str
        ):
            raise IdentityConflict("Selected write request is not typed")
        raw = contents.encode("utf-8")
        if not raw or len(raw) > _MAX_BYTES:
            raise IdentityConflict("Selected write requires 1..1048576 UTF-8 bytes")
        with _store_lock(self.comms._wire_lock_path):
            with _store_lock(self.comms.bus._path):
                marker = self.comms.bus._private_marker_unlocked()
            if marker["wire_root_id"] != self.root_id or marker.get("claim_envelopes_version") != 1:
                raise IdentityConflict("Selected write requires matching private claim root")
            owner, epoch = self.comms.registry.live_owner_with_admission(owner_name)
            if owner.pid != os.getpid() or owner.active_turn is not None:
                raise IdentityConflict("Selected write owner is not idle in this process")
            initial = self.comms.bus.read_initial_cohort(self.root_id, source_seq)
            if initial.message.message_id != source_message_id:
                raise IdentityConflict("Selected write source identity changed")
            lookup = stable_thread_lookup(owner.created_at)
            with MutationStore(str(self.comms.root / "coordination.sqlite3")) as store:
                claims = sealed_cohort_claims(store, lookup, after_seq=source_seq - 1)
                selected = [
                    claim
                    for claim in claims
                    if claim.wire_seq == source_seq
                    and claim.message_id == source_message_id
                    and claim.recipient == owner.name
                    and claim.lifecycle.full_pending
                ]
                if len(selected) != 1:
                    raise IdentityConflict("Selected write has no one pending FULL selected claim")
            path = normalize_existing_file(Path(owner.worktree), resource)
            self._prepare_dir()
            record_path = self._path(source_seq, lookup)
            operation_id = secrets.token_hex(16)
            record = {
                "schema": 1,
                "status": "accepted",
                "root_id": self.root_id,
                "source_seq": source_seq,
                "source_message_id": source_message_id,
                "claim_id": selected[0].claim_id,
                "owner": owner.name,
                "incarnation": owner.created_at,
                "admission_epoch": epoch,
                "expected_attempt_ordinal": 1,
                "operation_id": operation_id,
                "resource": str(path),
                "contents_b64": base64.b64encode(raw).decode("ascii"),
                "digest": hashlib.sha256(raw).hexdigest(),
            }
            data = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
            try:
                fd = os.open(
                    record_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
                )
            except FileExistsError as error:
                raise IdentityConflict(
                    "Selected write intent already accepted or UNKNOWN; never retry"
                ) from error
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                self._fsync_dir(self.directory)
            except BaseException:
                # The visible file may have committed despite the error. Keep
                # it as an UNKNOWN barrier; never unlink/retry the operation.
                raise
            return {
                "status": "accepted_not_applied",
                "operationId": operation_id,
                "sourceSeq": source_seq,
                "claimId": selected[0].claim_id,
            }

    def load(self, claim: WakeClaim, owner: Thread, epoch: int) -> PlannedWrite | None:
        lookup = stable_thread_lookup(owner.created_at)
        path = self._path(claim.wire_seq, lookup)
        row = self._read(path)
        if row is None:
            return None
        if (
            row.get("schema") != 1
            or row.get("status") != "accepted"
            or row.get("root_id") != self.root_id
            or row.get("source_seq") != claim.wire_seq
            or row.get("source_message_id") != claim.message_id
            or row.get("claim_id") != claim.claim_id
            or row.get("owner") != owner.name
            or row.get("incarnation") != owner.created_at
            or row.get("admission_epoch") != epoch
            or row.get("expected_attempt_ordinal") != 1
        ):
            raise IdentityConflict("Selected write intent is stale or uncertain")
        raw = base64.b64decode(row["contents_b64"], validate=True)
        if not raw or len(raw) > _MAX_BYTES or hashlib.sha256(raw).hexdigest() != row.get("digest"):
            raise IdentityConflict("Selected write intent bytes are corrupt")
        operation_id = row.get("operation_id")
        if type(operation_id) is not str or len(operation_id) != 32:
            raise IdentityConflict("Selected write intent operation is invalid")
        resource = normalize_existing_file(Path(owner.worktree), row["resource"])
        return PlannedWrite(Path(resource), raw, operation_id)

    def applied(self, claim: WakeClaim, owner: Thread, operation_id: str) -> None:
        path = self._path(claim.wire_seq, stable_thread_lookup(owner.created_at))
        row = self._read(path)
        if (
            row is None
            or row.get("status") != "accepted"
            or row.get("operation_id") != operation_id
        ):
            raise IdentityConflict("Selected write intent is no longer unique")
        row["status"] = "applied"
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(json.dumps(row, sort_keys=True, separators=(",", ":")).encode())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            self._fsync_dir(self.directory)
        except BaseException:
            # A post-write failure is UNKNOWN. The bus claim and attempt
            # remain visible; no path automatically reissues the write.
            raise
