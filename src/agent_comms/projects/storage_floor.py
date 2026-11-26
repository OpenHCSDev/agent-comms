"""Disk headroom floor shared by StorageRule and lightweight resource checks.

Keeping this leaf free of registry/execution imports lets ``agent-comms-resources scan``
read ``storage.json`` without pulling in the native Pi and thread stacks.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar


@dataclass(frozen=True)
class StorageFloor:
    """The configured ``/home`` free-space floor (GiB), defaulting to 10 GiB."""

    filename: ClassVar[str] = "storage.json"
    floor_gib: float = 10.0

    @classmethod
    def read_floor_gib(cls, root: Path) -> float:
        path = root / cls.filename
        if not path.exists():
            return cls.floor_gib
        raw = json.loads(path.read_text())
        value = raw.get("floor_gib", cls.floor_gib) if isinstance(raw, dict) else cls.floor_gib
        floor = float(value)
        if not math.isfinite(floor) or floor < 0:
            raise ValueError("Storage limits must be finite and nonnegative.")
        return floor
