"""One-use runtime journal retirement; original input/native proof is separate.

Called only by the retained all-stopped installation. No SQLite connection,
old-row decoder, enrollment reconstruction, input retry or target initialization.
"""
from __future__ import annotations

from abc import abstractmethod
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
from typing import Annotated

from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec, PathText
from agent_comms.private_path import FileRevision, PrivateDirectoryRole, PrivateFileRole
from publish_openhcs_recovery import fsync_directory, retain_file


@dataclass(frozen=True)
class RetainedRuntimeFile:
    path: Path
    revision: FileRevision
    sha256: str
    descriptor: int

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
class RuntimeCompactionPolicy(DeclaredFamily, affix="CompactionPolicy"):
    root: Annotated[Path, PathText]

    @property
    def paths(self):
        original = self.root / 'compaction-commits.sqlite3'
        return tuple(Path(str(original) + suffix)
                     for suffix in ('', '-journal', '-wal', '-shm'))

    @contextmanager
    def acquired(self):
        """Both policies use the same original file and namespace custody."""
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
                with os.fdopen(os.dup(descriptor), 'rb') as stream:
                    checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
                original = RetainedRuntimeFile(path, FileRevision.from_stat(info), checksum, descriptor)
                original.require_original()
                originals.append(original)
            present = {item.path for item in originals}
            if present and self.paths[0] not in present:
                raise RuntimeError('Orphan journal companion requires original attempt review')
            self.require_current(originals)
            yield tuple(originals)

    def require_current(self, originals):
        if {path for path in self.paths if path.exists() or path.is_symlink()} != {item.path for item in originals}:
            raise RuntimeError('Runtime journal membership changed during stopped custody')
        for original in originals:
            original.require_original()

    @abstractmethod
    def protect(self, paths, destination: Path):
        """Own whether untouched durable originals require additional preimages."""

    @abstractmethod
    def apply(self, destination: Path):
        """Apply the reviewed format policy while original owners are stopped."""


class ResetCompactionPolicy(RuntimeCompactionPolicy):
    def protect(self, paths, destination: Path):
        return [retain_file(path, destination / f'protected-{index}')
                for index, path in enumerate(sorted(paths))]

    def apply(self, destination: Path):
        """Retain EVERY present named member before removing ANY runtime file."""
        PrivateDirectoryRole.require(destination.parent.lstat())
        destination.mkdir(mode=0o700)
        PrivateDirectoryRole.require(destination.lstat())
        with self.acquired() as originals:
            retained = []
            for original in originals:
                original.require_original()
                proof = retain_file(original.path, destination / original.path.name)
                if proof['sha256'] != original.sha256:
                    raise RuntimeError('Preimage differs from acquired original runtime file')
                retained.append(proof)
            fsync_directory(destination)
            fsync_directory(destination.parent)
            # The WHOLE set and its private, durable preimages are checked before
            # the first unlink. Added companions cannot escape this observation.
            self.require_current(originals)
            for original in originals:
                original.path.unlink()
            fsync_directory(self.root)
            if any(path.exists() or path.is_symlink() for path in self.paths):
                raise RuntimeError('Runtime journal reappeared under stopped custody')
            return {'classification': 'runtime/reset', 'original_files': retained,
                    'retired': [str(item.path) for item in originals],
                    'original_revisions': [FieldCodec.encode(item.revision) for item in originals]}


class PreserveCompactionPolicy(RuntimeCompactionPolicy):
    def protect(self, paths, destination: Path):
        # Publisher hashes every original before and after this operation. No
        # original is rewritten, so another native/session copy is unnecessary.
        return []

    def apply(self, destination: Path):
        with self.acquired() as originals:
            self.require_current(originals)
            return {'classification': 'runtime/preserve', 'retired': [],
                    'original_files': [{'path': str(item.path), 'sha256': item.sha256,
                                        'revision': FieldCodec.encode(item.revision)}
                                       for item in originals]}
