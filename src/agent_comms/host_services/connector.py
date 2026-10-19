"""The claude.ai connector, when this host serves it."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Self

from ..connector import PORT, ServiceProcess
from . import SystemdUserService


@dataclass(frozen=True)
class ConnectorService(SystemdUserService):
    process: ServiceProcess

    @classmethod
    def running(cls) -> tuple[Self, ...]:
        return tuple(map(cls, ServiceProcess.listeners(PORT)))

    def unit(self) -> str:
        return self.process.unit

    def runtime(self) -> Path:
        return self.process.executable
