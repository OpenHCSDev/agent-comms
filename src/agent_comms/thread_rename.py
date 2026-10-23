"""The rename operation and its privacy-specific ownership transaction."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, ClassVar

from .coordination_contracts import MAX_IDENTIFIER_CHARS
from .coordination_database import CoordinationStore
from .coordination_errors import ParticipantNotRegistered
from .coordinator import Coordination
from .errors import RelationViolationError
from .private_registry_guard import (
    PrivateOwnerRenameIntent,
    PrivateRegistryGuard,
    PublicRegistry,
    RegistryPrivacy,
    _require_no_private_owner_rename,
)
from .thread_provenance import ThreadProvenance
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
    """Moves a committed coordinator owner under a durable intent, so a crash between the two
    stores fails closed instead of turning an old attempt into a new send."""

    privacy = PrivateRegistryGuard
    registry_privacy: PrivateRegistryGuard

    def renamed(self) -> tuple[str, str]:
        root, registry = self.threads.root, self.threads.registry
        before, new_name = self.before, self.new_name
        _require_no_private_owner_rename(root)
        coordinator = CoordinationStore.path_in(root)
        if not coordinator.exists() or before.name == new_name:
            return self.registry_renamed()
        # Validate registry/SQL name refusals before renaming the private owner.
        # Registry readers cannot select a different owner while both stores change.
        registry.status(before.name).require_self_rename()
        if registry.snapshot().taken(new_name, owner=before.name):
            raise RelationViolationError(f"Thread name {new_name!r} is already in use.")
        ThreadProvenance.require_name(new_name)
        if len(new_name) > MAX_IDENTIFIER_CHARS:
            raise ValueError("Private coordinator owner name exceeds its bound.")
        with Coordination(str(coordinator)) as store:
            try:
                person = store.participants.get(before.incarnation.require_recorded().lookup)
            except ParticipantNotRegistered:
                return self.registry_renamed()
            if not person.committed or person.owner_identity.owner_thread != before.name:
                raise RelationViolationError(
                    "Private coordinator owner differs from the registered owner."
                )
            generation = person.owner_identity.generation
            PrivateOwnerRenameIntent(
                lookup=person.lookup,
                old=before.name,
                new=new_name,
                generation=generation,
                wire_root_id=self.registry_privacy.root_id.hex(),
            ).write(root)
            store.participants.rename_owner(person.lookup, new_name, expected_generation=generation)
            try:
                return self.registry_renamed()
            except BaseException as error:
                # Registry persistence can fail after the SQL CAS. If its atomic snapshot still
                # names the old owner, restore that name without revoking its active attempt.
                uncertain = RelationViolationError(
                    "Private owner rename is uncertain; inspect both authorities."
                )
                try:
                    if registry.require(before.name).name == before.name:
                        store.participants.rename_owner(
                            person.lookup, before.name, expected_generation=generation
                        )
                except BaseException as compensation:
                    # Ambiguous dual-store failure: the inspection needs both failures.
                    raise uncertain from compensation
                raise uncertain from error

    def applied(self, title: ThreadText) -> RenameThreadResult:
        result = ThreadRename.applied(self, title)
        # The registry lock and renamed() bind a present intent to this rename;
        # it completes only after both authorities and the ancillary stores agree.
        if PrivateOwnerRenameIntent.path_in(self.threads.root).exists():
            PrivateOwnerRenameIntent.complete(self.threads.root)
        return result
