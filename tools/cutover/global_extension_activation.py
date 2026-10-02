"""Reviewed global source activation inside the existing stopped-owner batch.

Source bytes and their compiled package are one deployment. This operation runs
as the publisher's installation member; it never launches or retries an input.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
import stat
import subprocess

from agent_comms.owner_cutover import StoppedOwnerInstallation
from agent_comms.private_path import PrivateFileRole, TrustedAncestorRole
from agent_comms.store_files import _atomic_write_text
from publish_openhcs_recovery import digest, fsync_directory
from publish_retained_summary import ReviewedArtifact


@dataclass(frozen=True)
class GlobalSourceInstall(ABC):
    source: ReviewedArtifact
    destination: Path

    def require_original(self):
        self.source.require_original()
        TrustedAncestorRole.require(self.destination.parent.lstat())
        self.require_destination()

    @abstractmethod
    def require_destination(self):
        pass

    @abstractmethod
    def retain_original(self, directory: Path):
        pass

    def install(self):
        self.require_original()
        _atomic_write_text(self.destination, self.source.path.read_text(),
                           fsync_parent=True, mode=self.installation_mode())
        if digest(self.destination) != self.source.sha256:
            raise RuntimeError(f'Installed global source differs: {self.destination}')

    def installation_mode(self) -> int:
        return PrivateFileRole.permissions


@dataclass(frozen=True)
class ReplaceGlobalSource(GlobalSourceInstall):
    original: ReviewedArtifact

    def installation_mode(self) -> int:
        return stat.S_IMODE(self.original.path.stat().st_mode)

    def require_destination(self):
        if self.original.path != self.destination:
            raise RuntimeError('Source preimage names another destination')
        if self.destination.is_symlink() or not self.destination.is_file():
            raise RuntimeError('Global source must be a regular file')
        self.original.require_original()

    def retain_original(self, directory):
        self.require_original()
        preimage = directory / self.destination.name
        _atomic_write_text(preimage, self.destination.read_text(), fsync_parent=True)
        if digest(preimage) != self.original.sha256:
            raise RuntimeError('Global source changed while preserving its preimage')


@dataclass(frozen=True)
class CreateGlobalSource(GlobalSourceInstall):
    def require_destination(self):
        if self.destination.exists() or self.destination.is_symlink():
            raise RuntimeError(f'New source destination already exists: {self.destination}')

    def retain_original(self, directory):
        self.require_original()


@dataclass(frozen=True)
class ActivateGlobalExtension(StoppedOwnerInstallation):
    sources: tuple[GlobalSourceInstall, ...]
    preimages: Path

    def recovery_paths(self) -> frozenset[Path]:
        return frozenset(source.destination for source in self.sources)

    def require_selection(self, snapshot, owners):
        if self.preimages.exists() or self.preimages.is_symlink():
            raise RuntimeError('Original source activation attempt requires review')
        for source in self.sources:
            source.require_original()

    def after_stopped(self, lifecycle):
        self.require_selection(lifecycle.registry.snapshot(), ())
        self.preimages.mkdir(mode=0o700)
        for source in self.sources:
            source.retain_original(self.preimages)
        fsync_directory(self.preimages)
        fsync_directory(self.preimages.parent)
        for source in self.sources:
            source.install()


@dataclass(frozen=True)
class VerifiedGlobalExtension(ActivateGlobalExtension):
    verification_script: ReviewedArtifact
    interpreter: Path
    verification_args: tuple[str, ...]
    verification_log: Path

    def require_selection(self, snapshot, owners):
        super().require_selection(snapshot, owners)
        self.verification_script.require_original()
        if self.verification_log.exists() or self.verification_log.is_symlink():
            raise RuntimeError('Original global verification requires review')

    def after_stopped(self, lifecycle):
        super().after_stopped(lifecycle)
        self.verification_script.require_original()
        with self.verification_log.open('x') as log:
            subprocess.run((str(self.interpreter), '-I', str(self.verification_script.path),
                            *self.verification_args), stdout=log, stderr=subprocess.STDOUT,
                           check=True, timeout=120)
