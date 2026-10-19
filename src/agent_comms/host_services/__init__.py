"""Long-running services a host runs on the installed code, each declared by its owner.

A service keeps the code it loaded when it started, so an install restarts every declared
service that is running on this host onto the installed runtime. A host that does not run a
service has no instance of it and contributes nothing: there is no list of services to keep.
"""

from __future__ import annotations

import subprocess
import time
from abc import abstractmethod
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
    def restart_running(cls, installed: Path) -> tuple[str, ...]:
        """Restart every declared service running on this host onto ``installed``."""
        return tuple(
            instance.restart_onto(installed)
            for member in cls.members_with(cls)
            for instance in member.running()
        )

    def restart_onto(self, installed: Path) -> str:
        self.restart()
        for _ in range(self.patience):
            for instance in type(self).running():
                if instance != self:
                    return instance.serving(installed)
            time.sleep(1)
        raise ServiceNotServingError(f"{self} did not serve again within {self.patience}s")

    def serving(self, installed: Path) -> str:
        if self.runtime() != installed:
            raise ServiceNotServingError(
                f"{self} runs {self.runtime()}, not the installed {installed}"
            )
        return f"{self} restarted onto {installed}"


class SystemdUserService(HostService):
    """A service a systemd user unit runs; the unit's own definition starts it again."""

    @abstractmethod
    def unit(self) -> str:
        """The user unit this instance runs in."""

    def restart(self) -> None:
        subprocess.run(("systemctl", "--user", "restart", self.unit()), check=True)
