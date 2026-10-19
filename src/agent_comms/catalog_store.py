"""The current channel catalog document and its canonical transaction lock."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from .catalog_document import CatalogDocument
from .locked_store import LockedStore, RootFile
from .private_path import FileRevision
from .thread_owned_state import ThreadOwnedState

if TYPE_CHECKING:
    from .threads import Thread


@dataclass(frozen=True, slots=True)
class ChannelCatalog(RootFile, ThreadOwnedState, LockedStore[CatalogDocument], lock_rank=3):
    filename: ClassVar[str] = "catalog.json"
    json_indent = 2

    record_type = CatalogDocument

    def empty(self) -> CatalogDocument:
        return CatalogDocument()

    def revision(self) -> tuple[FileRevision | None]:
        return (FileRevision.capture(self.path),)

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

    def remove_threads(self, threads: Sequence[Thread]) -> None:
        """Unpin deleted threads from every channel preference."""
        with self.editing() as document:
            for thread in threads:
                document.remove_thread(thread.name)
