"""User unit definitions shared by the resident launcher and installed services."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import ClassVar


class SystemdUnit(ABC):
    """A unit owns its filename, rendered definition and file publication."""

    unit: ClassVar[str]

    @classmethod
    def unit_file(cls, home: Path) -> Path:
        return home / ".config/systemd/user" / cls.unit

    @classmethod
    @abstractmethod
    def unit_text(cls, arguments: tuple[str, ...] = ()) -> str: ...

    @classmethod
    def write_unit(cls, arguments: tuple[str, ...] = (), *, home: Path) -> None:
        unit = cls.unit_file(home)
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(cls.unit_text(arguments), encoding="utf-8")


class AgentsSlice(SystemdUnit):
    """Agent residents and their descendants leave two CPUs for the owner."""

    unit = "agents.slice"
    allowed_cpus: ClassVar[str] = "2-11"
    cpu_weight: ClassVar[int] = 50

    @classmethod
    def launch_option(cls) -> str:
        return f"--slice={cls.unit}"

    @classmethod
    def unit_text(cls, arguments: tuple[str, ...] = ()) -> str:
        return (
            "[Unit]\nDescription=Agent processes\n\n"
            f"[Slice]\nAllowedCPUs={cls.allowed_cpus}\nCPUWeight={cls.cpu_weight}\n"
        )
