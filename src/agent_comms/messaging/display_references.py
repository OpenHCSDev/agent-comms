"""Display-only references, derived from the original registry and channel catalog.

Finding a reference never creates a delivery mention or changes recipients.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from functools import cached_property
from types import MappingProxyType
from typing import Annotated, Generic, Self, TypeVar

from agent_comms.messaging.channel_targets import IncarnationThreadTarget, ThreadTarget
from agent_comms.messaging.page_traversal import HistorySourceKey
from agent_comms.messaging.snapshot_slices import (
    MappingRowsSlice,
    SnapshotRestored,
    WholeDocumentValue,
)
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import SlicePolicy
from agent_comms.registry.registry_names import RegistryNames
from agent_comms.registry.thread_identity import ThreadIncarnation
from agent_comms.registry.thread_identity_source import ThreadIdentitySource
from agent_comms.storage.private_path import FileObservation
from agent_comms.transport.tool_result import ToolResult


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


ReadT = TypeVar("ReadT")


class ThreadReferenceReader(ABC, Generic[ReadT]):
    """A question whose answer differs between a current and a retired birth. The reference
    calls the hook for what it is, so no reader tests which member it holds."""

    @abstractmethod
    def current(self, reference: ThreadDisplayReference) -> ReadT:
        """The answer for a birth the cut holds, whose name is current."""

    @abstractmethod
    def retired(self, reference: RetiredThreadReference) -> ReadT:
        """The answer for a birth the cut no longer holds; its name may belong to a successor."""


@dataclass(frozen=True)
class ThreadReference(ThreadReferenceIdentity, DisplayReference, declared_name=None):
    """A thread birth as one cut resolved it; it answers each question for its birth,
    so callers never act on a name a redeclared peer may have taken over."""

    @abstractmethod
    def target(self) -> ThreadTarget:
        """Where a send to this birth goes."""

    @abstractmethod
    def read(self, reader: ThreadReferenceReader[ReadT]) -> ReadT:
        """The reader's answer for this birth."""


@dataclass(frozen=True)
class ThreadDisplayReference(ThreadReference):
    """A birth the cut holds: its name is current."""

    def target(self) -> ThreadTarget:
        return ThreadTarget(self.name)

    def read(self, reader: ThreadReferenceReader[ReadT]) -> ReadT:
        return reader.current(self)


@dataclass(frozen=True)
class RetiredThreadReference(ThreadReference):
    """A birth the cut no longer holds: its peer was deleted or redeclared. Its recorded
    name is only a label; a new peer may already carry it."""

    def current_in(self, references: DisplayReferences) -> bool:
        return False

    def target(self) -> ThreadTarget:
        # Addresses the original birth, which refuses once it is unregistered.
        return IncarnationThreadTarget(self.incarnation)

    def read(self, reader: ThreadReferenceReader[ReadT]) -> ReadT:
        return reader.retired(self)


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


class HistoryReferenceSource(ABC):
    """The original recorded identity and names needed to link a history body."""

    __slots__ = ()
    provenance: RegistryNames[ThreadIdentitySource]

    @property
    @abstractmethod
    def key(self) -> HistorySourceKey: ...

    @property
    @abstractmethod
    def snapshot_identity(self) -> tuple[HistorySourceKey, int, FileObservation]: ...


class RegistryReferenceSource(ABC):
    """A UI observation that can read a root's names from Core's registry and channel catalog."""

    @abstractmethod
    async def registry_references(self, root: str) -> ReferenceContext: ...


class ReferenceContext(ABC):
    """Availability is not an empty successful catalog."""
    requires_observation = False

    async def acquire_from(self, read):
        return self

    async def settled(self, observation: RegistryReferenceSource) -> ReferenceContext:
        """The references to draw with now: an answered context is already settled."""
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

    def for_source(self, source: MessageHistory) -> ReferenceContext:
        """The names a body from `source` links through."""
        return source.references_from(self)

    @abstractmethod
    def for_history(self, source: HistoryReferenceSource) -> ReferenceContext:
        """The names a body recorded by an attached history links through."""


