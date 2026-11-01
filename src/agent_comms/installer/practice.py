"""The practice bus a rehearsal carries: its copy, private home, size and space."""

from __future__ import annotations

import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from math import ceil
from collections.abc import Iterable

from agent_comms.context_store import ContextManifestReference, ContextStore
from agent_comms.native_session_reopen import NativeSessionIdentity
from agent_comms.nominal.field_codec import boundary_decoder
from agent_comms.installer.refusals import UpgradeRefused
from agent_comms.installer.declared_uv import DeclaredUv
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.locked_store import (
    DeclaredFile, LiveFilesRequest, RegistryFile, RetainedCopyRequest, RootFile, UndeclaredFile,
)
from agent_comms.maintenance_barrier import MaintenanceBarrier
from agent_comms.storage_rules import StorageRule
from agent_comms.private_path import PrivateDirectoryRole


@dataclass(frozen=True)
class PracticeMeasurement:
    bytes: int

    @classmethod
    def capture(cls, copy: Path) -> PracticeMeasurement:
        """Count the complete materialized tree, including SQLite companions and stages."""
        return cls(sum(path.stat().st_size for path in copy.rglob("*") if path.is_file()))

    def remaining(self, maximum_bytes: int) -> int:
        """An explicit selected-input allowance, not a limit on complete backups."""
        if self.bytes > maximum_bytes:
            raise UpgradeRefused(
                f"Practice copy has {self.bytes} bytes, exceeding {maximum_bytes} allowed bytes")
        return maximum_bytes - self.bytes

    @classmethod
    def of(cls, copy: Path, floor: float) -> PracticeMeasurement:
        """Refuse a copy without room for the rehearsal's second copy above the disk floor. The
        free space is what a copy can cost; a fixed byte cap beside it refused a 3.01 GiB bus
        with 29 GiB free and stopped install 45."""
        measured = cls.capture(copy)
        if shutil.disk_usage(copy.parent).free < 2 * measured.bytes + floor:
            raise UpgradeRefused(
                f"Insufficient free space for rehearsal of {measured.bytes} copied bytes")
        return measured


@dataclass(frozen=True)
class RuntimeHome(ABC):
    """A private home a runtime build runs in; its kind declares its runtime link and uv."""

    path: Path

    @staticmethod
    def uv_path(home: Path) -> Path:
        return home / DeclaredUv.installed

    @property
    @abstractmethod
    def uv(self) -> Path: ...

    def prepare(self) -> None:
        source = self.uv
        PrivateDirectoryRole.make_parents(self.path)
        uv = self.uv_path(self.path)
        PrivateDirectoryRole.make_parents(uv.parent)
        uv.unlink(missing_ok=True)
        uv.symlink_to(source)
        cache = Path(subprocess.run(["npm", "config", "get", "cache"], capture_output=True,
                                    text=True, check=True).stdout.strip())
        if not (self.path / ".npm").exists() and cache.is_dir():
            (self.path / ".npm").symlink_to(cache, target_is_directory=True)
        elif not (self.path / ".npm").exists():
            (self.path / ".npm").mkdir(mode=0o700)


class RehearsalHome(RuntimeHome):
    """The live install's rehearsal borrows the installed runtime and the installer's uv,
    both resolved strictly before the home is touched, so a broken installation refuses
    the rehearsal instead of rehearsing on PATH."""

    @property
    def uv(self) -> Path:
        return self.uv_path(Path.home()).resolve(strict=True)

    @property
    def selected_runtime(self) -> LaunchRuntime:
        return LaunchRuntime.installed()

    def prepare(self) -> None:
        installed = LaunchRuntime.current_pointer()
        selected = self.selected_runtime.prefix.resolve(strict=True)
        super().prepare()
        pointer = self.path / installed.relative_to(Path.home())
        PrivateDirectoryRole.make_parents(pointer.parent)
        pointer.unlink(missing_ok=True)
        pointer.symlink_to(selected)


