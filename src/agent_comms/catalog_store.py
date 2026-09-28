"""The current channel catalog document and its canonical transaction lock."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from typing import ClassVar

from .catalog_document import CatalogDocument
from .locked_store import LockedStore
from .store_files import file_revision


@dataclass(frozen=True, slots=True)
class ChannelCatalog(LockedStore[CatalogDocument]):
    filename: ClassVar[str] = "catalog.json"
    json_indent = 2

    @property
    def record_type(self) -> type[CatalogDocument]:
        return CatalogDocument

    def empty(self) -> CatalogDocument:
        return CatalogDocument()

    def revision(self) -> tuple:
        return (file_revision(self.path),)

    @contextmanager
    def editing(self) -> Iterator[CatalogDocument]:
        """Caller holds wire; publish all declarations/preferences in one replace."""
        with self.locked():
            original = self._read_unlocked()
            document = deepcopy(original)
            yield document
            if document != original or not self.path.exists():
                document.validate()
                self._publish_unlocked(document)
