"""The uv this repository declares: its dev extra's pin, read alike by CI and the installer.

Run by path, before anything is installed, it prints that requirement so a workflow installs
exactly it; it therefore imports only the standard library and ``packaging``."""

from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


@dataclass(frozen=True)
class DeclaredUv:
    requirement: Requirement

    installed: ClassVar[Path] = Path(".local/state/agent-comms/upgrade-bus/uv")
    """Where in a home the installer keeps the uv it runs."""

    @classmethod
    def of(cls, pyproject: Path, executable: Path) -> DeclaredUv:
        """The dev extra's requirement for the distribution named like ``executable``."""
        match tomllib.loads(pyproject.read_text()):
            case {"project": {"optional-dependencies": {"dev": list(dev)}}}:
                (requirement,) = (
                    parsed
                    for parsed in map(Requirement, dev)
                    if canonicalize_name(parsed.name) == canonicalize_name(executable.name)
                )
                return cls(requirement)
        raise ValueError(f"{pyproject} declares no dev extra to pin uv")

    def accepts(self, reported: str) -> bool:
        """``reported`` is what ``uv --version`` prints: ``uv X.Y.Z (target)``."""
        return self.requirement.specifier.contains(reported.split()[1])


if __name__ == "__main__":
    print(DeclaredUv.of(Path(sys.argv[1]), DeclaredUv.installed).requirement)
