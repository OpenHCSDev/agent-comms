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
from typing import ClassVar, Self

from ..declared_family import DeclaredFamily
from ..launch_runtime import LaunchRuntime


class ServiceNotServingError(RuntimeError):
    """A restarted service did not serve its installed runtime's contract."""


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

    @abstractmethod
    def serves(self) -> None:
        """Prove the service answers; raise ServiceNotServingError naming a failure."""

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
        failure = f"{self} did not serve again within {self.patience}s"
        for _ in range(self.patience):
            for instance in type(self).running():
                runtime = instance.runtime()
                if runtime == installed:
                    instance.serves()
                    return f"{instance} restarted onto {installed}"
                failure = f"{instance} runs {runtime}, not the installed {installed}"
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
    notify_variable: ClassVar[str] = "NOTIFY_SOCKET"  # systemd's spelling of the supervisor socket

    @classmethod
    def ready(cls) -> None:
        """Tell the supervisor this process serves.

        Every launch has one: systemd for the installed unit, ``tests/connector_readiness.py``
        for a test. A launch without NOTIFY_SOCKET has nobody to tell and fails here, loudly.
        """
        address = os.environ[cls.notify_variable]
        target = "\0" + address[1:] if address.startswith("@") else address
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notify:
            notify.connect(target)
            notify.sendall(b"READY=1")

    @property
    def unit_file(self) -> Path:
        return Path.home() / ".config/systemd/user" / self.unit

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
        self.unit_file.parent.mkdir(parents=True, exist_ok=True)
        self.unit_file.write_text(self.unit_text(arguments), encoding="utf-8")
        subprocess.run(("systemctl", "--user", "daemon-reload"), check=True)
        subprocess.run(("systemctl", "--user", "enable", self.unit), check=True)

    @classmethod
    def pid(cls) -> int:
        return int(subprocess.check_output(
            ("systemctl", "--user", "show", "-p", "MainPID", "--value", cls.unit),
            text=True,
        ))

    @classmethod
    @abstractmethod
    def from_process(cls, pid: int) -> Self:
        """Read this instance's declared configuration from its running process."""

    @classmethod
    def running(cls) -> tuple[Self, ...]:
        pid = cls.pid()
        return () if pid == 0 else (cls.from_process(pid),)

    @classmethod
    def process_arguments(cls, pid: int) -> tuple[str, ...]:
        arguments = tuple(os.fsdecode(word) for word in
                          Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[:-1])
        if not arguments or not arguments[0]:
            raise ServiceNotServingError(f"{cls.unit} has no running command")
        return arguments

    def runtime(self) -> Path:
        return Path(self.process_arguments(self.pid())[0])

    def restart(self) -> None:
        subprocess.run(("systemctl", "--user", "restart", self.unit), check=True)
