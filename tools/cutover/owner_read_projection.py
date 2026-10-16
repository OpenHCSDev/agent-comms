"""Current-format acquired owner and recorded-source reads.

Both sides of this protocol use the current shape. There is no alternate
shared reader for retired records.
"""
from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec
from agent_comms.registry_document import RegistryDocument
from agent_comms.owner_launch import OwnerRestartSelection

if TYPE_CHECKING:
    from recorded_owner_read import RecordedOwnerRead


@dataclass(frozen=True)
class OriginalOwnerRead:
    """Current source values retain their declarations through stdio and decoding."""

    document: RegistryDocument
    selection: OwnerRestartSelection


class OwnerReadProjection(DeclaredFamily, affix='Projection'):
    @classmethod
    @abstractmethod
    def read(cls, registry, root: Path, name: str,
             request) -> OriginalOwnerRead | RecordedOwnerRead:
        """Acquire evidence using the current declarations on both ends."""


class CurrentThreadProjection(OwnerReadProjection):
    @classmethod
    def read(cls, registry, root: Path, name: str, request) -> OriginalOwnerRead:
        with registry.store.reading() as document:
            snapshot = document.snapshot()
            selection = (
                FieldCodec.decode(OwnerRestartSelection, request)
                if request is not None else OwnerRestartSelection.capture(snapshot, name)
            )
            selection.require_current(snapshot)
            return OriginalOwnerRead(document, selection)


class RecordedRegistryProjection(OwnerReadProjection):
    @classmethod
    def read(cls, registry, root: Path, name: str, request) -> RecordedOwnerRead:
        from recorded_owner_read import RecordedOwnerRead

        with registry.store.reading() as document:
            return RecordedOwnerRead.capture(root, registry.store.path, document, request)