@dataclass(frozen=True)
class SuppliedRuntimeHome(RehearsalHome):
    """The same private-home setup, bound to an explicitly supplied runtime.

    An installed rehearsal selects the current publication at preparation;
    a candidate's home must keep its declared runtime when that publication moves.
    """

    runtime: LaunchRuntime

    @property
    def selected_runtime(self) -> LaunchRuntime:
        return self.runtime


class TestHome(RuntimeHome):
    """A source checkout's test build: no installed runtime, the uv its caller put on PATH."""

    @property
    @boundary_decoder
    def uv(self) -> Path:
        executable = shutil.which("uv")
        if executable is None:
            raise UpgradeRefused("A private test runtime home requires uv on PATH")
        return Path(executable)


@dataclass
class PracticeCopy:
    """A private, incrementally selected cut of the source owners' files."""

    root: Path
    path: Path
    copied: dict[Path, DeclaredFile | UndeclaredFile] = field(default_factory=dict)

    @property
    def request(self) -> RetainedCopyRequest:
        """The copy's files, still naming the live root their recorded paths refer to."""
        return RetainedCopyRequest(self.path, self.root)

    @property
    def copied_names(self) -> tuple[Path, ...]:
        return tuple(sorted(resource.owner.relocated(
            resource.live(self.root), resource.path, source,
        ) for source, resource in self.copied.items()))

    @property
    def floor_bytes(self) -> int:
        return ceil(StorageRule.load(self.root).floor_gib * 2**30)

    @property
    def available_bytes(self) -> int:
        return shutil.disk_usage(self.path.parent).free - self.floor_bytes

    def prepare(self) -> None:
        """One space and private-directory owner for whole and selected materialization."""
        PrivateDirectoryRole.make_parents(self.path)
        if self.available_bytes < 0:
            raise UpgradeRefused("Insufficient free space before building the practice copy")

    def remaining_bytes(self, maximum_bytes: int) -> int:
        """Share one explicit total allowance across incrementally selected stores.

        The current tree, not the copied-resource index, owns used bytes. This
        includes retained WAL/groups and data placed there by another copy step.
        Caller owns this private copy's serial materialization and wider cut.
        """
        return min(PracticeMeasurement.capture(self.path).remaining(maximum_bytes),
                   self.available_bytes)

    def include_contexts(
        self, identities: Iterable[NativeSessionIdentity], *, maximum_bytes: int,
        requests: Iterable[ContextManifestReference] = (),
    ) -> None:
        """Explicit selected-context inputs, never a full-file backup.

        Normal include() and the installer's live-root backup remain whole-file
        operations. This selection must precede including the context resource;
        an existing destination is refused rather than overwritten or reimported.
        """
        self.prepare()
        source = ContextStore(self.root)
        resource, = RootFile.owned_files(LiveFilesRequest(self.root), (source.path,))
        source.retain_contexts(
            ContextStore(self.path), identities, requests=requests,
            maximum_bytes=self.remaining_bytes(maximum_bytes),
        )
        self.copied[source.path] = resource

    def include(self, paths: tuple[Path, ...]) -> None:
        self.prepare()
        witnesses = set(MaintenanceBarrier(RegistryFile.path_in(self.root)).witness_paths)
        for resource in RootFile.owned_files(LiveFilesRequest(self.root), paths):
            source = resource.live(self.root)
            if source in self.copied or source in witnesses or not source.exists():
                continue
            target = resource.retained(self.path)
            PrivateDirectoryRole.make_parents(target.parent)
            self.copied.update((member, resource) for member in resource.owner.copy_group(source, target))

    def retain(self, paths: tuple[Path, ...]) -> None:
        """Discard inputs used only to establish that an earlier carry was finished."""
        selected = RootFile.owned_files(LiveFilesRequest(self.root), paths)
        discarded = tuple(source for source, resource in self.copied.items() if resource not in selected)
        for source in discarded:
            resource = self.copied[source]
            primary = resource.live(self.root)
            resource.owner.relocated(primary, resource.retained(self.path), source).unlink(missing_ok=True)
            del self.copied[source]

    @property
    def measurement(self) -> PracticeMeasurement:
        return PracticeMeasurement.of(self.path, self.floor_bytes)
