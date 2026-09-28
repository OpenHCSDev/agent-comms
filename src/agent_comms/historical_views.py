"""Source-bound historical display values. These carry no delivery authority."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, fields
from pathlib import Path

from .channels import Channel
from .messages import Message
from .read_basis import ChannelDisplayScope, DisplayBasis, DMDisplayScope, MessageDisplayScope
from .registration import Registration
from .registry_document import RegistrySnapshot
from .response_policy import InformationalPolicy, ResponsePolicy
from .store_files import file_revision
from .threads import Thread


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
