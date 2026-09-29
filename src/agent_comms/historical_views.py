"""Source-bound historical display values. These carry no delivery authority."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, fields, replace
from pathlib import Path

from .channels import Channel
from .field_codec import FieldCodec
from .message_page import MessagePage, MessagePageRequest, PageTraversal
from .messages import Message
from .private_registry_guard import PrivateRegistryGuard
from .read_basis import ChannelDisplayScope, DisplayBasis, DMDisplayScope, MessageDisplayScope
from .registration import Registration
from .registry_document import RegistrySnapshot
from .response_policy import InformationalPolicy, ResponsePolicy
from .store_files import _atomic_write_text, _iter_jsonl_records, _store_lock, file_revision
from .threads import Thread
from .wire_log import WireLog
from .wire_metadata import ArchivedAccess


class HistoryView(ABC):
    """A requested view binds its predicate once to each original registry."""

    @abstractmethod
    def capture(self, snapshot: RegistrySnapshot) -> MessageDisplayScope:
        """Retain the source's aliases/membership without borrowing live identity."""


@dataclass(frozen=True, slots=True)
class ChannelHistory(HistoryView):
    target: str
    targets: frozenset[str] | None

    def capture(self, snapshot: RegistrySnapshot) -> ChannelDisplayScope:
        return ChannelDisplayScope(self.target, self.targets)


@dataclass(frozen=True, slots=True)
class ChannelDisplayHistory(HistoryView):
    channel: Channel

    def capture(self, snapshot: RegistrySnapshot) -> ChannelDisplayScope:
        return ChannelDisplayScope.capture(self.channel, snapshot)


@dataclass(frozen=True, slots=True)
class DMHistory(HistoryView):
    first: str
    second: str

    def capture(self, snapshot: RegistrySnapshot) -> DMDisplayScope:
        return DMDisplayScope.capture(self.first, self.second, snapshot)


@dataclass(frozen=True, slots=True)
class HistoryCursor:
    source: str
    sequence: int


@dataclass(frozen=True, slots=True)
class HistorySource:
    """An immutable bus/registry snapshot attached by the destination MessageBus."""

    root: str
    original_root: str
    wire_root_id: str
    bus_identity: tuple[int, int]
    size: int
    snapshot_bus_revision: tuple[int, int, int, int]
    snapshot_registry_revision: tuple[int, int, int, int]

    @property
    def key(self) -> str:
        return self.root

    def validate(self) -> None:
        root = Path(self.root)
        if (
            file_revision(root / "bus.jsonl") != self.snapshot_bus_revision
            or file_revision(root / "registry.json") != self.snapshot_registry_revision
        ):
            raise ValueError("Historical snapshot changed; restore it before browsing")

    def registry(self) -> Registration:
        self.validate()
        return Registration(Path(self.root) / "registry.json")


@dataclass(frozen=True, slots=True, kw_only=True)
class HistoricalMessage(Message):
    source: HistorySource
    source_order: int
    sender_created_at: float | None
    target_created_at: float | None

    @classmethod
    def project(
        cls, message: Message, source: HistorySource, order: int, snapshot: RegistrySnapshot
    ) -> HistoricalMessage:
        def creation(name: str) -> float | None:
            declaration = snapshot.threads.get(snapshot.aliases.get(name, name))
            # A later source declaration cannot certify a row from an earlier
            # incarnation. The original registry remains available for inspection.
            return (
                declaration.created_at
                if declaration is not None and declaration.created_at <= message.timestamp
                else None
            )

        return cls(
            **{f.name: getattr(message, f.name) for f in fields(Message)},
            source=source,
            source_order=order,
            sender_created_at=creation(message.sender),
            target_created_at=creation(message.target),
        )

    @property
    def response_policy(self) -> ResponsePolicy:
        return InformationalPolicy.instance()

    @property
    def view_cursor(self) -> HistoryCursor:
        return HistoryCursor(self.source.key, self.seq)

    @property
    def view_key(self) -> tuple[str, int]:
        return self.source.key, self.seq

    @property
    def view_order(self) -> tuple[int, int, int]:
        return 0, self.source_order, self.seq

    @property
    def display_metadata(self) -> dict:
        return {
            "history": {
                "source": self.source.original_root,
                "wire_root_id": self.source.wire_root_id,
                "bus_identity": self.source.bus_identity,
                "sender_created_at": self.sender_created_at,
                "target_created_at": self.target_created_at,
            },
        }

    def to_wire(self) -> dict:
        # Provenance is a view, never a change to the original public message.
        return Message(**{f.name: getattr(self, f.name) for f in fields(Message)}).to_wire()


@dataclass(frozen=True, slots=True)
class HistoricalThread:
    source: HistorySource
    thread: Thread


