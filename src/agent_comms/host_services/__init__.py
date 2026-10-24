"""Long-running services a host runs on the installed code, each declared by its owner.

A service keeps the code it loaded when it started, so an install restarts every declared
service that is running on this host onto the installed runtime. A host that does not run a
service has no instance of it and contributes nothing: there is no list of services to keep.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import time
from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, ClassVar, Self

from ..child_process import ProcessIdentity
from ..declared_family import DeclaredFamily
from ..field_codec import PathText
from ..launch_runtime import LaunchRuntime


class ServiceNotServingError(RuntimeError):
    """A restarted service did not serve its installed runtime's contract."""


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

    @classmethod
    def installed(cls) -> tuple[Self, ...]:
        """The instances an install leaves serving: by default, the ones running now."""
        return cls.running()

    @classmethod
    @abstractmethod
    def stop_all(cls, instances: tuple[Self, ...]) -> None:
        """Stop these running instances together; their launchers stay installed.

        Instances whose classes inherit one ``stop_all`` stop in one call, so the wait is the
        slowest service's, not the sum of one stop per member."""
        owners = tuple(dict.fromkeys(type(instance).stop_owner() for instance in instances))
        for owner in owners:
            owner.stop_all(tuple(i for i in instances if type(i).stop_owner() is owner))

    @classmethod
    def stop_owner(cls) -> type[HostService]:
        """The class whose ``stop_all`` stops this member: the nearest that declares it."""
        return next(base for base in cls.__mro__ if "stop_all" in vars(base))

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
        """Stop every running service, so nothing writes the root while it is restored,
        and answer every installed one, so a service already dead is started again too."""
        installed = tuple(instance for member in cls.members_with(cls)
                          for instance in member.installed())
        cls.stop_all(cls.instances())
        return installed

    def start_onto(self, installed: Path) -> RestartedService:
        self.start()
        return self.running_on(installed)

    def running_on(self, installed: Path) -> RestartedService:
        failure = f"{self.described()} did not run again within {self.patience}s"
        for _ in range(self.patience):
            for instance in type(self).running():
                if instance == self:
                    runtime = instance.runtime()
                    if runtime == installed:
                        return RestartedService(instance.described(), installed)
                    failure = (
                        f"{instance.described()} runs {runtime}, not the installed {installed}"
                    )
            time.sleep(1)
        raise ServiceNotServingError(failure)


class SystemdUnit:
    """A unit of the user's systemd manager: systemd, never the caller, owns its processes,
    so stopping the caller's own unit cannot reach them."""

    @staticmethod
    def shown(unit: str, name: str) -> str:
        """One property of the unit as its manager reports it."""
        return subprocess.check_output(
            ("systemctl", "--user", "show", "-p", name, "--value", unit), text=True,
        ).strip()


@dataclass(frozen=True)
class SystemdUserService(SystemdUnit, HostService, declared_name=None):
    """Systemd owns the unit's process; its definition starts that process again.

    Each member declares its readiness options. Systemd, not this configuration,
    owns the current PID; the member's serving proof checks the running service.
    """

    program: ClassVar[str]
    unit: ClassVar[str]
    service_options: ClassVar[tuple[str, ...]] = ("Type=notify", "NotifyAccess=main")

    @classmethod
    def unit_file(cls, home: Path) -> Path:
        return home / ".config/systemd/user" / cls.unit

    def unit_name(self) -> str:
        return self.unit

    @classmethod
    def command_line(cls, arguments: tuple[str, ...]) -> str:
        executable = LaunchRuntime.current_pointer() / "bin" / cls.program
        # systemd uses C-quoted words, then expands specifiers and environment
        # references. Protect literal percent/dollar signs before quoting.
        return " ".join(
            json.dumps(word.replace("%", "%%").replace("$", "$$"), ensure_ascii=False)
            for word in (str(executable), *arguments)
        )

    @classmethod
    def enabled_commands(cls) -> tuple[tuple[str, ...], ...]:
        """The command this home's enabled unit declares, read back from ``command_line``;
        enabling links the unit into ``default.target``, which ``render_unit`` wants."""
        unit = cls.unit_file(Path.home())
        return tuple(
            tuple(word.replace("%%", "%").replace("$$", "$")
                  for word in shlex.split(line.removeprefix("ExecStart=")))
            for wanted in (unit.parent / "default.target.wants" / cls.unit,) if wanted.exists()
            for line in unit.read_text(encoding="utf-8").splitlines()
            if line.startswith("ExecStart=")
        )

    @classmethod
    def render_unit(cls, command: str) -> str:
        options = "\n".join(cls.service_options)
        return (
            f"[Unit]\nDescription={cls.unit}\n\n"
            f"[Service]\n{options}\n"
            f"ExecStart={command}\nRestart=on-failure\n\n"
            "[Install]\nWantedBy=default.target\n"
        )

    @classmethod
    def unit_text(cls, arguments: tuple[str, ...]) -> str:
        return cls.render_unit(cls.command_line(arguments))

    @classmethod
    def install(cls, arguments: tuple[str, ...] = (), *, home: Path) -> None:
        unit = cls.unit_file(home)
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(cls.unit_text(arguments), encoding="utf-8")
        subprocess.run(("systemctl", "--user", "daemon-reload"), check=True)

    def enable(self) -> None:
        subprocess.run(("systemctl", "--user", "enable", self.unit_name()), check=True)

    def retire(self) -> None:
        """Stop this instance and keep login and installs from starting it again."""
        subprocess.run(("systemctl", "--user", "disable", "--now", self.unit_name()), check=True)

    def restore(self) -> None:
        """Undo ``retire`` when the change it made room for did not commit."""
        subprocess.run(("systemctl", "--user", "enable", "--now", self.unit_name()), check=True)

    @classmethod
    def _pid_for(cls, name: str) -> int:
        """Only the unit loaded from this home's owned definition belongs to it.

        The user manager belongs to the UID, not HOME; a private install must not
        discover or restart the real home's service through that shared manager.
        """
        unit = cls.unit_file(Path.home())
        if not unit.is_file():
            return 0
        if Path(cls.shown(name, "FragmentPath")).resolve() != unit.resolve():
            return 0
        return int(cls.shown(name, "MainPID"))

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

    @classmethod
    def stop_all(cls, instances: tuple[Self, ...]) -> None:
        if not instances:
            return
        subprocess.run(
            ("systemctl", "--user", "stop", *(instance.unit_name() for instance in instances)),
            check=True,
        )

    def start(self) -> None:
        subprocess.run(("systemctl", "--user", "start", self.unit_name()), check=True)
