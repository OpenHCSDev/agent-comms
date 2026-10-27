"""The current channel catalog document and its canonical transaction lock."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from .catalog_document import CatalogDocument
from .locked_store import DocumentChange
from .maintenance_barrier import CarriedDocument
from .private_path import FileObservation, FileRevision
from .store_files import CatalogTier, SingleFileStore
from .thread_owned_state import ThreadOwnedState

if TYPE_CHECKING:
    from .activity_clocks import ActivityClocks
    from .threads import Thread


@dataclass(frozen=True, slots=True)
class ChannelCatalog(SingleFileStore, ThreadOwnedState, CarriedDocument[CatalogDocument], CatalogTier):
    filename: ClassVar[str] = "catalog.json"
    json_indent = 2

    record_type = CatalogDocument

    def empty(self) -> CatalogDocument:
        return CatalogDocument()

    def revision(self) -> tuple[FileObservation]:
        return (FileRevision.capture(self.path),)

    def change[R](self, mutation: Callable[[CatalogDocument], R]) -> R:
        """Copy and validate one catalog mutation through the shared document transaction."""
        def apply(original: CatalogDocument) -> DocumentChange[CatalogDocument, R]:
            document = deepcopy(original)
            result = mutation(document)
            if document != original or not self.path.exists():
                document.validate()
                return DocumentChange(document, result)
            return DocumentChange(original, result)

        return self.update(apply)

    def remove_threads(self, threads: Sequence[Thread]) -> None:
        """Unpin deleted threads from every channel preference."""
        def remove(document: CatalogDocument) -> None:
            for thread in threads:
                document.remove_thread(thread.name)
        self.change(remove)

    def restore_missing(self, source: CatalogDocument, activity: ActivityClocks) -> tuple[str, ...]:
        """The current transaction owns whether destination preferences already existed."""
        return self.change(lambda document: document.restore_missing(
            source, activity, existing=self.path.exists()))
