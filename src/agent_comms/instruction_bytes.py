"""One original instruction read, before rendering or provenance projection."""

from dataclasses import dataclass
from pathlib import Path
from typing import Self


@dataclass(frozen=True)
class InstructionBytes:
    path: Path
    raw: bytes

    @classmethod
    def read(cls, name: str) -> Self:
        path = Path(__file__).with_name("instructions") / name
        return cls(path, path.read_bytes())

    @property
    def text(self) -> str:
        return self.raw.decode()
