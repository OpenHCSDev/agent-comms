"""Long-running services a host runs on the installed code, each declared by its owner.

A service keeps the code it loaded when it started, so an install restarts every declared
service that is running on this host onto the installed runtime. A host that does not run a
service has no instance of it and contributes nothing: there is no list of services to keep.
"""

from __future__ import annotations

import json
import time
from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, ClassVar, Self

from ..child_process import ProcessIdentity, ResidentManager
from ..declared_family import DeclaredFamily
from ..field_codec import PathText
from ..launch_runtime import LaunchRuntime
from ..systemd_units import AgentsSlice, SystemdUnit


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
    def start_enabled(cls) -> None:
        """Start every service this host enables, each by its own name, so one the install
        found already dead serves again; a member nothing enables starts nothing."""
        for member in cls.members_with(cls):
            member.start_enabled_unit()

    @classmethod
    def start_enabled_unit(cls) -> None: ...

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


@dataclass(frozen=True)
class SystemdUserService(SystemdUnit, HostService, declared_name=None):
    """Systemd owns the unit's process; its definition starts that process again.

    Each member declares its readiness options. Systemd, not this configuration,
    owns the current PID; the member's serving proof checks the running service.
    """

    program: ClassVar[str]
    unit: ClassVar[str]
    service_options: ClassVar[tuple[str, ...]] = ("Type=notify", "NotifyAccess=main")

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
    def render_unit(cls, command: str) -> str:
        options = "\n".join(cls.service_options)
        return (
            f"[Unit]\nDescription={cls.unit}\n\n"
            f"[Service]\nSlice={AgentsSlice.unit}\n{options}\n"
            f"ExecStart={command}\nRestart=on-failure\n\n"
            "[Install]\nWantedBy=default.target\n"
        )

    @classmethod
    def unit_text(cls, arguments: tuple[str, ...] = ()) -> str:
        return cls.render_unit(cls.command_line(arguments))

    @classmethod
    def install(cls, arguments: tuple[str, ...] = (), *, home: Path) -> None:
        AgentsSlice.write_unit(home=home)
        cls.write_unit(arguments, home=home)
        ResidentManager.current().reload()

    def enable(self) -> None:
        ResidentManager.current().control("enable", self.unit_name())

    def retire(self) -> None:
        """Stop this instance and keep login and installs from starting it again."""
        ResidentManager.current().control("disable", "--now", self.unit_name())

    def restore(self) -> None:
        """Undo ``retire`` when the change it made room for did not commit."""
        ResidentManager.current().control("enable", "--now", self.unit_name())

    @classmethod
    def _pid_for(cls, name: str) -> int:
        return ResidentManager.current().main_pid(name, cls.unit_file(Path.home()))

    @classmethod
    def start_enabled_unit(cls) -> None:
        """``render_unit`` installs into ``default.target``, so enabling links it there."""
        manager = ResidentManager.current()
        enabled = cls.unit_file(Path.home()).parent / "default.target.wants" / cls.unit
        if enabled.exists() and manager.loads(cls.unit, cls.unit_file(Path.home())):
            manager.control("start", cls.unit)

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
        ResidentManager.current().control(
            "stop", *(instance.unit_name() for instance in instances))

    def start(self) -> None:
        ResidentManager.current().control("start", self.unit_name())
