"""Explicit fresh Pi session enrollment with a durable pre-prompt file identity.

A normal Pi new session has only a *future path* before its first prompt. The
reviewed SessionManager opens an explicitly supplied valid session header with
``--session``, retaining that inode when it appends messages. A file created by
this helper is not by itself selected-summary permission: the owner must also
register coverage under its wire/registry/store locks and settle every raw
input with a returned terminal receipt. An interrupted creation is never
reconstructed into an enrollment from disk.
"""

from __future__ import annotations

import hashlib
import json
import os
from abc import abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, NoReturn
from uuid import uuid4

from .pi_vocabulary import ThinkingLevel
from .private_path import FileIdentity, FileRevision, PrivateFileRole
from .declared_family import DeclaredFamily
from .native_entries import (
    ModelChangeEntry,
    NativeEntry,
    SelectedFreshMarker,
    SessionEntry,
    ThinkingLevelChangeEntry,
)
from .native_pi import (
    NativePiUnavailable,
    _durable_private_session_dir,
    _fsync_directory,
    _read_private_file,
)

if TYPE_CHECKING:
    from .pi_payloads import StateData


_MINT = object()
_MAX_STARTUP_APPEND = 2048


@dataclass(frozen=True, slots=True)
class FreshFileCheck:
    source: FreshPrivateSession
    observed: os.stat_result
    minimum_size: int
    revision: FileRevision | None = None
    exact_size: int | None = None

    @property
    def identity(self) -> FileIdentity:
        return FileIdentity.from_stat(self.observed)

    @property
    def observed_revision(self) -> FileRevision:
        return FileRevision.from_stat(self.observed)

    def require_valid(self, context: str) -> None:
        try:
            PrivateFileRole.require(self.observed)
        except ValueError as error:
            raise NativePiUnavailable(f"{context}: {error}") from error
        for member in FreshFileRule.members_with(FreshFileRule):
            if member.violated(self):
                raise NativePiUnavailable(
                    f"{context}: {member.declared_name} {member.explanation}"
                )


class FreshFileRule(DeclaredFamily, affix="Rule"):
    explanation: ClassVar[str] = ""

    @classmethod
    @abstractmethod
    def violated(cls, check: FreshFileCheck) -> bool: ...


class LinkedFreshFileRule(FreshFileRule):
    @classmethod
    def violated(cls, check):
        return check.observed.st_nlink != 1


class ReboundFreshFileRule(FreshFileRule):
    @classmethod
    def violated(cls, check):
        return check.identity != check.source.file_identity


class TruncatedFreshFileRule(FreshFileRule):
    @classmethod
    def violated(cls, check):
        return check.observed.st_size < check.minimum_size


class ChangedFreshFileRule(FreshFileRule):
    @classmethod
    def violated(cls, check):
        return check.revision is not None and check.observed_revision != check.revision


class PrewrittenFreshFileRule(FreshFileRule):
    explanation = "Fresh-session has earlier input before enrollment"

    @classmethod
    def violated(cls, check):
        return check.exact_size is not None and check.observed.st_size != check.exact_size


@dataclass(frozen=True, slots=True)
class SelectedStartupCheck:
    source: FreshPrivateSession
    model: ModelChangeEntry
    thinking: ThinkingLevelChangeEntry

    def require_valid(self) -> None:
        for rule in SelectedStartupRule.members_with(SelectedStartupRule):
            if rule.violated(self):
                raise NativePiUnavailable(
                    f"Selected startup metadata does not match runtime: {rule.declared_name}"
                )


class SelectedStartupRule(DeclaredFamily, affix="Rule"):
    @classmethod
    @abstractmethod
    def violated(cls, check: SelectedStartupCheck) -> bool: ...


class DifferentSelectedModelRule(SelectedStartupRule):
    @classmethod
    def violated(cls, check):
        return not check.model.matches_startup(
            check.source.selected_model, check.source.selected_thinking_level
        )


class DifferentSelectedThinkingRule(SelectedStartupRule):
    @classmethod
    def violated(cls, check):
        return not check.thinking.matches_startup(
            check.source.selected_model, check.source.selected_thinking_level
        )


class DetachedModelEntryRule(SelectedStartupRule):
    @classmethod
    def violated(cls, check):
        return check.model.parent_id != check.source.bootstrap_leaf_id


class DetachedThinkingEntryRule(SelectedStartupRule):
    @classmethod
    def violated(cls, check):
        return check.thinking.parent_id != check.model.id


