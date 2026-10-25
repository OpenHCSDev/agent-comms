"""Systemd unit definitions and the resource slice shared by agent launches."""
from __future__ import annotations

import multiprocessing
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
    """Cap aggregate agent CPU time, leaving a desktop CPU-time budget."""

    unit = "agents.slice"
    reserved: ClassVar[int] = 2

    @classmethod
    def unit_text(cls, arguments: tuple[str, ...] = ()) -> str:
        count = multiprocessing.cpu_count()
        return f"[Slice]\nCPUQuota={(count - cls.reserved) * 100}%\nCPUWeight=50\n"

    @classmethod
    def run_prefix(cls) -> tuple[str, ...]:
        return ("systemd-run", "--user", "--quiet", "--collect", f"--slice={cls.unit}")
