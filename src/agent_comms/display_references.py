"""Display-only references, derived from the original registry and channel catalog.

Finding a reference never creates a delivery mention or changes recipients.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
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


_NAME_START = re.compile(r"(?<![\w@/\\.#:-])@?(?=\S)")
_NAME_END = re.compile(r"[\w/\\@:-]|\.\w")


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
        targets = {name: HistoricalDisplayReference(str(root), thread.incarnation, source.key)
                   for name, thread in registry.named_members()}
        return DisplayReferences(str(root), targets)

    def for_source(self, source) -> DisplayReferences:
        if source is None:
            return self
        try:
            return self.sourced[source.snapshot_identity]
        except KeyError:
            recorded = self.recorded(self.root, source)
            return self.sourced.setdefault(source.snapshot_identity, DisplayReferences(self.root, {
                **{name: entry for name, reference in self.targets.items()
                   if (entry := reference.historical_entry(recorded.targets.get(name))) is not None},
                **recorded.targets,
            }))

    @cached_property
    def sourced(self) -> dict[object, DisplayReferences]:
        """One answer per source: every historical body on a page asks with the same one."""
        return {}

    @cached_property
    def name_lengths(self) -> tuple[int, ...]:
        """Longest first: a name wins over any shorter name it starts with."""
        return tuple(sorted({len(name) for name in self.targets}, reverse=True))

    def name_end(self, text: str, start: int) -> int:
        """Where the longest declared name beginning at start ends; start itself when none does."""
        ends = (start + length for length in self.name_lengths)
        return next((end for end in ends if text[start:end] in self.targets and not _NAME_END.match(text, end)), start)

    def find(self, text: str) -> tuple[DisplayReferenceMatch, ...]:
        """Names are looked up where one can start: thousands of threads never become a regex."""
        found = []
        position = 0
        while self.targets and (candidate := _NAME_START.search(text, position)):
            end = self.name_end(text, candidate.end())
            if end == candidate.end():
                position = max(end, candidate.start() + 1)
            else:
                found.append(DisplayReferenceMatch(self.targets[text[candidate.end():end]], candidate.start(), end))
                position = end
        return tuple(found)

    def accepts_history(self, root):
        return self.root == root

    def contains(self, reference: DisplayReference) -> bool:
        return self.targets.get(reference.name) == reference


@dataclass(frozen=True, eq=False)
class CurrentTargets(Mapping[str, DisplayReference]):
    """Each target is built when its name is asked for; a channel wins a name it shares with a member."""

    root: str
    members: Mapping[str, ThreadIncarnation] = field(repr=False)
    channels: tuple[str, ...]

    def __getitem__(self, name: str) -> DisplayReference:
        if name in self.channels:
            return ChannelDisplayReference(self.root, name)
        return ThreadDisplayReference(self.root, self.members[name])

    def __contains__(self, name: object) -> bool:
        return name in self.members or name in self.channels

    def __iter__(self) -> Iterator[str]:
        return iter(dict.fromkeys((*self.members, *self.channels)))

    def __len__(self) -> int:
        return len(self.members) + len(set(self.channels) - self.members.keys())


@dataclass(frozen=True)
class CurrentDisplayReferences(SnapshotRestored, DisplayReferences):
    """Current targets keep each member's current incarnation by name, read once from the
    registry cut, never the cut itself: every rendered line, link and draft holds its
    reference set, and a held cut pins every thread's whole registry record.

    Recorded catalogs share name recognition but cannot resolve a live target.
    """

    targets: Mapping[str, DisplayReference] = field(init=False, compare=False)
    members: Annotated[Mapping[str, ThreadIncarnation], SlicePolicy(MappingRowsSlice)] = field(
        default_factory=dict, repr=False)
    channels: Annotated[tuple[str, ...], SlicePolicy(WholeDocumentValue)] = ()

    @classmethod
    def from_registry(cls, root: str, registry: RegistrySnapshot, channels: tuple[str, ...] = ()) -> Self:
        return cls(root, {name: thread.incarnation for name, thread in registry.named_members()}, channels)

    def __post_init__(self) -> None:
        """Restoring a packet copies the member map; lookups derive from it when asked."""
        object.__setattr__(self, "members", MappingRowsSlice.freeze(self.members))
        object.__setattr__(self, "targets", CurrentTargets(self.root, self.members, self.channels))

    @cached_property
    def current(self) -> Mapping[float, ThreadIncarnation]:
        """Each member's incarnation by its recorded birth."""
        return MappingProxyType({
            incarnation.require_recorded().wire_birth: incarnation for incarnation in self.members.values()})

    def thread(self, incarnation: ThreadIncarnation) -> ThreadDisplayReference:
        """Resolve an exact birth to its current name; a missing birth fails."""
        return ThreadDisplayReference(self.root, self.current[incarnation.require_recorded().wire_birth])
