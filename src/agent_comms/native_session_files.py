"""Native transcript resources and bus-owned input proofs."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, fields
from pathlib import Path

from .errors import RelationViolationError
from agent_comms.locked_store import DurableFile, PatternFile, SQLiteFile
from agent_comms.field_codec import Id128Text
from agent_comms.participant_identity import ParticipantIdentity
from agent_comms.native_session_reopen import NativeSessionIdentity


class SessionFiles:
    """Declared session members share reference checks and deletion lifecycle."""

    __slots__ = ()
    session: Path
    members: tuple[Path, ...]

    @property
    def schema_paths(self) -> tuple[Path, ...]:
        return (self.session,)

    def present(self) -> tuple[Path, ...]:
        return tuple(path for path in self.members if path.exists() or path.is_symlink())

    def require_only_declared_sidecars(self) -> None:
        directory = self.session.parent
        if not directory.is_dir():
            return
        declared = {path.name for path in self.members}
        stray = sorted(
            entry.name
            for entry in os.scandir(directory)
            if (entry.name.startswith(self.session.name)
                or entry.name.startswith(f".{self.session.name}"))
            and entry.name not in declared
        )
        if stray:
            raise RelationViolationError(
                f"Saved session {self.session} has undeclared sidecars: {', '.join(stray)}"
            )

    def referencing_processes(self) -> tuple[int, ...]:
        """Processes holding a member open, or naming the session in argv or environment.

        Another user's process cannot be inspected; it also cannot open these
        owner-private files, so it is not counted.
        """
        return tuple(
            int(entry.name)
            for entry in os.scandir("/proc")
            if entry.name.isdigit() and self._referenced_by(entry.path)
        )

    def _referenced_by(self, process: str) -> bool:
        members = {str(path) for path in self.members}
        needle = str(self.session).encode()
        try:
            for name in ("cmdline", "environ"):
                with open(f"{process}/{name}", "rb") as stream:
                    if needle in stream.read():
                        return True
            for descriptor in os.scandir(f"{process}/fd"):
                try:
                    target = os.readlink(descriptor.path)
                except FileNotFoundError:
                    continue
                if target.removesuffix(" (deleted)") in members:
                    return True
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            return False
        return False

    def require_unreferenced(self) -> None:
        self.require_only_declared_sidecars()
        if referencing := self.referencing_processes():
            raise RelationViolationError(
                f"Saved session {self.session} is referenced by processes {list(referencing)}"
            )

    def delete(self) -> int:
        """Unlink every member after proving no process references it; bytes freed."""
        self.require_unreferenced()
        freed = 0
        for path in self.present():
            freed += path.lstat().st_size
            path.unlink()
        if self.session.parent.is_dir():
            directory = os.open(self.session.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        return freed


class NativeTranscriptFiles(SessionFiles):
    """Share native transcript/writer-lock lifetime without owning proof storage."""

    __slots__ = ()

    @property
    def schema_paths(self) -> tuple[Path, ...]:
        return ()

    @property
    def writer_lock(self) -> Path:
        return self.session.with_name(f".{self.session.name}.agent-comms-writer.lock")

    @property
    def members(self) -> tuple[Path, ...]:
        return self.session, self.writer_lock


@dataclass(frozen=True, slots=True)
class NativeSessionLocks(NativeTranscriptFiles):
    """A transcript whose caller needs only its native writer lock."""

    session: Path

    @classmethod
    def of(cls, session_file: str) -> NativeSessionLocks:
        return cls(Path(session_file).resolve())


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeSessionFiles(NativeSessionIdentity, NativeTranscriptFiles):
    """Bind the existing saved identity to its bus and participant namespace."""

    root: Path
    owner: ParticipantIdentity

    @classmethod
    def for_identity(
        cls, identity: NativeSessionIdentity, *, root: Path, owner: ParticipantIdentity,
    ) -> NativeSessionFiles:
        return cls(*(getattr(identity, declaration.name)
                     for declaration in fields(NativeSessionIdentity)), root=root, owner=owner)

    @property
    def session(self) -> Path:
        return self.path

    @property
    def input_proof(self) -> Path:
        # Native IDs are opaque header data, not trusted path components. Hashing
        # keeps the key stable across transcript moves and confined to one file.
        key = hashlib.sha256(self.session_id.encode()).hexdigest()
        return NativeInputProof.path_in(
            self.root, lookup=Id128Text.decode(self.owner.lookup), name=key,
        )


class NativeInputProof(DurableFile, SQLiteFile, PatternFile):
    """One thread's native-session proof, retained and copied with its bus."""

    __slots__ = ()
    filename = "input-proofs/{lookup}/{name}.sqlite"


class SavedSessionFile(PatternFile):
    """A bus location holding saved native transcripts and their writer locks."""

    __slots__ = ()

    @classmethod
    def sessions_in(cls, root: Path) -> tuple[NativeSessionLocks, ...]:
        """Every saved session under ``root``, found relative to it, so a practice copy's
        sessions are its own and never the live bus the copied registry still names."""
        return tuple(
            NativeSessionLocks(path)
            for owner, directory in cls.declarations_in(root)
            for path in sorted(owner.matching_in(directory))
        )


class PrivateSession(DurableFile, SavedSessionFile):
    """A thread's own session, created fresh for it."""

    __slots__ = ()
    filename = "sessions/enrolled-{session_id}.jsonl"


class CheckSession(DurableFile, SavedSessionFile):
    """A check root's copy of one selected worker's history, under its record's slot."""

    __slots__ = ()
    filename = "native-sessions/{slot}/enrolled-{session_id}.jsonl"