class ReusedStartupEntryRule(SelectedStartupRule):
    @classmethod
    def violated(cls, check):
        ids = (check.source.bootstrap_leaf_id, check.model.id, check.thinking.id)
        return len(set(ids)) != len(ids)


class FreshRuntimeRule(DeclaredFamily, affix="FreshRuntimeRule"):
    """Named observed-runtime constraints of the original fresh enrollment."""

    explanation: ClassVar[str]

    @classmethod
    @abstractmethod
    def violated(cls, enrolled: FreshPrivateSession, state: StateData) -> bool:
        raise NotImplementedError


class SessionChangedFreshRuntimeRule(FreshRuntimeRule):
    explanation = "Selected first source session identity differs"

    @classmethod
    def violated(cls, enrolled: FreshPrivateSession, state: StateData) -> bool:
        return state.session_id != enrolled.session_id


class ModelChangedFreshRuntimeRule(FreshRuntimeRule):
    explanation = "Selected first source model differs"

    @classmethod
    def violated(cls, enrolled: FreshPrivateSession, state: StateData) -> bool:
        return not state.matches_model(enrolled.selected_model)


class ThinkingChangedFreshRuntimeRule(FreshRuntimeRule):
    explanation = "Selected first source thinking level differs"

    @classmethod
    def violated(cls, enrolled: FreshPrivateSession, state: StateData) -> bool:
        return ThinkingLevel.optional_name(state.thinking_level) != enrolled.selected_thinking_level


class MessagesPresentFreshRuntimeRule(FreshRuntimeRule):
    explanation = "Selected first source is not empty"

    @classmethod
    def violated(cls, enrolled: FreshPrivateSession, state: StateData) -> bool:
        return state.message_count != 0


class PendingMessagesFreshRuntimeRule(FreshRuntimeRule):
    explanation = "Selected first source pending messages differ"

    @classmethod
    def violated(cls, enrolled: FreshPrivateSession, state: StateData) -> bool:
        return state.pending_message_count != 0


class StreamingFreshRuntimeRule(FreshRuntimeRule):
    explanation = "Selected first source is not attested idle"

    @classmethod
    def violated(cls, enrolled: FreshPrivateSession, state: StateData) -> bool:
        return state.is_streaming is not False


class CompactingFreshRuntimeRule(FreshRuntimeRule):
    explanation = "Selected first source compaction state is not idle"

    @classmethod
    def violated(cls, enrolled: FreshPrivateSession, state: StateData) -> bool:
        return state.is_compacting is not False


