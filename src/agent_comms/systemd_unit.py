"""Systemd unit definitions and the resource slice shared by agent launches."""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import ClassVar


class SystemdUnit(ABC):
    """A user unit declares the definition the resident manager installs."""

    unit: ClassVar[str]

    @classmethod
    def unit_file(cls, home: Path) -> Path:
        return home / ".config/systemd/user" / cls.unit

    @classmethod
    def required_units(cls) -> tuple[type[SystemdUnit], ...]:
        return ()

    @classmethod
    @abstractmethod
    def unit_text(cls, arguments: tuple[str, ...]) -> str: ...


class AgentsSlice(SystemdUnit):
    """Keep agent processes off the desktop's reserved CPUs."""

    unit = "agents.slice"
    reserved: ClassVar[int] = 2

    @classmethod
    def unit_text(cls, arguments: tuple[str, ...] = ()) -> str:
        count = os.cpu_count()
        if count is None:
            raise RuntimeError("Cannot reserve desktop CPUs: machine CPU count is unknown.")
        return f"[Slice]\nAllowedCPUs={cls.reserved}-{count - 1}\nCPUWeight=50\n"

    @classmethod
    def run_prefix(cls) -> tuple[str, ...]:
        return ("systemd-run", "--user", "--quiet", "--collect", f"--slice={cls.unit}")
