"""One-shot B2 channel cutover. Run at the quiet install, then delete this tool."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field, fields
from pathlib import Path

from agent_comms.catalog_document import CatalogDocument, ChannelPreferences
from agent_comms.catalog_store import ChannelCatalog
from agent_comms.channels import AnyOfMatch, SavedView, ViewKind, ViewPredicate
from agent_comms.display_order import ChannelSort, ThreadSort
from agent_comms.field_codec import FieldCodec
from agent_comms.store_files import _store_lock


@dataclass(frozen=True, slots=True)
class AudienceCatalogMigration:
    """The retired saved document, consumed once; never a runtime catalog."""

    tags: frozenset[str] = frozenset()
    audiences: dict[str, frozenset[str]] = field(default_factory=dict)
    preferences: dict[str, ChannelPreferences] = field(default_factory=dict)
    saved_views: dict[str, SavedView] = field(default_factory=dict)
    list_order: ChannelSort = ChannelSort.NAME

    def migrate(self) -> CatalogDocument:
        tags = self.tags.union(*self.audiences.values())
        views = dict(self.saved_views)
        for target, members in self.audiences.items():
            name = target.removeprefix("#")
            if members == frozenset({name}):
                continue
            if name in tags or name in views:
                raise ValueError(
                    f"Saved audience {target!r} collides with an existing tag/view; "
                    "reconcile before migration."
                )
            preference = self.preferences.get(target, ChannelPreferences())
            views[name] = SavedView(
                name,
                ViewKind.ACTIVITY,
                ViewPredicate(AnyOfMatch, members),
                preference.created_at,
                frozenset({target}),
            )
        return CatalogDocument(tags, dict(self.preferences), views, self.list_order)


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
        return AudienceCatalogMigration(
            source.channels.tags,
            source.channels.channels,
            preferences,
            source.saved.views,
            source.channels.list_order,
        ).migrate()


def read_source(root: Path) -> CatalogDocument:
    path = root / ChannelCatalog.filename
    if path.exists():
        raw = json.loads(path.read_text())
        if "audiences" in raw:
            return FieldCodec.decode(AudienceCatalogMigration, raw).migrate()
        return FieldCodec.decode(CatalogDocument, raw)
    return CatalogMigration.read(root)


def cutover(root: Path) -> CatalogDocument:
    # Operator stops writers first; canonical locks preserve normal write order.
    # Neither the wire, its sequence, nor a historical source identity is rewritten.
    with _store_lock(root / "wire"):
        document = read_source(root)
        ChannelCatalog(root / ChannelCatalog.filename).replace(document)
        for path in CatalogMigration.paths(root):
            path.unlink(missing_ok=True)
    return document


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="+", type=Path)
    parser.add_argument("--apply", action="store_true", help="Publish after owners are quiet")
    args = parser.parse_args()
    for root in args.roots:
        document = cutover(root) if args.apply else read_source(root)
        print(
            json.dumps(
                {
                    "root": str(root),
                    "applied": args.apply,
                    "preferences": len(document.preferences),
                    "views": len(document.saved_views),
                }
            )
        )


if __name__ == "__main__":
    main()
