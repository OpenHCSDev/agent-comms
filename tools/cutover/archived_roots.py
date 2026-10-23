"""The archive catalogue owns retained-root mapping for every carry."""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Mapping
from pathlib import Path
from typing import Self

from agent_comms.field_codec import FieldCodec, SubclassUnion
from agent_comms.historical_views import HistoryArchive, HistorySource
from agent_comms.installer.probes.carry import ArchivedInputs, Carried, Carry
from agent_comms.recorded_bus import RecordedBusIdentity
from agent_comms.store_files import AtomicFile


class CarryCatalogSource(SubclassUnion, ABC, affix=""):
    """Decode the archive's stored state once, including an already converted source."""

    root: str

    @property
    @abstractmethod
    def pending(self) -> int: ...

    @abstractmethod
    def require_snapshot(self) -> None: ...

    @abstractmethod
    def carried_registry(self, mapped: Path) -> Self:
        """Reseal a registry carried before the bus format changes."""

    @abstractmethod
    def converted(
        self,
        root: Path,
        original_root: Path,
        bindings: Mapping[RecordedBusIdentity, RecordedBusIdentity],
    ) -> tuple[HistorySource, Carried]: ...

    @classmethod
    def read(cls, root: Path, original_root: Path) -> tuple[CarryCatalogSource, ...]:
        path = HistoryArchive.path_in(root)
        if not path.exists():
            return ()
        sources = FieldCodec.decode(
            tuple[CarryCatalogSource, ...],
            FieldCodec.parse(path.read_bytes()),
        )
        for source in sources:
            source.mapped_root(root, original_root)
            source.require_snapshot()
        return sources

    @classmethod
    def publish(
        cls,
        root: Path,
        original_root: Path,
        sources: tuple[CarryCatalogSource, ...],
        bindings: Mapping[RecordedBusIdentity, RecordedBusIdentity],
    ) -> Carried:
        path = HistoryArchive.path_in(root)
        if not path.exists():
            return Carried(0)
        results = tuple(source.converted(root, original_root, bindings) for source in sources)
        converted = tuple(source for source, _ in results)
        changed = sum(result.changed for _, result in results)
        rewritten = tuple(name for _, result in results for name in result.rewritten)
        # BusHistory holds the catalog lock from the old read through this publication.
        payload = json.dumps(FieldCodec.encode(converted))
        if path.read_bytes() == payload.encode():
            return Carried(changed, rewritten=rewritten)
        AtomicFile(path).write_text(payload)
        return Carried(changed + len(converted), rewritten=(*rewritten, path.relative_to(root)))

    def mapped_root(self, root: Path, original_root: Path) -> Path:
        original = Path(self.root)
        if original.is_relative_to(root):
            Carry.require_confined(root, original)
            return original
        Carry.require_confined(original_root, original)
        mapped = root / original.relative_to(original_root)
        Carry.require_confined(root, mapped)
        return mapped


class ArchivedCarry(ArchivedInputs, Carry):
    """The catalogue owns how retained roots map into this installation's copy."""

    def roots(self) -> tuple[Path, ...]:
        return tuple(dict.fromkeys((
            self.root,
            *(source.mapped_root(self.root, self.original_root)
              for source in CarryCatalogSource.read(self.root, self.original_root)),
        )))
