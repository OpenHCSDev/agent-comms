"""Display-only references, derived from the original registry and channel catalog.

Finding a reference never creates a delivery mention or changes recipients.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cached_property
import re

from .declared_family import DeclaredFamily
from .thread_identity import ThreadIncarnation


@dataclass(frozen=True)
class DisplayReference(DeclaredFamily, affix="Reference"):
    root: str

    @property
    @abstractmethod
    def name(self) -> str: ...

    def current_in(self, references: DisplayReferences) -> bool:
        return references.contains(self)


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
    root: str
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

    @classmethod
    def capture(cls, root, registry, channels) -> DisplayReferences:
        targets = {name: ThreadDisplayReference(str(root), thread.incarnation)
                   for name, thread in registry.threads.items()}
        targets.update((alias, targets[name]) for alias, name in registry.aliases.items()
                       if name in targets)
        targets.update((name, ChannelDisplayReference(str(root), name)) for name in channels)
        return cls(str(root), targets)

    @classmethod
    def recorded(cls, root, source) -> DisplayReferences:
        registry = source.provenance
        targets = {name: HistoricalDisplayReference(str(root), thread.incarnation, source.key)
                   for name, thread in registry.threads.items()}
        targets.update((alias, targets[name]) for alias, name in registry.aliases.items()
                       if name in targets)
        return cls(str(root), targets)

    def for_source(self, source) -> DisplayReferences:
        if source is None:
            return self
        recorded = self.recorded(self.root, source)
        return DisplayReferences(self.root, {
            **{name: reference for name, reference in self.targets.items()
               if isinstance(reference, ChannelDisplayReference)},
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