class MessageHistory:
    """Where a displayed message was recorded: an attached history snapshot, or nowhere (live)."""

    __slots__ = ()

    def references_from(self, context: ReferenceContext) -> ReferenceContext:
        """The names a body recorded here links through; a live message uses the current ones."""
        return context


class NoHistorySource(MessageHistory):
    """A live message: no snapshot recorded it."""

    __slots__ = ()


NO_HISTORY_SOURCE = NoHistorySource()


@dataclass(frozen=True)
class RootedReferences(ReferenceContext):
    root: Annotated[str, SlicePolicy(WholeDocumentValue)]
    requires_observation = True

    async def acquire_from(self, read):
        return await read(self.root)

    def for_history(self, source: HistoryReferenceSource) -> DisplayReferences:
        return DisplayReferences.recorded(self.root, source)


@dataclass(frozen=True)
class ProjectReferences(ReferenceContext):
    """An ordinary project view has no coordination identity to recognize."""
    description = ""

    def for_history(self, source: HistoryReferenceSource) -> ReferenceContext:
        raise ValueError("Recorded references require their original coordination root")


@dataclass(frozen=True)
class PendingReferences(RootedReferences):
    description = "Waiting for the original reference catalog."

    async def settled(self, observation: RegistryReferenceSource) -> ReferenceContext:
        """Names come from the registry and the channel catalog, not from a sidebar capture."""
        return await observation.registry_references(self.root)


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

    def at(self, root: str) -> RootedReferences:
        """These names for a view at ``root``; another root's view cannot resolve them."""
        return self if root == self.root else ForeignReferences(root, self.root)

    @staticmethod
    def recorded(root, source) -> DisplayReferences:
        registry = source.provenance
        targets = {name: HistoricalDisplayReference(str(root), thread.incarnation, source.key)
                   for name, thread in registry.named_members()}
        return DisplayReferences(str(root), targets)

    def for_history(self, source: HistoryReferenceSource) -> DisplayReferences:
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


class ThreadNames(ABC):
    """What resolves a thread's exact birth to the name it currently goes by."""

    @abstractmethod
    def thread(self, incarnation: ThreadIncarnation) -> ThreadReference:
        """The incarnation as this source resolves it, or this source's refusal to say."""


@dataclass(frozen=True)
class CurrentDisplayReferences(SnapshotRestored, DisplayReferences, ThreadNames):
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
    def from_registry(cls, root: str, registry: RegistryNames[ThreadIdentitySource], channels: tuple[str, ...] = ()) -> CurrentDisplayReferences:
        return ReferenceNames.of(registry, channels).references(root)

    def __post_init__(self) -> None:
        """Restoring a packet copies the member map; lookups derive from it when asked."""
        object.__setattr__(self, "members", MappingRowsSlice.freeze(self.members))
        object.__setattr__(self, "targets", CurrentTargets(self.root, self.members, self.channels))

    @cached_property
    def current(self) -> Mapping[float, ThreadIncarnation]:
        """Each member's incarnation by its recorded birth."""
        return MappingProxyType({
            incarnation.require_recorded().wire_birth: incarnation for incarnation in self.members.values()})

    def thread(self, incarnation: ThreadIncarnation) -> ThreadReference:
        """Resolve an exact birth to its current name; a birth this cut no longer holds is retired."""
        birth = incarnation.require_recorded().wire_birth
        if birth in self.current:
            return ThreadDisplayReference(self.root, self.current[birth])
        return RetiredThreadReference(self.root, incarnation)


@dataclass(frozen=True)
class ReferenceNames(ToolResult):
    """The names a UI links through: each registry name's incarnation and every declared channel.

    A remote UI reads this instead of the whole registry (megabytes of thread
    records over a tunnel) when all it needs is which names are linkable.
    """

    members: Mapping[str, ThreadIncarnation]
    channels: tuple[str, ...]

    @classmethod
    def of(cls, registry: RegistryNames[ThreadIdentitySource], channels: tuple[str, ...] = ()) -> ReferenceNames:
        """The one construction of the linkable names; the registry decodes no row for it."""
        return cls(dict(registry.named_incarnations()), channels)

    def references(self, root: str) -> CurrentDisplayReferences:
        return CurrentDisplayReferences(root, self.members, self.channels)
