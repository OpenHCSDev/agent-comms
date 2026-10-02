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
from contextlib import suppress
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .acp_extension import SelectedWriteAcceptedUpdate

from agent_comms.coordination_errors import IdentityConflict
from agent_comms.coordination_tables.assignments import WakeAssignment
from agent_comms.coordinator import Coordination

from .bus_publication import stable_thread_lookup
from .comms import Comms
from .coordination_cohort import sealed_cohort_assignments
from .envelope_claim_transitions import ExistingFileClaim
from .store_files import _store_lock
from .threads import Thread
from .field_codec import FieldCodec
from .declared_family import DeclaredFamily
from .private_path import PrivateDirectoryRole, PrivateFileRole
from .pi_rpc import unique_fields
from .message_reference import MessageReference
from .thread_identity import AdmissionIdentity, ThreadIncarnation

_MAX_BYTES = 1024 * 1024


@dataclass(frozen=True)
class PlannedWrite:
    resource: ExistingFileClaim
    contents: bytes
    operation_id: str


@dataclass(frozen=True, slots=True)
class SelectedWriteBinding:
    root_id: str
    source: MessageReference
    assignment_id: str
    admission: AdmissionIdentity


@dataclass(frozen=True, kw_only=True)
class SelectedWriteIntent(DeclaredFamily, affix="WriteIntent"):
    """The existing one-use plan file owns its state and exact source binding."""

    family_discriminator = "status"
    schema: Literal[1]
    root_id: str
    source_seq: int
    source_message_id: str
    claim_id: str
    owner: str
    incarnation: float
    admission_epoch: int
    expected_attempt_ordinal: Literal[1]
    operation_id: str
    resource: str
    contents_b64: str
    digest: str

    @property
    def binding(self) -> SelectedWriteBinding:
        return SelectedWriteBinding(
            self.root_id, MessageReference(self.source_seq, self.source_message_id),
            self.claim_id, AdmissionIdentity(ThreadIncarnation(self.owner, self.incarnation), self.admission_epoch),
        )

    def require_accepted(self):
        raise IdentityConflict("Selected write intent is already applied or uncertain")

    def __post_init__(self):
        if len(self.operation_id) != 32:
            raise IdentityConflict("Selected write intent operation is invalid")


class AcceptedWriteIntent(SelectedWriteIntent, declared_name="accepted"):
    def require_accepted(self):
        return self

    def planned(self, worktree: str) -> PlannedWrite:
        raw = base64.b64decode(self.contents_b64, validate=True)
        if not raw or len(raw) > _MAX_BYTES or hashlib.sha256(raw).hexdigest() != self.digest:
            raise IdentityConflict("Selected write intent bytes are corrupt")
        resource = ExistingFileClaim(Path(self.resource))
        resource.normalized(Path(worktree))
        return PlannedWrite(resource, raw, self.operation_id)

    def applied(self):
        return AppliedWriteIntent(**{item.name: getattr(self, item.name) for item in fields(self)})


