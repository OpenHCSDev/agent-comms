"""Build identities shared by the isolated build hook and the running package."""

import json
import os
import subprocess
import sys
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path


class CoreStamp(ABC):
    @abstractmethod
    def commit(self) -> str: ...

    @abstractmethod
    def producer_digests(
        self,
        declared: Mapping[str, Callable[[], str]],
    ) -> dict[str, str]:
        """Available digests; a source stamp resolves the requested declarations.

        A built stamp can return additional precomputed rows. Consumers select
        their own rows from the result.
        """

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

    @classmethod
    def of_source(cls, src: Path, commit: str) -> WheelStamp:
        """Import the bus's declared schema once while building its immutable wheel.

        The custom hook requires the project's runtime dependencies in pyproject.toml;
        hatchling alone cannot import the application in an isolated build.
        """
        return cls(
            commit,
            json.loads(
                subprocess.check_output(
                    (sys.executable, "-m", "agent_comms.producer_stamp"),
                    cwd=src.parent,
                    env={**os.environ, "PYTHONPATH": str(src)},
                    text=True,
                )
            ),
        )

    def commit(self) -> str:
        return self.value

    def producer_digests(
        self,
        declared: Mapping[str, Callable[[], str]],
    ) -> dict[str, str]:
        return self.digests
