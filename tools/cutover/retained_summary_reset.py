"""One-use runtime journal retirement; original input/native proof is separate.

Called only by the retained all-stopped installation. No SQLite connection,
old-row decoder, enrollment reconstruction, input retry or target initialization.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import tempfile

from agent_comms.field_codec import FieldCodec
from agent_comms.private_path import FileRevision, PrivateDirectoryRole, PrivateFileRole
from agent_comms.store_files import _replace_snapshot
from publish_openhcs_recovery import fsync_directory, retain_file


@dataclass(frozen=True)
class RetainedRuntimeFile:
    path: Path
    revision: FileRevision
    sha256: str
    descriptor: int

    def checksum(self):
        # dup shares the original offset; every audit starts at its beginning.
        os.lseek(self.descriptor, 0, os.SEEK_SET)
        with os.fdopen(os.dup(self.descriptor), 'rb') as stream:
            return hashlib.file_digest(stream, 'sha256').hexdigest()

    def require_original(self):
        named = self.path.lstat()
        opened = os.fstat(self.descriptor)
        PrivateFileRole.require(named)
        PrivateFileRole.require(opened)
        if named.st_nlink != 1 or opened.st_nlink != 1:
            raise RuntimeError('Runtime journal has another hardlink')
        if FileRevision.from_stat(named) != self.revision:
            raise RuntimeError('Named runtime journal changed during stopped custody')
        if FileRevision.from_stat(opened) != self.revision:
            raise RuntimeError('Opened runtime journal changed during stopped custody')

    def evidence(self):
        return {'path': str(self.path), 'sha256': self.sha256,
                'revision': FieldCodec.encode(self.revision)}

    @contextmanager
    def prepare_replacement(self, candidate: Path, expected_sha256: str):
        """Hold a private candidate on this destination's filesystem.

        Recovery archives may live elsewhere. Failure retains the staged bytes;
        only a completed publication retires its disposable staging directory.
        """
        self.require_original()
        PrivateDirectoryRole.require(self.path.parent.lstat())
        directory = Path(tempfile.mkdtemp(prefix=f'.{self.path.name}.carry-',
                                         dir=self.path.parent))
        PrivateDirectoryRole.require(directory.lstat())
        fsync_directory(self.path.parent)
        path = directory / self.path.name
        proof = retain_file(candidate, path)
        fsync_directory(directory)
        if proof['sha256'] != expected_sha256:
            raise ValueError('Staged candidate differs from reviewed carry')
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            replacement = RetainedRuntimeFile(
                path, FileRevision.from_stat(os.fstat(descriptor)),
                expected_sha256, descriptor)
            replacement.require_original()
            if replacement.checksum() != expected_sha256:
                raise ValueError('Opened staged candidate differs from reviewed carry')
            yield replacement
            path.unlink(missing_ok=True)
            directory.rmdir()
            fsync_directory(self.path.parent)
        finally:
            os.close(descriptor)

    def replace_original(self, original: RetainedRuntimeFile):
        """Publish this held candidate through the existing atomic file owner."""
        original.require_original()
        self.require_original()
        if self.checksum() != self.sha256:
            raise ValueError('Staged candidate changed before publication')
        self.require_original()
        _replace_snapshot(self.path, original.path)
        fsync_directory(original.path.parent)


@dataclass(frozen=True)
class AcquiredRuntimeFiles:
    paths: tuple[Path, ...]
    originals: tuple[RetainedRuntimeFile, ...]

    def unchanged_by(self, changed: set[Path]):
        """Project these same opened resources after a declared carry replaces files."""
        return AcquiredRuntimeFiles(
            tuple(path for path in self.paths if path not in changed),
            tuple(original for original in self.originals if original.path not in changed),
        )

    def require_original(self):
        present = {item.path for item in self.originals}
        if {path for path in self.paths if path.exists() or path.is_symlink()} != present:
            raise RuntimeError('Runtime journal membership changed under stopped custody')
        for original in self.originals:
            original.require_original()
            if original.checksum() != original.sha256:
                raise RuntimeError('Runtime journal bytes changed under stopped custody')
            original.require_original()

    def evidence(self):
        return [item.evidence() for item in self.originals]

    def retain_and_remove(self, destination: Path):
        """Retain EVERY present named member before removing ANY runtime file."""
        self.require_original()
        PrivateDirectoryRole.require(destination.parent.lstat())
        destination.mkdir(mode=0o700)
        PrivateDirectoryRole.require(destination.lstat())
        retained = []
        for original in self.originals:
            proof = retain_file(original.path, destination / original.path.name)
            if proof['sha256'] != original.sha256:
                raise RuntimeError('Preimage differs from acquired original runtime file')
            retained.append(proof)
        fsync_directory(destination)
        fsync_directory(destination.parent)
        self.require_original()
        for original in self.originals:
            original.path.unlink()
        fsync_directory(self.paths[0].parent)
        if any(path.exists() or path.is_symlink() for path in self.paths):
            raise RuntimeError('Runtime journal reappeared under stopped custody')
        return {'classification': 'runtime/reset', 'original_files': retained,
                'retired': [str(item.path) for item in self.originals],
                'original_revisions': [FieldCodec.encode(item.revision) for item in self.originals]}


@dataclass(frozen=True)
class RuntimeCompactionFiles:
    root: Path

    @property
    def paths(self):
        original = self.root / 'compaction-commits.sqlite3'
        return tuple(Path(str(original) + suffix)
                     for suffix in ('', '-journal', '-wal', '-shm'))

    @contextmanager
    def acquire(self):
        with ExitStack() as acquired:
            originals = []
            for path in self.paths:
                try:
                    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                except FileNotFoundError:
                    continue
                acquired.callback(os.close, descriptor)
                info = os.fstat(descriptor)
                PrivateFileRole.require(info)
                original = RetainedRuntimeFile(path, FileRevision.from_stat(info), '', descriptor)
                checksum = original.checksum()
                original = RetainedRuntimeFile(path, original.revision, checksum, descriptor)
                original.require_original()
                originals.append(original)
            present = {item.path for item in originals}
            if present and self.paths[0] not in present:
                raise RuntimeError('Orphan journal companion requires original attempt review')
            resource = AcquiredRuntimeFiles(self.paths, tuple(originals))
            resource.require_original()
            yield resource


class RuntimeGoalFiles(RuntimeCompactionFiles):
    @property
    def paths(self):
        original = self.root / 'goal-private' / 'goal_attempts.sqlite3'
        return tuple(Path(str(original) + suffix)
                     for suffix in ('', '-journal', '-wal', '-shm'))

    def acquire(self):
        directory = self.paths[0].parent
        if directory.exists() or directory.is_symlink():
            PrivateDirectoryRole.require(directory.lstat())
        if any(path.exists() or path.is_symlink() for path in self.paths[1:]):
            raise RuntimeError('Goal ledger companion requires its original stopped recovery')
        return super().acquire()


class RuntimeCompactionReset(RuntimeCompactionFiles):
    def retain_and_remove(self, destination: Path):
        with self.acquire() as acquired:
            return acquired.retain_and_remove(destination)