@dataclass(frozen=True, slots=True)
class HistoricalDisplay:
    source: HistorySource
    displayed: DisplayBasis

    @property
    def viewer(self) -> str:
        return self.displayed.viewer

    @property
    def viewer_created_at(self) -> float:
        return self.displayed.viewer_created_at

    def select(self, sequences) -> HistoricalDisplay:
        return HistoricalDisplay(self.source, self.displayed.select(sequences))


class HistoryArchive:
    """Ordered immutable source snapshots; never a delivery/launch authority."""

    filename = "history_sources.json"

    def __init__(self, bus_path: Path):
        self.bus_path = bus_path
        self.path = bus_path.with_name(self.filename)

    def sources(self) -> tuple[HistorySource, ...]:
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return ()
        return FieldCodec.decode(tuple[HistorySource, ...], raw)

    def attach(self, source_root: Path) -> HistorySource:
        """Snapshot a preserved source, then publish it for ordinary display.

        Only destination files are written. No Comms constructor, source locks,
        inboxes, execution inputs, or source sequence allocator are touched.
        A source is attached once. Its original bytes and identity survive.
        """
        import shutil
        import tempfile

        from .catalog_store import ChannelCatalog
        from .historical_views import HistorySource
        from .transcript_routes import TranscriptRoutes

        source_root = source_root.resolve()
        if source_root == self.bus_path.parent.resolve():
            raise ValueError("The live bus cannot be its own history source")
        with _store_lock(self.path):
            sources = self.sources()
            existing = next((s for s in sources if s.original_root == str(source_root)), None)
            if existing is not None:
                return existing
            parent = self.bus_path.parent / "history"
            parent.mkdir(mode=0o700, exist_ok=True)
            stage = Path(tempfile.mkdtemp(prefix="source-", dir=parent))
            try:
                paths = [
                    source_root / name
                    for name in (
                        "bus.jsonl",
                        "registry.json",
                        ChannelCatalog.filename,
                        "bus_meta.json",
                    )
                ]
                revisions = tuple(file_revision(path) for path in paths)
                for path in paths:
                    if path.exists():
                        shutil.copy2(path, stage / path.name)
                TranscriptRoutes(source_root).snapshot(stage)
                if revisions != tuple(file_revision(path) for path in paths):
                    raise ValueError("Historical source changed during snapshot; retry")
                bus_info = paths[0].stat() if paths[0].exists() else None
                if bus_info is None:
                    (stage / "bus.jsonl").touch()
                # One current marker format; snapshots retain source identity
                # and explicitly prohibit publication or historical admission.
                archived = WireLog(stage / "bus.jsonl")
                marker = archived.read_metadata_unlocked(required=True)
                marker.admission_after_seq = marker.last_seq
                marker.access = ArchivedAccess()
                marker.checkpoint_version = None
                marker.checkpoint_seal = None
                guard = PrivateRegistryGuard(stage / "registry.json", marker.root_id)
                guard.create_pending()
                archived.write_metadata_unlocked(marker)
                guard.commit_initial()
                source = HistorySource(
                    str(stage.resolve()),
                    str(source_root),
                    marker.root_id,
                    (bus_info.st_dev, bus_info.st_ino) if bus_info else (0, 0),
                    bus_info.st_size if bus_info else 0,
                    file_revision(stage / "bus.jsonl"),
                    file_revision(stage / "registry.json"),
                )
                declarations = source.registry().all_threads()
                previous = 0
                for record, size in _iter_jsonl_records(stage / "bus.jsonl"):
                    message, _ = archived._public_page_record(record, size, marker)
                    if message.seq <= previous:
                        raise ValueError("Historical source has nonascending sequences")
                    previous = message.seq
                if not declarations:
                    raise ValueError("Historical source has no identity declarations")
                _atomic_write_text(
                    self.path,
                    json.dumps([FieldCodec.encode(item) for item in (*sources, source)]),
                )
                return source
            except BaseException:
                shutil.rmtree(stage)
                raise

    def page(self, view: HistoryView, *, before=None, after=None, limit=100,
             max_bytes=256 * 1024) -> MessagePage:
        traversal = PageTraversal.capture(
            before.sequence if before is not None else None,
            after.sequence if after is not None else None,
        )
        sources = self.sources()
        cursor = before or after
        start = next(
            (i for i, source in enumerate(sources) if cursor and source.key == cursor.source),
            len(sources) - 1 if cursor is None else -1,
        )
        if cursor is not None and start < 0:
            raise ValueError("Historical source detached; reload history")
        for index in traversal.source_indexes(start, len(sources)):
            source = sources[index]
            snapshot = source.registry().snapshot()
            request = MessagePageRequest(
                view.capture(snapshot), traversal.for_source(index == start), limit, max_bytes
            )
            page = request.read(WireLog(Path(source.root) / "bus.jsonl"))
            page = replace(page, messages=tuple(
                HistoricalMessage.project(message, source, index, snapshot)
                for message in page.messages
            ))
            if page.messages:
                return replace(page, has_older=page.has_older or index > 0,
                               has_newer=page.has_newer or index < len(sources) - 1)
        return MessagePage((), False, False)
