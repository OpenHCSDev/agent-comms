"""Display-only references, derived from the original registry and channel catalog.

Finding a reference never creates a delivery mention or changes recipients.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cached_property
import re
from types import MappingProxyType
from typing import TYPE_CHECKING, Annotated, Self

from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import SlicePolicy
from agent_comms.registry_document import MappingRowsSlice, SnapshotRestored, WholeDocumentValue
from agent_comms.thread_identity import ThreadIncarnation

if TYPE_CHECKING:
    from agent_comms.registry_document import RegistrySnapshot


@dataclass(frozen=True)
class DisplayReference(DeclaredFamily, affix="Reference"):
    root: str

    @property
    @abstractmethod
    def name(self) -> str: ...

    def current_in(self, references: DisplayReferences) -> bool:
        return references.contains(self)

    def historical_entry(self, recorded: DisplayReference | None) -> DisplayReference | None:
        return recorded


@dataclass(frozen=True)
class ThreadReferenceIdentity:
    incarnation: ThreadIncarnation

    @property
    def name(self) -> str:
        return self.incarnation.name


@dataclass(frozen=True)
class ThreadDisplayReference(ThreadReferenceIdentity, DisplayReference):
    pass


@dataclass(frozen=True)
class HistoricalDisplayReference(ThreadReferenceIdentity, DisplayReference):
    source: str

    def current_in(self, references: DisplayReferences) -> bool:
        # Recorded identity is intentionally not current registry membership.
        return references.accepts_history(self.root)


@dataclass(frozen=True)
class ChannelDisplayReference(DisplayReference):
    target: str

    def historical_entry(self, recorded: DisplayReference | None) -> DisplayReference:
        return self

    @property
    def name(self) -> str:
        return self.target


@dataclass(frozen=True)
class DisplayReferenceMatch:
    reference: DisplayReference
    start: int
    end: int


class ReferenceContext(ABC):
    """Availability is not an empty successful catalog."""
    requires_observation = False

    async def acquire_from(self, read):
        return self

    @property
    @abstractmethod
    def description(self): ...

    def find(self, text):
        return ()

    def contains(self, reference):
        return False

    def accepts_history(self, root):
        return False

    @abstractmethod
    def for_source(self, source): ...


@dataclass(frozen=True)
class RootedReferences(ReferenceContext):
    root: Annotated[str, SlicePolicy(WholeDocumentValue)]
    requires_observation = True

    async def acquire_from(self, read):
        return await read(self.root)

    def for_source(self, source):
        return self if source is None else DisplayReferences.recorded(self.root, source)


@dataclass(frozen=True)
class ProjectReferences(ReferenceContext):
    """An ordinary project view has no coordination identity to recognize."""
    description = ""

    def for_source(self, source):
        if source is not None:
            raise ValueError("Recorded references require their original coordination root")
        return self


@dataclass(frozen=True)
class PendingReferences(RootedReferences):
    description = "Waiting for the original reference catalog."


@dataclass(frozen=True)
class UnavailableReferences(RootedReferences):
    reason: str

    @property
    def description(self):
        return f"Reference catalog unavailable: {self.reason}"


@dataclass(frozen=True)
class ForeignReferences(RootedReferences):
    observed_root: str

    @property
    def description(self):
        return f"This view belongs to {self.root}; the observed catalog belongs to {self.observed_root}."


@dataclass(frozen=True)
class DisplayReferences(RootedReferences):
    """One observation's unfiltered names; no store, widget or wake state."""

    description = ""
    targets: Mapping[str, DisplayReference] = field(default_factory=dict)

    @staticmethod
    def recorded(root, source) -> DisplayReferences:
        registry = source.provenance
        targets = {name: HistoricalDisplayReference(str(root), incarnation, source.key)
                   for name, incarnation in registry.named_incarnations()}
        return DisplayReferences(str(root), targets)

    def for_source(self, source) -> DisplayReferences:
        if source is None:
            return self
        recorded = self.recorded(self.root, source)
        return DisplayReferences(self.root, {
            **{name: entry for name, reference in self.targets.items()
               if (entry := reference.historical_entry(recorded.targets.get(name))) is not None},
            **recorded.targets,
        })

    @cached_property
    def pattern(self):
        # Escape declared names, not a second grammar for thread/channel names.
        names = "|".join(re.escape(name) for name in sorted(self.targets, key=len, reverse=True))
        return re.compile(r"(?<![\w@/\\.#:-])@?(?:" + names + r")(?![\w/\\@:-]|\.\w)")

    def find(self, text: str) -> tuple[DisplayReferenceMatch, ...]:
        if not self.targets:
            return ()
        return tuple(DisplayReferenceMatch(self.targets[match.group().removeprefix('@')],
                                           match.start(), match.end())
                     for match in self.pattern.finditer(text))

    def accepts_history(self, root):
        return self.root == root

    def contains(self, reference: DisplayReference) -> bool:
        return self.targets.get(reference.name) == reference


@dataclass(frozen=True)
class CurrentDisplayReferences(SnapshotRestored, DisplayReferences):
    """Current targets keep each member's current incarnation by name, read once from the
    registry cut, never the cut itself: every rendered line, link and draft holds its
    reference set, and a held cut pins every thread's whole registry record.

    Recorded catalogs share name recognition but cannot resolve a live target.
    """

    targets: Mapping[str, DisplayReference] = field(init=False)
    members: Annotated[Mapping[str, ThreadIncarnation], SlicePolicy(MappingRowsSlice)] = field(
        default_factory=dict, repr=False)
    channels: Annotated[tuple[str, ...], SlicePolicy(WholeDocumentValue)] = ()
    current: Mapping[str, ThreadIncarnation] = field(init=False, repr=False, compare=False)

    @classmethod
    def from_registry(cls, root: str, registry: RegistrySnapshot, channels: tuple[str, ...] = ()) -> Self:
        return cls(root, dict(registry.named_incarnations()), channels)

    def __post_init__(self) -> None:
        """The target and lookup views of the one stored member map."""
        object.__setattr__(self, "members", MappingRowsSlice.freeze(self.members))
        object.__setattr__(self, "current", MappingProxyType({
            incarnation.require_recorded().lookup: incarnation for incarnation in self.members.values()}))
        targets: dict[str, DisplayReference] = {
            name: ThreadDisplayReference(self.root, incarnation) for name, incarnation in self.members.items()}
        targets.update((name, ChannelDisplayReference(self.root, name)) for name in self.channels)
        object.__setattr__(self, "targets", MappingProxyType(targets))

    def thread(self, incarnation: ThreadIncarnation) -> ThreadDisplayReference:
        """Resolve an exact birth to its current name; a missing birth fails."""
        return ThreadDisplayReference(self.root, self.current[incarnation.require_recorded().lookup])
