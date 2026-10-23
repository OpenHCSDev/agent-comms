"""The rename operation and its privacy-specific ownership transaction."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, ClassVar

from .private_registry_guard import (
    PrivateRegistryGuard,
    PublicRegistry,
    RegistryPrivacy,
    _require_no_private_owner_rename,
)
from .thread_text import ThreadText
from .tool_result import ToolResult

if TYPE_CHECKING:
    from .thread_management import ThreadManagement
    from .threads import Thread


@dataclass(frozen=True, slots=True)
class RenameThreadResult(ToolResult):
    previous: str
    current: str
    changed: bool


@dataclass(frozen=True, slots=True)
class ThreadRename(ABC):
    """One rename, its kind derived from the registry's privacy."""

    privacy: ClassVar[type[RegistryPrivacy]]
    kinds: ClassVar[dict[type[RegistryPrivacy], type[ThreadRename]]] = {}
    threads: ThreadManagement
    before: Thread
    new_name: str
    registry_privacy: RegistryPrivacy

    def __init_subclass__(cls, **kwargs):
        # slots=True rebuilds the class; its final declaration replaces the first.
        super(ThreadRename, cls).__init_subclass__(**kwargs)
        cls.kinds[cls.privacy] = cls

    @classmethod
    def chosen(cls, threads: ThreadManagement, before: Thread, new_name: str) -> ThreadRename:
        privacy = threads._privacy()
        return cls.kinds[type(privacy)](threads, before, new_name, privacy)

    @abstractmethod
    def renamed(self) -> tuple[str, str]:
        """Move the registry name, after whatever this kind's owner stores need first."""

    def registry_renamed(self) -> tuple[str, str]:
        return self.threads.registry.rename(self.before.name, self.new_name)

    def applied(self, title: ThreadText) -> RenameThreadResult:
        previous, current = self.renamed()
        threads = self.threads
        thread = threads.registry.require(current)
        titled = replace(thread, auto_title_pending=False, title=title.applied(thread.title))
        if titled != thread:
            threads.registry.register(titled, threads.registry.status(current))
        if previous == current:
            return RenameThreadResult(previous, current, False)
        threads.agents.activity.rename_thread(previous, current)
        threads.agents.runtime_info.rename_thread(previous, current)
        threads.ledger.rename_thread(previous, current)
        with threads.catalog.editing() as document:
            document.rename_thread(previous, current)
        return RenameThreadResult(previous, current, True)


class PlainRename(ThreadRename):
    privacy = PublicRegistry

    def renamed(self) -> tuple[str, str]:
        return self.registry_renamed()


@dataclass(frozen=True, slots=True)
class PrivateOwnerRename(ThreadRename):
    """Rename registry presentation while retaining coordinator identity."""

    privacy = PrivateRegistryGuard
    registry_privacy: PrivateRegistryGuard

    def renamed(self) -> tuple[str, str]:
        # An unfinished historical dual-store rename still needs inspection.
        # New renames only change the registry; coordinator lookups are stable.
        _require_no_private_owner_rename(self.threads.root)
        return self.registry_renamed()
