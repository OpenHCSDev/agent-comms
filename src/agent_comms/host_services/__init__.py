"""Long-running services a host runs on the installed code, each declared by its owner.

A service keeps the code it loaded when it started, so an install restarts every declared
service that is running on this host onto the installed runtime. A host that does not run a
service has no instance of it and contributes nothing: there is no list of services to keep.
"""

from __future__ import annotations

import os
import subprocess
import time
from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Self

from ..declared_family import DeclaredFamily


class ServiceNotServingError(RuntimeError):
    """A restarted service did not come back on the installed runtime."""


class HostService(DeclaredFamily, affix="Service", discovery_package=__name__):
    """One running instance of a declared service on this host."""

    patience: ClassVar[int] = 30
    """Seconds a restarted service has to serve again."""

    @classmethod
    @abstractmethod
    def running(cls) -> tuple[Self, ...]:
        """This service's instances on this host; none when the host does not run it."""

    @abstractmethod
    def restart(self) -> None:
        """Stop this instance and start it again from its own launcher."""

    @abstractmethod
    def runtime(self) -> Path:
        """The interpreter this instance runs."""

    @classmethod
    def restart_running(cls, installed: Path, installation: Path) -> tuple[str, ...]:
        """Restart this installation's services, never another root's runtime family."""
        return tuple(
            instance.restart_onto(installed)
            for member in cls.members_with(cls)
            for instance in member.running()
            if instance.runtime().parent.resolve().is_relative_to(installation.resolve())
        )

    def restart_onto(self, installed: Path) -> str:
        self.restart()
        failure = f"{self} did not serve again within {self.patience}s"
        for _ in range(self.patience):
            for instance in type(self).running():
                if instance != self:
                    runtime = instance.runtime()
                    if runtime == installed:
                        return f"{instance} restarted onto {installed}"
                    failure = f"{instance} runs {runtime}, not the installed {installed}"
            time.sleep(1)
        raise ServiceNotServingError(failure)


@dataclass(frozen=True)
class SystemdUserService(HostService, declared_name=None):
    """Systemd owns the unit's process; its definition starts that process again."""

    pid: int
    unit: ClassVar[str]

    @classmethod
    def running(cls) -> tuple[Self, ...]:
        pid = int(subprocess.check_output(
            ("systemctl", "--user", "show", "-p", "MainPID", "--value", cls.unit),
            text=True,
        ))
        return () if pid == 0 else (cls(pid),)

    def runtime(self) -> Path:
        executable = Path(f"/proc/{self.pid}/cmdline").read_bytes().split(b"\0")[0]
        if not executable:
            raise ServiceNotServingError(f"{self} has no running command")
        return Path(os.fsdecode(executable))

    def restart(self) -> None:
        subprocess.run(("systemctl", "--user", "restart", self.unit), check=True)
