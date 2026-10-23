"""Build identities shared by the isolated build hook and the running package."""

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path


class CoreStamp(ABC):
    @abstractmethod
    def commit(self) -> str: ...

    @abstractmethod
    def producer_digests(
        self, declared: Mapping[str, Callable[[], str]],
    ) -> dict[str, str]: ...

    def write(self, path: Path) -> None:
        """Generate one importable value using its declared concrete type."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"from {type(self).__module__} import {type(self).__name__}\nSTAMP = {self!r}\n",
            encoding="utf-8",
        )


@dataclass(frozen=True)
class WheelStamp(CoreStamp):
    value: str
    digests: dict[str, str]

    def commit(self) -> str:
        return self.value

    def producer_digests(
        self, declared: Mapping[str, Callable[[], str]],
    ) -> dict[str, str]:
        return self.digests
