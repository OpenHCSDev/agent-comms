"""One A8 catalog document; historical four-file data is a one-way input."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import ClassVar

from .catalog_document import CatalogDocument, ChannelPreferences
from .channels import SavedView
from .display_order import ChannelSort, ThreadSort
from .field_codec import FieldCodec
from .locked_store import LockedStore
from .store_files import file_revision


@dataclass(frozen=True, slots=True)
class StoredChannels:
    tags: frozenset[str] = frozenset()
    channels: dict[str, frozenset[str]] = field(default_factory=dict)
    orders: dict[str, ThreadSort] = field(default_factory=dict)
    created_at: dict[str, float] = field(default_factory=dict)
    list_order: ChannelSort = ChannelSort.NAME


@dataclass(frozen=True, slots=True)
class StoredPins:
    channels: frozenset[str] = frozenset()
    threads: dict[str, frozenset[str]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StoredMetadata:
    parent: str | None = None
    archived: bool = False
    any_mode: bool = False


@dataclass(frozen=True, slots=True)
class StoredChannelMetadata:
    channels: dict[str, StoredMetadata] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StoredViews:
    views: dict[str, SavedView] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CatalogMigration:
    """Declarations describe actual retained file formats, not old internal APIs."""

    channels: StoredChannels = field(metadata={"filename": "channels.json"})
    pins: StoredPins = field(metadata={"filename": "channel_pins.json"})
    metadata: StoredChannelMetadata = field(metadata={"filename": "channel_metadata.json"})
    saved: StoredViews = field(metadata={"filename": "saved_views.json"})

    @classmethod
    def paths(cls, root: Path) -> tuple[Path, ...]:
        return tuple(root / item.metadata["filename"] for item in fields(cls))

    @classmethod
    def read(cls, root: Path) -> CatalogDocument:
        data = {}
        for item, path in zip(fields(cls), cls.paths(root), strict=True):
            try:
                data[item.name] = json.loads(path.read_text())
            except FileNotFoundError:
                data[item.name] = {}
        # The old map key owns view identity; missing dates remain unknown.
        saved = data["saved"]
        if "views" in saved:
            saved["views"] = {
                name: dict(name=name, **{"created_at": 0.0, **value})
                for name, value in saved["views"].items()
            }
        source = FieldCodec.decode(cls, data)
        preferences = {}
        names = (
            set(source.channels.orders)
            | set(source.channels.created_at)
            | set(source.channels.channels)
            | source.pins.channels
            | set(source.pins.threads)
            | set(source.metadata.channels)
        )
        for name in names:
            metadata = source.metadata.channels.get(name, StoredMetadata())
            preferences[name] = ChannelPreferences(
                order=source.channels.orders.get(name, ThreadSort.CREATED),
                created_at=source.channels.created_at.get(name, 0.0),
                pinned=name in source.pins.channels,
                pinned_threads=source.pins.threads.get(name, frozenset()),
                parent=metadata.parent,
                archived=metadata.archived,
                any_mode=metadata.any_mode,
            )
        return CatalogDocument(
            source.channels.tags,
            source.channels.channels,
            preferences,
            source.saved.views,
            source.channels.list_order,
        )


@dataclass(frozen=True, slots=True)
class ChannelCatalog(LockedStore[CatalogDocument]):
    filename: ClassVar[str] = "catalog.json"
    json_indent = 2

    @property
    def record_type(self) -> type[CatalogDocument]:
        return CatalogDocument

    def empty(self) -> CatalogDocument:
        return CatalogMigration.read(self.path.parent)

    def source_paths(self) -> tuple[Path, ...]:
        """After the atomic migration, old files no longer participate in reads."""
        return (
            (self.path,)
            if self.path.exists()
            else (self.path, *CatalogMigration.paths(self.path.parent))
        )

    def revision(self) -> tuple:
        return tuple(file_revision(path) for path in self.source_paths())

    @contextmanager
    def editing(self) -> Iterator[CatalogDocument]:
        """Caller holds wire; publish all declarations/preferences in one replace.

        Historical inputs remain untouched. A canonical document supersedes them
        permanently; there are no sidecar writes, old-writer merges or caches.
        """
        with self.locked():
            original = self._read_unlocked()
            document = deepcopy(original)
            yield document
            if document != original or not self.path.exists():
                document.validate()
                self._write_unlocked(json.dumps(self._encode(document), indent=self.json_indent))
