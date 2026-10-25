"""Every active Codex inbox instance participates in the host's install lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Self

from ..child_process import ResidentManager
from ..launch_runtime import LaunchRuntime
from . import ServiceNotServingError, SystemdUserService


@dataclass(frozen=True)
class CodexInboxService(SystemdUserService):
    session: str

    program = "agent-comms-codex-bridge"
    unit = "codex-comms-inbox@.service"
    service_options = (
        "Type=simple",
        "RestartSec=5",
        "KillMode=control-group",
        "TimeoutStopSec=10",
        "UMask=0077",
    )

    @classmethod
    def unit_text(cls, arguments: tuple[str, ...]) -> str:
        command = cls.command_line(("watch", *arguments)) + ' --session "%i"'
        return cls.render_unit(command)

    def unit_name(self) -> str:
        return self.unit.replace("@.", f"@{self.session}.")

    @classmethod
    def running(cls) -> tuple[Self, ...]:
        if not cls.unit_file(Path.home()).is_file():
            return ()
        prefix = cls.unit.removesuffix(".service")
        instances = tuple(
            cls(name.removeprefix(prefix).removesuffix(".service"))
            for name in ResidentManager.current().listed(prefix + "*")
        )
        return tuple(instance for instance in instances if instance.pid() != 0)

    def serves(self) -> None:
        if not ResidentManager.current().activity(self.unit_name()).serving():
            raise ServiceNotServingError(f"{self.described()} is not active")
        runtime = self.runtime()
        installed = LaunchRuntime.installed().interpreter
        if runtime != installed:
            raise ServiceNotServingError(
                f"{self.described()} runs {runtime}, not the installed {installed}"
            )

    def described(self) -> str:
        return f"Codex inbox for {self.session}"
