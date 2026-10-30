"""Build identities shared by the isolated build hook and the running package."""

import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CoreStamp:
    """A source whose commit is known: its producer declarations are evaluated on request."""

    value: str

    def commit(self) -> str:
        return self.value

    def producer_digests(
        self,
        declared: Mapping[str, Callable[[], str]],
    ) -> dict[str, str]:
        """Available digests; a source stamp resolves the requested declarations.

        A built stamp can return additional precomputed rows. Consumers select
        their own rows from the result.
        """
        return {name: digest() for name, digest in declared.items()}

    def write(self, path: Path) -> None:
        """Generate one importable value using its declared concrete type."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"from {type(self).__module__} import {type(self).__name__}\nSTAMP = {self!r}\n",
            encoding="utf-8",
        )


@dataclass(frozen=True)
class WheelStamp(CoreStamp):
    digests: dict[str, str]

    @classmethod
    def of_source(
        cls, src: Path, commit: str, *, source_paths: tuple[str, ...] = ()
    ) -> WheelStamp:
        """Import the bus's declared schema once while building its immutable wheel.

        The custom hook requires the project's runtime dependencies in pyproject.toml;
        hatchling alone cannot import the application in an isolated build.
        The child imports from the source root, then the caller's revision import context
        (an archived revision's pinned audit scripts), then the inherited import path. It is
        given the known commit, so a source with no Git checkout still states its identity.
        """
        path = os.pathsep.join(
            dict.fromkeys(filter(None, (str(src), *source_paths, os.environ.get("PYTHONPATH"))))
        )
        return cls(
            commit,
            json.loads(
                subprocess.check_output(
                    (sys.executable, "-m", "agent_comms.producer_stamp", commit),
                    cwd=src.parent,
                    env={**os.environ, "PYTHONPATH": path},
                    text=True,
                )
            ),
        )

    def producer_digests(
        self,
        declared: Mapping[str, Callable[[], str]],
    ) -> dict[str, str]:
        return self.digests
