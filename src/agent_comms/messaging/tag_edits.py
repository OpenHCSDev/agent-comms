"""Tag edit and tag deletion disposition families, independent of ChannelManagement."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from agent_comms.cli_verbs.cli_result import TargetEditResult
from agent_comms.messaging.channel_targets import ChannelTarget, Tag
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import RecordMember
from agent_comms.registry.catalog_store import ChannelCatalog
from agent_comms.registry.registration import Registration
from agent_comms.registry.thread_identity import ThreadIncarnation
from agent_comms.registry.threads import Thread


class TagChannelOwner(ABC):
    """Channel operations used by tag edits and tag deletion dispositions."""

    catalog: ChannelCatalog
    registry: Registration

    @abstractmethod
    def create_tag(self, name: str) -> Tag: ...

    @abstractmethod
    def rename_tag(self, old_name: str, new_name: str) -> Tag: ...

    @abstractmethod
    def delete_tag(
        self,
        name: str,
        *,
        disposition: TagDisposition = ...,
        confirmed: bool = False,
    ) -> TagChangeResult: ...

    @abstractmethod
    def _apply_tag_change(
        self, old_tag: str, new_tag: str | None, affected: tuple[Thread, ...],
    ) -> None: ...


@dataclass(frozen=True)
class TagChangeResult(RecordMember, TargetEditResult):
    tag: str
    removed_tag: bool
    removed_threads: tuple[ThreadIncarnation, ...] = ()
    archived_threads: tuple[ThreadIncarnation, ...] = ()


@dataclass(frozen=True)
class TagDisposition(DeclaredFamily, affix="TagDisposition"):
    """Each declared channel operation owns its membership effect and warning."""

    label: ClassVar[str]

    def require_confirmed(self, name: tuple[str, ...], confirmed: bool) -> None:
        """Raise if confirmation is needed and not given."""
        if not confirmed:
            raise ValueError(self.confirmation(name))

    @abstractmethod
    def confirmation(self, tags: tuple[str, ...]) -> str: ...

    @staticmethod
    def tag_names(tags: tuple[str, ...]) -> str:
        return ", ".join(map(ChannelTarget.normalize_name, tags))

    @abstractmethod
    def apply(
        self, channels: TagChannelOwner, tag: str, cohort: tuple[Thread, ...],
    ) -> TagChangeResult: ...


class KeepThreadsTagDisposition(TagDisposition):
    label = "Remove tag; keep threads"

    def require_confirmed(self, name: tuple[str, ...], confirmed: bool) -> None:
        pass

    def confirmation(self, tags: tuple[str, ...]) -> str:
        return (
            f"Remove {self.tag_names(tags)} from their threads? "
            "Threads, saved views and history remain."
        )

    def apply(self, channels, tag, cohort):
        channels._apply_tag_change(tag, None, cohort)
        return TagChangeResult(tag, True)


class ArchiveThreadsTagDisposition(TagDisposition):
    label = "Archive tagged threads; keep tag"

    def confirmation(self, tags: tuple[str, ...]) -> str:
        return (
            f"Archive all stopped threads tagged with {self.tag_names(tags)}? "
            "Tags and history remain; active owners are refused."
        )

    def apply(self, channels, tag, cohort):
        with channels.registry.store.editing() as edit:
            channels.threads._archive_in(edit, cohort)
        return TagChangeResult(
            tag, False, archived_threads=tuple(thread.incarnation for thread in cohort)
        )


class DeleteThreadsTagDisposition(TagDisposition):
    label = "Delete tagged threads and remove tag"

    def confirmation(self, tags: tuple[str, ...]) -> str:
        return (
            f"Delete ALL inactive threads tagged with {self.tag_names(tags)} and close all "
            "their views? Active owners are refused; retained history and uncertain inputs "
            "are preserved."
        )

    def deletion_cohort(self, channels, tag, cohort) -> tuple[Thread, ...]:
        return cohort

    def apply(self, channels, tag, cohort):
        selected = self.deletion_cohort(channels, tag, cohort)
        channels.threads._delete_originals(selected)
        channels._apply_tag_change(
            tag, None, tuple(thread for thread in cohort if thread not in selected)
        )
        return TagChangeResult(tag, True, tuple(thread.incarnation for thread in selected))


class DeleteExclusiveInactiveThreadsTagDisposition(DeleteThreadsTagDisposition):
    label = "Delete inactive single-tag threads; remove tag from others"

    def confirmation(self, tags: tuple[str, ...]) -> str:
        return (
            f"For {self.tag_names(tags)}, delete only inactive threads with that sole tag, "
            "and remove each selected tag from the remaining threads? Active owners, multitag "
            "threads, their other tags, retained history and uncertain inputs remain."
        )

    def deletion_cohort(self, channels, tag, cohort):
        return tuple(
            member
            for thread in cohort
            if thread.tags == frozenset({tag})
            for member in channels.registry.status(thread.name).deletable_threads(thread)
        )


@dataclass(frozen=True, kw_only=True)
class TagEdit(DeclaredFamily, affix="TagEdit"):
    """A tag operation carries only the arguments that operation uses."""

    def apply(self, channels: TagChannelOwner) -> frozenset[str]:
        self.change(channels)
        return channels.catalog.read().tags

    @abstractmethod
    def change(self, channels: TagChannelOwner) -> None: ...


class ListTagEdit(TagEdit):
    label = "List tags"

    def change(self, channels: TagChannelOwner) -> None:
        pass


@dataclass(frozen=True, kw_only=True)
class NamedTagEdit(TagEdit, declared_name=None):
    name: str


class CreateTagEdit(NamedTagEdit):
    label = "Create tag"

    def change(self, channels: TagChannelOwner) -> None:
        channels.create_tag(self.name)


@dataclass(frozen=True, kw_only=True)
class RenameTagEdit(NamedTagEdit):
    label: ClassVar[str] = "Rename tag"
    new_name: str

    def change(self, channels: TagChannelOwner) -> None:
        channels.rename_tag(self.name, self.new_name)


@dataclass(frozen=True, kw_only=True)
class DeleteTagEdit(NamedTagEdit):
    label: ClassVar[str] = "Delete tag"
    disposition: TagDisposition = KeepThreadsTagDisposition()
    confirmed: bool = False

    def change(self, channels: TagChannelOwner) -> None:
        channels.delete_tag(self.name, disposition=self.disposition, confirmed=self.confirmed)
