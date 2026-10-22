"""A GitHub repository identity, with its existing owner/name wire form."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import ClassVar

from .field_codec import MalformedRecord, WireValue


@dataclass(frozen=True, slots=True, eq=False)
class RepositoryName(WireValue):
    owner: str
    name: str
    _part: ClassVar[re.Pattern[str]] = re.compile(r"(?!\.{1,2}$)[\w.-]+\Z")

    def __post_init__(self) -> None:
        if len(self.to_wire()) > 200 or not all(self._part.fullmatch(part) for part in self.key):
            raise ValueError("Repo ID must have the exact owner/repo form.")

    @property
    def key(self) -> tuple[str, str]:
        return self.owner.casefold(), self.name.casefold()

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RepositoryName):
            return NotImplemented
        return self.key == other.key

    def __hash__(self) -> int:
        return hash(self.key)

    def named(self, name: str) -> bool:
        return self.name.casefold() == name.casefold()

    def to_wire(self) -> str:
        return "/".join(self.key)

    def api(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        """The one GitHub reader: ``gh api`` with ``arguments``; callers judge the result."""
        return subprocess.run(("gh", "api", *arguments), capture_output=True, text=True, timeout=60)

    def rest(self, path: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        """``path`` under this repository's REST root."""
        return self.api(f"repos/{self.to_wire()}/{path}", *arguments)

    @classmethod
    def from_wire(cls, data: object) -> RepositoryName:
        if not isinstance(data, str):
            raise MalformedRecord("Repo ID must be an owner/repo string.")
        owner, _, name = data.partition("/")
        return cls(owner, name)

    @classmethod
    def schema(cls) -> dict[str, object]:
        return {"type": "string", "description": "GitHub repository owner/name"}