@dataclass(frozen=True, init=False, slots=True, weakref_slot=True)
class FreshPrivateSession:
    selected_model: ClassVar[tuple[str, str]] = ("openrouter", "z-ai/glm-5.3-flash")
    path: Path
    session_id: str
    file_identity: FileIdentity
    header_sha256: str
    bootstrap_sha256: str
    bootstrap_size: int
    bootstrap_leaf_id: str | None
    selected_thinking_level: str | None
    creator_pid: int

    def __init__(
        self,
        key: object,
        path: Path,
        session_id: str,
        file_identity: FileIdentity,
        header_sha256: str,
        bootstrap_sha256: str,
        bootstrap_size: int,
        bootstrap_leaf_id: str | None,
        selected_thinking_level: str | None,
    ) -> None:
        if key is not _MINT:
            raise TypeError("Fresh-session enrollment cannot be reconstructed from a file")
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "file_identity", file_identity)
        object.__setattr__(self, "header_sha256", header_sha256)
        object.__setattr__(self, "bootstrap_sha256", bootstrap_sha256)
        object.__setattr__(self, "bootstrap_size", bootstrap_size)
        object.__setattr__(self, "bootstrap_leaf_id", bootstrap_leaf_id)
        object.__setattr__(self, "selected_thinking_level", selected_thinking_level)
        object.__setattr__(self, "creator_pid", os.getpid())

    def __reduce__(self) -> NoReturn:
        raise TypeError("Fresh-session enrollment cannot cross a process boundary")

    def require_runtime(self, state: StateData) -> None:
        for rule in FreshRuntimeRule.members_with(FreshRuntimeRule):
            if rule.violated(self, state):
                raise NativePiUnavailable(f"{rule.declared_name}: {rule.explanation}")

    @staticmethod
    def require_launch_header(path: Path, selected_thinking_level: str | None) -> None:
        """A saved marker can deny reopen; it cannot recreate first-start authority."""
        rows = _read_private_file(path)
        try:
            header = NativeEntry.from_evidence(next(rows))
            if not isinstance(header, SessionEntry):
                raise ValueError("Native source lacks a session header")
            header.require_header()
            for _ in rows:
                pass  # Exhaust revision validation without retaining historical bodies.
            expected = (
                SelectedFreshMarker(1, selected_thinking_level)
                if selected_thinking_level is not None
                else None
            )
            if header.selected_fresh != expected:
                raise ValueError("Selected fresh source lacks its exact first-start token")
        except (StopIteration, ValueError, TypeError, KeyError) as error:
            raise NativePiUnavailable(
                "Selected fresh source cannot reopen without exact first-start token"
            ) from error

    def verify_saved_identity(self, *, prewrite: bool = False) -> None:
        """Verify the original header/inode, optionally requiring no Pi appends yet."""
        if os.getpid() != self.creator_pid:
            raise NativePiUnavailable("Fresh-session creator process changed")
        try:
            info = self.path.lstat()
            FreshFileCheck(self, info, 1, exact_size=self.bootstrap_size if prewrite else None).require_valid(
                "Fresh-session saved inode changed"
            )
            descriptor = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                opened = os.fstat(descriptor)
                FreshFileCheck(self, opened, self.bootstrap_size).require_valid(
                    "Fresh-session opened identity changed"
                )
                bootstrap = bytearray()
                while len(bootstrap) < self.bootstrap_size:
                    chunk = os.read(descriptor, self.bootstrap_size - len(bootstrap))
                    if not chunk:
                        raise NativePiUnavailable("Fresh-session bootstrap became incomplete")
                    bootstrap.extend(chunk)
            finally:
                os.close(descriptor)
            header = bytes(bootstrap).split(b"\n", 1)[0] + b"\n"
            if (
                not header.endswith(b"\n")
                or hashlib.sha256(header).hexdigest() != self.header_sha256
                or hashlib.sha256(bootstrap).hexdigest() != self.bootstrap_sha256
            ):
                raise NativePiUnavailable("Fresh-session bootstrap changed")
            row = NativeEntry.from_evidence(json.loads(header))
            after = self.path.lstat()
            FreshFileCheck(
                self, after, self.bootstrap_size,
                exact_size=self.bootstrap_size if prewrite else None,
            ).require_valid("Fresh-session path changed after bootstrap read")
            if (
                not isinstance(row, SessionEntry)
                or row.id != self.session_id
                or row.selected_fresh
                != (
                    SelectedFreshMarker(1, self.selected_thinking_level)
                    if self.selected_thinking_level is not None
                    else None
                )
            ):
                raise NativePiUnavailable("Fresh-session header identity changed")
        except (OSError, ValueError, TypeError) as error:
            raise NativePiUnavailable(
                "Fresh-session header cannot prove new-file identity"
            ) from error

    def verify_prewrite(self) -> None:
        self.verify_saved_identity(prewrite=True)

    def verify_selected_startup(self) -> FileRevision:
        """Attest exactly Pi's two expected metadata appends, no raw messages.

        Pinned createAgentSession appends its initial model and thinking entries
        on a zero-message session, even when explicit launch flags are supplied.
        This is a file observation, not provider/child/terminal authority.
        """
        if not ThinkingLevel.supports_selected(self.selected_thinking_level) or not self.bootstrap_leaf_id:
            raise NativePiUnavailable("Fresh source lacks explicit selected bootstrap")
        self.verify_saved_identity()
        try:
            info = self.path.lstat()
            tail_size = info.st_size - self.bootstrap_size
            if not 0 < tail_size <= _MAX_STARTUP_APPEND:
                raise NativePiUnavailable("Selected startup has no exact metadata tail")
            descriptor = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                opened = os.fstat(descriptor)
                observed = FreshFileCheck(self, info, self.bootstrap_size)
                FreshFileCheck(self, opened, self.bootstrap_size, observed.observed_revision).require_valid(
                    "Selected startup inode/revision changed"
                )
                chunks = bytearray()
                while len(chunks) < info.st_size:
                    chunk = os.read(descriptor, info.st_size - len(chunks))
                    if not chunk:
                        raise NativePiUnavailable("Selected startup file became incomplete")
                    chunks.extend(chunk)
                # Pi's startup appendFileSync alone is not a durable receipt.
                # Fail closed on uncertain fsync before the raw prompt writer.
                os.fsync(descriptor)
                synced = os.fstat(descriptor)
                FreshFileCheck(
                    self, synced, self.bootstrap_size, FileRevision.from_stat(opened),
                ).require_valid("Selected startup changed across fsync")
            finally:
                os.close(descriptor)
            data = bytes(chunks)
            if hashlib.sha256(
                data[: self.bootstrap_size]
            ).hexdigest() != self.bootstrap_sha256 or not data.endswith(b"\n"):
                raise NativePiUnavailable("Selected startup changed its enrolled prefix")
            lines = data[self.bootstrap_size :].splitlines(keepends=True)
            if len(lines) != 2 or any(not line.endswith(b"\n") for line in lines):
                raise NativePiUnavailable("Selected startup has extra or partial entries")
            model = ModelChangeEntry.read_startup(lines[0])
            thinking = ThinkingLevelChangeEntry.read_startup(lines[1])
            SelectedStartupCheck(self, model, thinking).require_valid()
            after = self.path.lstat()
            revision = FileRevision.from_stat(info)
            FreshFileCheck(self, after, self.bootstrap_size, revision).require_valid(
                "Selected startup revision changed after read"
            )
            return revision
        except (OSError, ValueError, TypeError, KeyError, NativePiUnavailable) as error:
            raise NativePiUnavailable(f"Selected startup metadata cannot be attested: {error}") from error


