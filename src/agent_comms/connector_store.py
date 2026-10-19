"""Connector grant storage, independent of its HTTP transport and Comms root."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from .locked_store import LockedStore, RootFile

if TYPE_CHECKING:
    from .connector import ConnectorGrants


@dataclass(frozen=True, slots=True)
class ConnectorStore(RootFile, LockedStore["ConnectorGrants"]):
    filename: ClassVar[str] = "grants.json"

    @classmethod
    def owned(cls) -> ConnectorStore:
        directory = Path.home() / ".local/state/agent-comms/connector"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        return cls(cls.path_in(directory))

    @property
    def record_type(self) -> type[ConnectorGrants]:
        from .connector import ConnectorGrants

        return ConnectorGrants

    def empty(self) -> ConnectorGrants:
        return self.record_type()
