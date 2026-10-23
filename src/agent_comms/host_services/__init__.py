"""Long-running services a host runs on the installed code, each declared by its owner.

A service keeps the code it loaded when it started, so an install restarts every declared
service that is running on this host onto the installed runtime. A host that does not run a
service has no instance of it and contributes nothing: there is no list of services to keep.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, ClassVar, Self

from aiohttp.web import TCPSite

from ..child_process import ProcessIdentity
from ..declared_family import DeclaredFamily
from ..field_codec import PathText
from ..launch_runtime import LaunchRuntime


class ServiceNotServingError(RuntimeError):
    """A restarted service did not serve its installed runtime's contract."""


class SystemdSite(TCPSite):
    """The listening site notifies its supervisor only after binding succeeds."""

    notify_variable: ClassVar[str] = "NOTIFY_SOCKET"  # systemd's supervisor socket

    async def start(self) -> None:
        await super().start()
        address = os.environ[self.notify_variable]
        target = "\0" + address[1:] if address.startswith("@") else address
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notify:
            notify.connect(target)
            notify.sendall(b"READY=1")


@dataclass(frozen=True)
class RestartedService:
    """One host service an install restarted, in its own words, and the runtime it now
    serves from."""

    service: str
    runtime: Annotated[Path, PathText]

    def line(self) -> str:
        return f"{self.service} restarted onto {self.runtime}"


class HostService(DeclaredFamily, affix="Service", discovery_package=__name__):
    """One running instance of a declared service on this host."""

    patience: ClassVar[int] = 30
    """Seconds a restarted service has to serve again."""

    @classmethod
    @abstractmethod
    def running(cls) -> tuple[Self, ...]:
        """This service's instances on this host; none when the host does not run it."""

    @abstractmethod
    def stop(self) -> None:
        """Stop this instance; its launcher stays installed."""

    @abstractmethod
    def start(self) -> None:
        """Start this stopped instance from its own launcher."""

    @abstractmethod
    def runtime(self) -> Path:
        """The interpreter this instance runs."""

    @abstractmethod
    def serves(self) -> None:
        """Prove the service answers; raise ServiceNotServingError naming a failure.

        The proof reads the root, which admits only the root's owning runtime, so it runs
        in that runtime (``ServingProbe``), never in an installer of another version."""

    @abstractmethod
    def described(self) -> str:
        """This instance in the words an install's report and its failures use."""

    @classmethod
    def instances(cls) -> tuple[HostService, ...]:
        return tuple(instance for member in cls.members_with(cls) for instance in member.running())

    @classmethod
    def stop_running(cls) -> tuple[HostService, ...]:
        """Stop every running service, so nothing writes the root while it is restored."""
        stopped = cls.instances()
        for instance in stopped:
            instance.stop()
        return stopped

    def start_onto(self, installed: Path) -> RestartedService:
        self.start()
        return self.running_on(installed)

    def running_on(self, installed: Path) -> RestartedService:
        failure = f"{self.described()} did not run again within {self.patience}s"
        for _ in range(self.patience):
            runtime = self.runtime()
            if runtime == installed:
                return RestartedService(self.described(), installed)
            failure = f"{self.described()} runs {runtime}, not the installed {installed}"
            time.sleep(1)
        raise ServiceNotServingError(failure)


@dataclass(frozen=True)
class SystemdUserService(HostService, declared_name=None):
    """Systemd owns the unit's process; its definition starts that process again.

    Its owned unit declares readiness. The process reports READY=1 once it serves,
    so a restart returns only when it is serving. Systemd, not this configuration,
    owns the current PID.
    """

    program: ClassVar[str]
    unit: ClassVar[str]
    @classmethod
    def unit_file(cls) -> Path:
        return Path.home() / ".config/systemd/user" / cls.unit

    def unit_name(self) -> str:
        return self.unit

    def unit_text(self, arguments: tuple[str, ...]) -> str:
        executable = LaunchRuntime.current_pointer() / "bin" / self.program
        # systemd uses C-quoted words, then expands specifiers and environment
        # references. Protect literal percent/dollar signs before quoting.
        command = " ".join(json.dumps(word.replace("%", "%%").replace("$", "$$"),
                                      ensure_ascii=False)
                           for word in (str(executable), *arguments))
        return (
            f"[Unit]\nDescription={self.unit}\n\n"
            "[Service]\nType=notify\nNotifyAccess=main\n"
            f"ExecStart={command}\nRestart=on-failure\n\n"
            "[Install]\nWantedBy=default.target\n"
        )

    def install(self, arguments: tuple[str, ...]) -> None:
        self.unit_file().parent.mkdir(parents=True, exist_ok=True)
        self.unit_file().write_text(self.unit_text(arguments), encoding="utf-8")
        subprocess.run(("systemctl", "--user", "daemon-reload"), check=True)
        subprocess.run(("systemctl", "--user", "enable", self.unit), check=True)

    @classmethod
    def _pid_for(cls, name: str) -> int:
        """Only the unit loaded from this home's owned definition belongs to it.

        The user manager belongs to the UID, not HOME; a private install must not
        discover or restart the real home's service through that shared manager.
        """
        unit = cls.unit_file()
        if not unit.is_file():
            return 0
        loaded = subprocess.check_output(
            ("systemctl", "--user", "show", "-p", "FragmentPath", "--value", name),
            text=True,
        ).strip()
        if Path(loaded).resolve() != unit.resolve():
            return 0
        return int(subprocess.check_output(
            ("systemctl", "--user", "show", "-p", "MainPID", "--value", name),
            text=True,
        ))

    def pid(self) -> int:
        return self._pid_for(self.unit_name())

    @classmethod
    def running_arguments(cls, pid: int) -> tuple[str, ...]:
        try:
            return ProcessIdentity.capture(pid).arguments()
        except ProcessLookupError:
            raise ServiceNotServingError(f"{cls.unit} has no running command") from None

    def runtime(self) -> Path:
        return Path(self.running_arguments(self.pid())[0])

    def stop(self) -> None:
        subprocess.run(("systemctl", "--user", "stop", self.unit_name()), check=True)

    def start(self) -> None:
        subprocess.run(("systemctl", "--user", "start", self.unit_name()), check=True)
