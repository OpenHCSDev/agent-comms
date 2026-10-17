"""A saved native session file and the sidecars kept beside it."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .errors import RelationViolationError


@dataclass(frozen=True, slots=True)
class NativeSessionFiles:
    """One saved session: the transcript, its input-proof journal and writer lock.

    The input proof is a SQLite journal written by the native process; its
    rollback/WAL companions belong to it. Any other file sharing the session's
    name is unexpected and refuses deletion.
    """

    session: Path

    @classmethod
    def of(cls, session_file: str) -> NativeSessionFiles:
        return cls(Path(session_file).resolve())

    @property
    def input_proof(self) -> Path:
        return Path(f"{self.session}.input-proof")

    @property
    def writer_lock(self) -> Path:
        return self.session.with_name(f".{self.session.name}.agent-comms-writer.lock")

    @property
    def members(self) -> tuple[Path, ...]:
        proof = self.input_proof
        return (
            self.session,
            proof,
            *(proof.with_name(proof.name + suffix) for suffix in ("-journal", "-wal", "-shm")),
            self.writer_lock,
        )

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
