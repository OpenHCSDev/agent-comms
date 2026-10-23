"""Every active Codex inbox instance participates in the host's install lifecycle."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Self

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
        if not cls.unit_file().is_file():
            return ()
        prefix = cls.unit.removesuffix(".service")
        listed = subprocess.check_output(
            (
                "systemctl",
                "--user",
                "list-units",
                prefix + "*",
                "--state=active",
                "--plain",
                "--no-legend",
            ),
            text=True,
        )
        instances = tuple(
            cls(line.split()[0].removeprefix(prefix).removesuffix(".service"))
            for line in listed.splitlines()
            if line.strip()
        )
        return tuple(instance for instance in instances if instance.pid() != 0)

    def serves(self) -> None:
        state = subprocess.run(
            ("systemctl", "--user", "is-active", self.unit_name()),
            check=False,
            capture_output=True,
            text=True,
        )
        if state.returncode != 0 or state.stdout.strip() != "active":
            raise ServiceNotServingError(f"{self.described()} is not active")
        runtime = self.runtime()
        installed = LaunchRuntime.installed().interpreter
        if runtime != installed:
            raise ServiceNotServingError(
                f"{self.described()} runs {runtime}, not the installed {installed}"
            )

    def described(self) -> str:
        return f"Codex inbox for {self.session}"
