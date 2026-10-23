"""Default-OFF ACP owner preplan for one private selected existing-file write.

An ACP operator supplies bytes *before* dispatch. The owner process persists
one intent; the selected runner applies it only after a verified native result.
An accepted or uncertain intent is never automatically retried. This is not
an interceptor for Pi/shell/edit writes or a filesystem security boundary.
"""

from __future__ import annotations

import base64
import hashlib
import os
from contextlib import suppress
from dataclasses import dataclass, fields
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .coordination_database import CoordinationStore
from .locked_store import DurableFile, PatternFile
from .store_files import AtomicFile

if TYPE_CHECKING:
    from .acp_extension import SelectedWriteAcceptedUpdate
    from .comms import Comms
    from .coordination_tables.assignments import WakeAssignment
    from .threads import Thread

from agent_comms.coordination_errors import IdentityConflict
from agent_comms.coordinator import Coordination

from .child_process import Platform
from .coordination_cohort import sealed_cohort_assignments
from .declared_family import DeclaredFamily
from .envelope_claim_transitions import ExistingFileClaim
from .field_codec import FieldCodec, Id128Text, MalformedRecord
from .message_reference import MessageReference
from .private_path import PrivateDirectoryRole, PrivateFileRole
from .thread_identity import ClaimedIdentity, ClaimScoped, RecordedThreadIncarnation

_MAX_BYTES = 1024 * 1024


@dataclass(frozen=True)
class PlannedWrite:
    resource: ExistingFileClaim
    contents: bytes
    operation_id: str


@dataclass(frozen=True, slots=True)
class SelectedWriteBinding(ClaimScoped):
    root_id: str
    source: MessageReference
    assignment_id: str


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
    claim_epoch: int
    expected_attempt_ordinal: Literal[1]
    operation_id: str
    resource: str
    contents_b64: str
    digest: str

    @property
    def binding(self) -> SelectedWriteBinding:
        return SelectedWriteBinding(
            root_id=self.root_id, source=MessageReference(self.source_seq, self.source_message_id),
            assignment_id=self.claim_id,
            claimed_identity=ClaimedIdentity(
                RecordedThreadIncarnation(self.owner, self.incarnation), self.claim_epoch,
            ),
        )

    def require_accepted(self):
        raise IdentityConflict("Selected write intent is already applied or uncertain")

    def __post_init__(self):
        try:
            Id128Text.decode(self.operation_id)
        except ValueError as error:
            raise IdentityConflict("Selected write intent operation is invalid") from error


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


class SelectedWritePlans(DurableFile, PatternFile):
    filename = "{source_seq}-{lookup}.json"
    root_directory = "selected-write-plans"

    def __init__(self, comms: Comms, root_id: str):
        self.comms = comms
        self.root_id = root_id
        self.directory = self.directory_in(comms.root)

    def _prepare_dir(self) -> None:
        with suppress(FileExistsError):
            self.directory.mkdir(mode=0o700)
        self._validate_dir()
        # Also on an existing directory: a previous mkdir may have survived
        # while its parent fsync failed. Do not acknowledge a later intent
        # until the directory entry itself is known durable.
        Platform.current().sync_directory(self.comms.root)

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
                    return FieldCodec.decode(SelectedWriteIntent, FieldCodec.parse(stream.read()))
                except MalformedRecord as error:
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
        if type(source_seq) is not int or source_seq <= 0:
            raise IdentityConflict("Selected write requires a positive source sequence")
        try:
            FieldCodec.decode(str, source_message_id)
            FieldCodec.decode(str, resource)
            FieldCodec.decode(str, contents)
        except MalformedRecord as error:
            raise IdentityConflict("Selected write request is not typed") from error
        if not source_message_id or not resource:
            raise IdentityConflict("Selected write request requires its source and resource")
        raw = contents.encode("utf-8")
        if not raw or len(raw) > _MAX_BYTES:
            raise IdentityConflict("Selected write requires 1..1048576 UTF-8 bytes")
        with self.comms.registry.store.locked():
            self.comms.registry.store.private_guard_unlocked().require_root(
                self.root_id, IdentityConflict("Selected write requires its original native namespace"),
            )
            owner, claimed_generation = self.comms.registry.live_owner_with_claim(owner_name)
            if not owner.process_identity.runs_here() or owner.executing:
                raise IdentityConflict("Selected write owner is not idle in this process")
            initial = self.comms.bus.bus.delivery(source_seq)
            if initial.message.message_id != source_message_id:
                raise IdentityConflict("Selected write source identity changed")
            lookup = owner.incarnation.lookup
            with Coordination(str(CoordinationStore.path_in(self.comms.root))) as store:
                assignments = sealed_cohort_assignments(store, lookup, after_seq=source_seq - 1)
                selected = [
                    pending
                    for assignment in assignments
                    for pending in assignment.lifecycle.writable_pending_assignments(assignment)
                    if pending.source == MessageReference(source_seq, source_message_id)
                    and pending.recipient_lookup == lookup
                ]
                if len(selected) != 1:
                    raise IdentityConflict("Selected write has no one pending FULL selected claim")
            path = ExistingFileClaim(Path(resource)).normalized(Path(owner.worktree))
            self._prepare_dir()
            record_path = self.path_in(self.directory, source_seq=str(source_seq), lookup=lookup)
            operation_id = Id128Text.mint()
            record = AcceptedWriteIntent(
                schema=1, root_id=self.root_id, source_seq=source_seq,
                source_message_id=source_message_id, claim_id=selected[0].assignment_id,
                owner=owner.name, incarnation=owner.created_at.to_wire(),
                claim_epoch=claimed_generation,
                expected_attempt_ordinal=1, operation_id=operation_id, resource=str(path),
                contents_b64=base64.b64encode(raw).decode("ascii"), digest=hashlib.sha256(raw).hexdigest(),
            )
            data = FieldCodec.compact(FieldCodec.encode(record), sort_keys=True).encode()
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
                Platform.current().sync_directory(self.directory)
            except BaseException:
                # The visible file may have committed despite the error. Keep
                # it as an UNKNOWN barrier; never unlink/retry the operation.
                raise
            from .acp_extension import SelectedWriteAcceptedUpdate

            return SelectedWriteAcceptedUpdate(operation_id, source_seq, selected[0].assignment_id)

    def load(
        self, assignment: WakeAssignment, owner: Thread, claimed_generation: int
    ) -> PlannedWrite | None:
        lookup = owner.incarnation.lookup
        path = self.path_in(self.directory, source_seq=str(assignment.wire_seq), lookup=lookup)
        row = self._read(path)
        if row is None:
            return None
        row = row.require_accepted()
        expected = SelectedWriteBinding(
            root_id=self.root_id, source=assignment.source, assignment_id=assignment.assignment_id,
            claimed_identity=ClaimedIdentity(owner.incarnation, claimed_generation),
        )
        if row.binding != expected:
            raise IdentityConflict("Selected write intent is stale or uncertain")
        return row.planned(owner.worktree)

    def applied(self, assignment: WakeAssignment, owner: Thread, operation_id: str) -> None:
        path = self.path_in(self.directory, source_seq=str(assignment.wire_seq),
                            lookup=owner.incarnation.lookup)
        row = self._read(path)
        if row is None:
            raise IdentityConflict("Selected write intent is missing")
        accepted = row.require_accepted()
        if accepted.operation_id != operation_id:
            raise IdentityConflict("Selected write intent is no longer unique")
        completed = accepted.applied()
        AtomicFile(path).write_bytes(FieldCodec.compact(FieldCodec.encode(completed), sort_keys=True).encode())