def create_fresh_private_session(
    session_dir: Path, *, worktree: Path, selected_thinking_level: str | None = None
) -> FreshPrivateSession:
    """Create a unique v3 header with O_EXCL, file fsync, then parent fsync.

    Call only for explicit fresh-session enrollment. An existing path, reused
    ID, failed fsync or uncertain return is not eligible, even if a header is
    later visible. This API never opens an old saved session for enrollment.
    """
    # Explicitly selected static model only; the level may not come from an
    # arbitrary request or be silently clamped from an unsupported 'off'.
    if selected_thinking_level is not None and (
        type(selected_thinking_level) is not str or not ThinkingLevel.supports_selected(selected_thinking_level)
    ):
        raise ValueError("Explicit supported selected thinking level required")
    session_dir = Path(session_dir).absolute()
    worktree = Path(worktree).absolute()
    if not worktree.is_dir():
        raise NativePiUnavailable("Fresh-session worktree is unavailable")
    _durable_private_session_dir(session_dir)
    session_id = uuid4().hex
    path = session_dir / f"enrolled-{session_id}.jsonl"
    header_row = {
        "type": "session",
        "version": 3,
        "id": session_id,
        "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "cwd": str(worktree),
    }
    if selected_thinking_level is not None:
        # This is a denial marker, not authority to enroll or to dispatch.
        # Omission on a later path-only reopen must not bypass the selected
        # first-startup model/thinking gate.
        header_row["agentCommsSelectedFresh"] = SelectedFreshMarker(
            1, selected_thinking_level
        ).to_wire()
    header = json.dumps(header_row, separators=(",", ":"), allow_nan=False).encode() + b"\n"
    bootstrap_leaf_id = None
    bootstrap = header
    if selected_thinking_level is not None:
        model_entry_id = uuid4().hex[:8]
        bootstrap_leaf_id = uuid4().hex[:8]
        while bootstrap_leaf_id == model_entry_id:
            bootstrap_leaf_id = uuid4().hex[:8]
        timestamp = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        for row in (
            {
                "type": "model_change",
                "id": model_entry_id,
                "parentId": None,
                "timestamp": timestamp,
                "provider": FreshPrivateSession.selected_model[0],
                "modelId": FreshPrivateSession.selected_model[1],
            },
            {
                "type": "thinking_level_change",
                "id": bootstrap_leaf_id,
                "parentId": model_entry_id,
                "timestamp": timestamp,
                "thinkingLevel": selected_thinking_level,
            },
        ):
            bootstrap += json.dumps(row, separators=(",", ":"), allow_nan=False).encode() + b"\n"
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            info = os.fstat(descriptor)
            PrivateFileRole.require(info)
            if info.st_nlink != 1:
                raise NativePiUnavailable("Fresh-session file identity is not exclusive")
            pending = memoryview(bootstrap)
            while pending:
                written = os.write(descriptor, pending)
                if written <= 0:
                    raise OSError("Fresh-session bootstrap write made no progress")
                pending = pending[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory(session_dir)
        result = FreshPrivateSession(
            _MINT,
            path,
            session_id,
            FileIdentity.from_stat(info),
            hashlib.sha256(header).hexdigest(),
            hashlib.sha256(bootstrap).hexdigest(),
            len(bootstrap),
            bootstrap_leaf_id,
            selected_thinking_level,
        )
        result.verify_prewrite()
        return result
    except OSError as error:
        # Do not delete a possibly committed header after a failed fsync. It
        # remains unenrolled, never an inferred coverage grant.
        raise NativePiUnavailable("Fresh-session creation durability UNKNOWN") from error
