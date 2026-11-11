from __future__ import annotations

import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from agent_comms.execution.launch_runtime import LaunchRuntime
from agent_comms.installer.declared_uv import DeclaredUv
from agent_comms.installer.refusals import UpgradeRefused
from agent_comms.nominal.field_codec import boundary_decoder
from agent_comms.storage.private_path import PrivateDirectoryRole


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
