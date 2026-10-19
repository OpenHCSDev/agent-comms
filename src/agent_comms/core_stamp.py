"""Build identities shared by the isolated build hook and the running package."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
import subprocess


class CoreStamp(ABC):
    @abstractmethod
    def commit(self) -> str: ...

    @abstractmethod
    def resource(self, checkout: str, packaged: str) -> Path:
        """A resource a checkout keeps at ``checkout`` and a wheel carries as ``packaged``."""

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

    def resource(self, checkout: str, packaged: str) -> Path:
        return Path(__file__).resolve().parent / packaged


@dataclass(frozen=True)
class CheckoutStamp(CoreStamp):
    value: str

    def commit(self) -> str:
        return subprocess.check_output(
            ("git", "rev-parse", "HEAD"), cwd=self.value, text=True,
        ).strip()

    def resource(self, checkout: str, packaged: str) -> Path:
        return Path(self.value) / checkout
