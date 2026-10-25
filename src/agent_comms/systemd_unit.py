"""Systemd unit definitions and the resource slice shared by agent launches."""
from __future__ import annotations

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
    """Bound aggregate agent CPU time using the delegated CPU controller."""

    unit = "agents.slice"

    @classmethod
    def unit_text(cls, arguments: tuple[str, ...] = ()) -> str:
        return "[Slice]\nCPUQuota=1000%\nCPUWeight=50\n"

    @classmethod
    def run_prefix(cls) -> tuple[str, ...]:
        return ("systemd-run", "--user", "--quiet", "--collect", f"--slice={cls.unit}")
