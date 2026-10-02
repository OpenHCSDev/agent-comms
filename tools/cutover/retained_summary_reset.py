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

from agent_comms.field_codec import FieldCodec
from agent_comms.private_path import FileRevision, PrivateDirectoryRole, PrivateFileRole
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
        return [{'path': str(item.path), 'sha256': item.sha256,
                 'revision': FieldCodec.encode(item.revision)} for item in self.originals]

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


class RuntimeCompactionReset(RuntimeCompactionFiles):
    def retain_and_remove(self, destination: Path):
        with self.acquire() as acquired:
            return acquired.retain_and_remove(destination)
