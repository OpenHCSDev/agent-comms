"""The runtime socket owns its pathname and borrowed native address."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Annotated

from .field_codec import PathText
from .private_path import PrivateSocketRole


@dataclass(frozen=True)
class RuntimeSocket:
    path: Annotated[Path, PathText]

    @staticmethod
    def path_for(root: Path, pid: int) -> Path:
        return root / "runtime" / f"{pid}.sock"

    @contextmanager
    def bound(self):
        with PrivateSocketRole.address(self.path) as address:
            yield replace(self, path=address)
