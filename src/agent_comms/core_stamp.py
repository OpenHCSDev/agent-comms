"""Build identities shared by the isolated build hook and the running package."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


class CoreStamp(ABC):
    @abstractmethod
    def commit(self) -> str: ...

    def write(self, path: Path) -> None:
        """Generate one importable value using its declared concrete type."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"from agent_comms.core_stamp import {type(self).__name__}\n"
            f"STAMP = {self!r}\n",
            encoding="utf-8",
        )


@dataclass(frozen=True)
class WheelStamp(CoreStamp):
    value: str

    def commit(self) -> str:
        return self.value


@dataclass(frozen=True)
class CheckoutStamp(CoreStamp):
    value: str

    def commit(self) -> str:
        # Imported here: hatch_build.py loads this module with run_path in a build env
        # holding only hatchling, where Repo's dependencies do not exist.
        from agent_comms.repo import Repo

        return Repo.git(self.value, "rev-parse", "HEAD").strip()
