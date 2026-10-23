"""The practice bus a rehearsal carries: its copy, private home, size and space."""

from __future__ import annotations

import shutil
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from agent_comms.field_codec import boundary_decoder
from agent_comms.installer.refusals import UpgradeRefused
from agent_comms.installer.declared_uv import DeclaredUv
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.locked_store import RegistryFile, RootFile
from agent_comms.maintenance_barrier import MaintenanceBarrier
from agent_comms.storage_rules import StorageRule


@dataclass(frozen=True)
class PracticeMeasurement:
    bytes: int
    seconds: float

    @classmethod
    def of(cls, copy: Path, seconds: float, floor: float) -> PracticeMeasurement:
        """Refuse a copy without room for the rehearsal's second copy above the disk floor. The
        free space is what a copy can cost; a fixed byte cap beside it refused a 3.01 GiB bus
        with 29 GiB free and stopped install 45."""
        size = sum(path.stat().st_size for path in copy.rglob("*") if path.is_file())
        if shutil.disk_usage(copy.parent).free < 2 * size + floor:
            raise UpgradeRefused(f"Insufficient free space for rehearsal of {size} copied bytes")
        return cls(size, seconds)


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
        self.path.mkdir(mode=0o700, parents=True, exist_ok=True)
        uv = self.uv_path(self.path)
        uv.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
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

    def prepare(self) -> None:
        installed = LaunchRuntime.current_pointer()
        selected = installed.resolve(strict=True)
        super().prepare()
        pointer = self.path / installed.relative_to(Path.home())
        pointer.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        pointer.unlink(missing_ok=True)
        pointer.symlink_to(selected)


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
    copied: dict[Path, Path] = field(default_factory=dict)
    seconds: float = 0.0

    def include(self, paths: tuple[Path, ...]) -> None:
        floor = StorageRule.load(self.root).floor_gib * 2**30
        self.path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if shutil.disk_usage(self.path.parent).free < floor:
            raise UpgradeRefused("Insufficient free space before building the practice copy")
        started = time.monotonic()
        witnesses = set(MaintenanceBarrier(RegistryFile.path_in(self.root)).witness_paths)
        for resource, owner in RootFile.owned_files(self.root, paths).items():
            source = self.root / resource.path
            if source in self.copied or source in witnesses or not source.exists():
                continue
            target = self.path / resource.path
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.copied.update((member, source) for member in owner.copy_group(source, target))
        self.seconds += time.monotonic() - started

    def retain(self, paths: tuple[Path, ...]) -> None:
        """Discard inputs used only to establish that an earlier carry was finished."""
        selected = {self.root / resource.path for resource in RootFile.owned_files(self.root, paths)}
        discarded = tuple(source for source, primary in self.copied.items() if primary not in selected)
        for source in discarded:
            (self.path / source.relative_to(self.root)).unlink(missing_ok=True)
            del self.copied[source]

    @property
    def measurement(self) -> PracticeMeasurement:
        return PracticeMeasurement.of(
            self.path, self.seconds, StorageRule.load(self.root).floor_gib * 2**30)
