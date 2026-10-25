"""Native transcript resources and bus-owned input proofs."""

from __future__ import annotations

import asyncio
from abc import abstractmethod
import hashlib
import os
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, fields
from functools import partial
from pathlib import Path
from typing import Self

from agent_comms.child_process import Platform, ProcessIdentity
from agent_comms.locked_store import DurableFile, PatternFile, SQLiteFile
from agent_comms.native_session_reopen import NativeSessionIdentity
from agent_comms.participant_identity import ParticipantIdentity
from agent_comms.private_path import PrivateDirectoryRole, TrustedAncestorRole

from .errors import RelationViolationError
from .native_failures import NativePiUnavailable
from .store_files import ProcessSingleton
from agent_comms.typed_table import ReconciledFile, TableSchema


class SessionFiles:
    """Declared session members share reference checks and deletion lifecycle."""

    __slots__ = ()
    session: Path
    members: tuple[Path, ...]

    @property
    @abstractmethod
    def writer_lock(self) -> Path: ...

    @property
    def writer_claim(self) -> ProcessSingleton:
        """The one writer of this session, across processes."""
        return ProcessSingleton(self.writer_lock)

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
            if entry.name.isdigit() and self._referenced_by(int(entry.name))
        )

    def _referenced_by(self, pid: int) -> bool:
        members = {str(path) for path in self.members}
        needle = str(self.session)
        try:
            process = ProcessIdentity.capture(pid)
            if any(needle in argument for argument in process.arguments()):
                return True
            if any(needle in f"{name}={value}" for name, value in process.environment().items()):
                return True
            for descriptor in os.scandir(f"/proc/{pid}/fd"):
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
            Platform.current().sync_directory(self.session.parent)
        return freed


class NativeTranscriptFiles(SessionFiles):
    """Share native transcript/writer-lock lifetime without owning proof storage."""

    __slots__ = ()

    @asynccontextmanager
    async def accept_input(self) -> AsyncIterator[None]:
        """Keep later inputs queued until this session's current input settles.

        Acquire before reserving a NativeRuntimeInput. Its original queued wake
        assignments remain unchanged while the kernel waits, across processes.
        """
        descriptor = self.writer_claim.descriptor()
        # Lock waiters must not exhaust the workers the current input needs
        # to finish. This worker retires as soon as its kernel wait completes.
        waiter = ThreadPoolExecutor(max_workers=1, thread_name_prefix="session-input")
        acquisition = asyncio.get_running_loop().run_in_executor(
            waiter, self.writer_claim.wait, descriptor)
        waiter.shutdown(wait=False)
        try:
            await asyncio.shield(acquisition)
            yield
        finally:
            # Cancellation cannot close a descriptor still used by its waiter.
            if acquisition.done():
                self._close_input_writer(descriptor, acquisition)
            else:
                acquisition.add_done_callback(partial(self._close_input_writer, descriptor))

    @staticmethod
    def _close_input_writer(descriptor: int, acquisition: asyncio.Future[None]) -> None:
        try:
            acquisition.exception()
        finally:
            os.close(descriptor)

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
    ) -> Self:
        return cls(*(getattr(identity, declaration.name)
                     for declaration in fields(NativeSessionIdentity)), root=root, owner=owner)

    @staticmethod
    def require_private_directory(directory: Path) -> None:
        if ".." in directory.parts:
            raise NativePiUnavailable("Native Pi session directory is not lexical")
        TrustedAncestorRole.require_path(
            directory, NativePiUnavailable("Native Pi session directory has unsafe ancestor"))
        PrivateDirectoryRole.require(
            directory.lstat(), NativePiUnavailable("Native Pi session directory must be owner-only"))

    @classmethod
    def prepare_private_directory(cls, directory: Path) -> None:
        """Commit creation and re-sync the owner-private ancestor chain on reuse."""
        missing: list[Path] = []
        current = directory
        while True:
            try:
                current.lstat()
            except FileNotFoundError:
                missing.append(current)
                current = current.parent
            else:
                break
        try:
            for path in reversed(missing):
                path.mkdir(mode=0o700, exist_ok=True)
                cls.require_private_directory(path)
                Platform.current().sync_directory(path.parent)
            cls.require_private_directory(directory)
            parent = directory.parent
            while True:
                Platform.current().sync_directory(parent)
                if parent == parent.parent:
                    break
                try:
                    PrivateDirectoryRole.require(
                        parent.lstat(), RelationViolationError("Native Pi parent must be owner-only"))
                except RelationViolationError:
                    break
                parent = parent.parent
        except OSError as error:
            raise NativePiUnavailable("Native Pi session directory could not be committed") from error

    @property
    def session(self) -> Path:
        return self.path

    @property
    def input_proof(self) -> Path:
        # Native IDs are opaque header data, not trusted path components. Hashing
        # keeps the key stable across transcript moves and confined to one file.
        key = hashlib.sha256(self.session_id.encode()).hexdigest()
        return NativeInputProof.path_in(
            self.root, lookup=self.owner.lookup, name=key,
        )


class NativeInputProof(DurableFile, ReconciledFile, PatternFile):
    """One thread's native-session proof, retained and copied with its bus; the installer
    reconciles each onto NativeContextJournal like every other declared store."""

    __slots__ = ()
    filename = "input-proofs/{lookup}/{name}.sqlite"

    @classmethod
    def table_schema(cls) -> type[TableSchema]:
        from .native_pi import NativeContextJournal

        return NativeContextJournal


class BusTranscriptFile(PatternFile):
    """A bus location holding saved native transcripts and their writer locks."""

    __slots__ = ()

    @classmethod
    def member_paths(cls, path: Path) -> frozenset[Path]:
        return super().member_paths(path) | frozenset(NativeSessionLocks(path).members)

    @classmethod
    def transient_paths(cls, path: Path) -> frozenset[Path]:
        return super().transient_paths(path) | frozenset((NativeSessionLocks(path).writer_lock,))

    @classmethod
    def files_in(cls, root: Path) -> frozenset[Path]:
        return super().files_in(root) | frozenset(
            root.glob(str(NativeSessionLocks(cls.pattern()).writer_lock)))


class PrivateSession(DurableFile, BusTranscriptFile):
    """A thread's own session, created fresh for it."""

    __slots__ = ()
    filename = "sessions/enrolled-{session_id}.jsonl"


class ForkedSession(DurableFile, BusTranscriptFile):
    """A fork's copy of its parent's history; the native fork names it in this directory."""

    __slots__ = ()
    filename = "sessions/{stamp}_{session_id}.jsonl"

    @classmethod
    def prepared_directory(cls, root: Path) -> Path:
        directory = (Path(root) / cls.pattern()).parent
        NativeSessionFiles.prepare_private_directory(directory)
        return directory


class CheckSession(DurableFile, BusTranscriptFile):
    """A check root's copy of one selected worker's history, under its record's slot."""

    __slots__ = ()
    filename = "native-sessions/{slot}/enrolled-{session_id}.jsonl"


class NativeAgentSettings(DurableFile, PatternFile):
    """Pi's SettingsManager writes this file in the session's private agent directory."""

    directory_name = ".native-pi-agent"
    filename = f"native-sessions/{{lookup}}/{directory_name}/settings.json"

    @classmethod
    def prepare_directory(cls, session_directory: Path) -> Path:
        directory = session_directory / cls.directory_name
        NativeSessionFiles.prepare_private_directory(directory)
        return directory


class RetainedNativeWriteMarker(DurableFile, PatternFile):
    """The retired PR48 writer left this after an uncertain write; retain its evidence."""

    filename = "sessions/{session}.pr48-writer.lock"