class AppliedWriteIntent(SelectedWriteIntent, declared_name="applied"):
    pass


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
        if PrivateDirectoryRole.violation(info) is not None:
            raise IdentityConflict("Selected write plan directory is not private")
        return True

    def _read(self, path: Path) -> SelectedWriteIntent | None:
        if not self._validate_dir():
            return None
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(fd)
            if PrivateFileRole.violation(info) is not None:
                raise IdentityConflict("Selected write intent is not private")
            if info.st_nlink != 1 or info.st_size > 2 * _MAX_BYTES:
                raise IdentityConflict("Selected write intent has unsafe identity")
            with os.fdopen(fd, "rb") as stream:
                fd = -1
                try:
                    return FieldCodec.decode(SelectedWriteIntent, json.loads(stream.read(), object_pairs_hook=unique_fields))
                except (ValueError, TypeError) as error:
                    raise IdentityConflict("Selected write intent is malformed") from error
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
    ) -> SelectedWriteAcceptedUpdate:
        """Persist exactly one predispatch operator intent; no native/model call."""
        try:
            FieldCodec.decode(str, source_message_id)
            FieldCodec.decode(str, resource)
            FieldCodec.decode(str, contents)
        except (TypeError, ValueError) as error:
            raise IdentityConflict("Selected write request is not typed") from error
        if not source_message_id or not resource:
            raise IdentityConflict("Selected write request requires its source and resource")
        raw = contents.encode("utf-8")
        if not raw or len(raw) > _MAX_BYTES:
            raise IdentityConflict("Selected write requires 1..1048576 UTF-8 bytes")
        with _store_lock(self.comms._wire_lock_path):
            with self.comms.bus.log.locked():
                marker = self.comms.bus.log._private_marker_unlocked()
            if marker.root_id != self.root_id or not marker.claims:
                raise IdentityConflict("Selected write requires matching private claim root")
            owner, admission_generation = self.comms.registry.live_owner_with_admission(owner_name)
            if owner.pid != os.getpid() or owner.active_turn is not None:
                raise IdentityConflict("Selected write owner is not idle in this process")
            initial = self.comms.bus.log.read_delivery_cohort(self.root_id, source_seq)
            if initial.message.message_id != source_message_id:
                raise IdentityConflict("Selected write source identity changed")
            lookup = stable_thread_lookup(owner.created_at)
            with Coordination(str(self.comms.root / "coordination.sqlite3")) as store:
                assignments = sealed_cohort_assignments(store, lookup, after_seq=source_seq - 1)
                selected = [
                    assignment
                    for assignment in assignments
                    if assignment.source == MessageReference(source_seq, source_message_id)
                    and assignment.recipient == owner.name
                    and assignment.lifecycle.full_pending
                ]
                if len(selected) != 1:
                    raise IdentityConflict("Selected write has no one pending FULL selected claim")
            path = ExistingFileClaim(Path(resource)).normalized(Path(owner.worktree))
            self._prepare_dir()
            record_path = self._path(source_seq, lookup)
            operation_id = secrets.token_hex(16)
            record = AcceptedWriteIntent(
                schema=1, root_id=self.root_id, source_seq=source_seq,
                source_message_id=source_message_id, claim_id=selected[0].assignment_id,
                owner=owner.name, incarnation=owner.created_at, admission_epoch=admission_generation,
                expected_attempt_ordinal=1, operation_id=operation_id, resource=str(path),
                contents_b64=base64.b64encode(raw).decode("ascii"), digest=hashlib.sha256(raw).hexdigest(),
            )
            data = json.dumps(FieldCodec.encode(record), sort_keys=True, separators=(",", ":")).encode()
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
            from .acp_extension import SelectedWriteAcceptedUpdate

            return SelectedWriteAcceptedUpdate(operation_id, source_seq, selected[0].assignment_id)

    def load(
        self, assignment: WakeAssignment, owner: Thread, admission_generation: int
    ) -> PlannedWrite | None:
        lookup = stable_thread_lookup(owner.created_at)
        path = self._path(assignment.wire_seq, lookup)
        row = self._read(path)
        if row is None:
            return None
        row = row.require_accepted()
        expected = SelectedWriteBinding(
            self.root_id, assignment.source, assignment.assignment_id,
            AdmissionIdentity(owner.incarnation, admission_generation),
        )
        if row.binding != expected:
            raise IdentityConflict("Selected write intent is stale or uncertain")
        return row.planned(owner.worktree)

    def applied(self, assignment: WakeAssignment, owner: Thread, operation_id: str) -> None:
        path = self._path(assignment.wire_seq, stable_thread_lookup(owner.created_at))
        row = self._read(path)
        if row is None:
            raise IdentityConflict("Selected write intent is missing")
        accepted = row.require_accepted()
        if accepted.operation_id != operation_id:
            raise IdentityConflict("Selected write intent is no longer unique")
        completed = accepted.applied()
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(json.dumps(FieldCodec.encode(completed), sort_keys=True, separators=(",", ":")).encode())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            self._fsync_dir(self.directory)
        except BaseException:
            # A post-write failure is UNKNOWN. The bus claim and attempt
            # remain visible; no path automatically reissues the write.
            raise
