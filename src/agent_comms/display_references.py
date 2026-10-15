"""Display-only references, derived from the original registry and channel catalog.

Finding a reference never creates a delivery mention or changes recipients.
"""
from __future__ import annotations

from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
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
        return self.root == references.root


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


@dataclass(frozen=True)
class DisplayReferences:
    """One observation's unfiltered names; no store, widget or wake state."""

    root: str = ""
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

    def find(self, text: str) -> tuple[DisplayReferenceMatch, ...]:
        # Escape declared names, not a second grammar for thread/channel names.
        # Paths, URLs, emails and longer identifiers must remain indivisible.
        if not self.targets:
            return ()
        names = "|".join(re.escape(name) for name in sorted(self.targets, key=len, reverse=True))
        pattern = re.compile(r"(?<![\w@/\\.#:-])@?(?:" + names + r")(?![\w/\\@:-]|\.\w)")
        return tuple(DisplayReferenceMatch(self.targets[match.group().removeprefix('@')],
                                           match.start(), match.end())
                     for match in pattern.finditer(text))

    def contains(self, reference: DisplayReference) -> bool:
        return self.targets.get(reference.name) == reference
