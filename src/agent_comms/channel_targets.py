"""Named destinations own route behavior; strings are decoded at the boundary."""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, overload

from .declared_family import DeclaredFamily
from .errors import RelationViolationError, UnregisteredThreadError
from .field_codec import NullMember, SingleFieldValue, SubclassUnion
from .history_result import DmHistoryPageResult, HistoryPageResult
from .response_policy import DirectPolicy, MentionedOnlyPolicy
from .wake_policy import BoundedTriageWake, FullWake, MessageAudience, UnmentionedWake

if TYPE_CHECKING:
    from .bus_activity_index import ActivityClocks
    from .catalog_document import CatalogDocument
    from .channels import Channel
    from .goal_management import Goals
    from .historical_views import ChannelHistory, DMHistory, HistoryView
    from .history_views import HistoryViews
    from .messages import Message
    from .read_basis import ChannelConversation, Conversation, DisplayViewer, DMConversation
    from .registry_provenance import RegistryProvenance
    from .thread_status import ThreadStatus
    from .threads import Thread


class ChannelSelection(ABC):
    """Decoded menu context; container shapes stop at this input boundary."""
    needs_catalog: ClassVar[bool] = True

    @abstractmethod
    def channels_for(self, target: str) -> tuple[str | None, ...]: ...

    def require_unbound(self) -> None:
        """Ordinary thread actions may ignore one surrounding channel."""


class SurroundingChannel(SubclassUnion, ChannelSelection, affix="Channel"):
    """The one channel a command line names around its targets, or none: null or the name."""


@dataclass(frozen=True)
class NoChannel(NullMember, SurroundingChannel):
    needs_catalog: ClassVar[bool] = False

    def channels_for(self, target: str) -> tuple[str | None, ...]:
        return (None,)


@dataclass(frozen=True)
class OneChannel(SingleFieldValue, SurroundingChannel):
    name: str

    def channels_for(self, target: str) -> tuple[str | None, ...]:
        return (self.name,)


@dataclass(frozen=True)
class PerTargetChannels(ChannelSelection):
    mapping: dict[str, tuple[str, ...]]

    def channels_for(self, target: str) -> tuple[str | None, ...]:
        return self.mapping.get(target, (None,))

    def require_unbound(self) -> None:
        raise ValueError('This action does not accept per-target channels')


@dataclass(frozen=True)
class ResolvedThread:
    thread: Thread
    status: ThreadStatus


@dataclass(frozen=True)
class ResolvedChannel:
    channel: Channel
    members: tuple[ResolvedThread, ...]


def exact_stored_target(name: str) -> bool:
    from .channels import BuiltinChannelView

    return (BuiltinChannelView.addressed(name).exact_stored_target()
            if name in BuiltinChannelView.addresses() else True)


class CommandTarget(ABC):
    """A command destination owns its binding and catalog participation."""
    needs_channel_state: ClassVar[bool] = False

    @staticmethod
    @overload
    def named(name: str) -> NamedTarget: ...

    @staticmethod
    @overload
    def named(name: CommandTarget) -> CommandTarget: ...

    @staticmethod
    def named(name: str | CommandTarget) -> CommandTarget:
        if isinstance(name, CommandTarget):
            return name
        if not isinstance(name, str) or not name:
            raise ValueError("Select a named target")
        name = name.removeprefix(ThreadTarget.sigil)
        if not name:
            raise ValueError("Select a named target")
        return ChannelTarget(name) if ChannelTarget.names_channel(name) else ThreadTarget(name)

    @staticmethod
    def arguments(names: tuple[str, ...], project: str):
        selected = tuple(CommandTarget.named(name) for name in names) or (ProjectTarget(project),)
        return selected[0] if len(selected) == 1 else selected

    @property
    @abstractmethod
    def names(self) -> tuple[str, ...]: ...

    def available(self, snapshot) -> bool:
        return True

    @abstractmethod
    def resolve(self, snapshot, require_channel): ...

    @abstractmethod
    def bindings(self, declaration, comms, channel, resolved, *, snapshot, catalog): ...

    def history_view(self, viewer: DisplayViewer, require_channel: Callable[[str], Channel],
                     channel: type[ChannelHistory], direct: type[DMHistory]) -> HistoryView:
        raise ValueError("History requires a #channel or DM peer.")

    def history_result(self, require_channel: Callable[[str], Channel], messages: tuple[Message, ...],
                       has_older: bool) -> HistoryPageResult:
        raise ValueError("History requires a #channel or DM peer.")

    def contexts(self, declaration, channel):
        return (None,)


@dataclass(frozen=True)
class NamedTarget(CommandTarget):
    name: str
    sigil: ClassVar[str]
    batchable = False

    @property
    def direct_names(self) -> tuple[str, ...]:
        return ()

    guidance_scope = ""
    default_audience = MessageAudience.DIRECT

    @classmethod
    def address(cls, name: str) -> str:
        """The target name an address of this kind names once its sigil is read."""
        return name

    @staticmethod
    def sigils() -> tuple[str, ...]:
        """Every named target kind's sigil, from the family."""
        return tuple(kind.sigil for kind in NamedTarget.__subclasses__())

    @staticmethod
    def leads(text: str) -> bool:
        """Whether text opens with a named target's address."""
        return text.lstrip().startswith(NamedTarget.sigils())

    @property
    def names(self) -> tuple[str, ...]:
        return (self.name,)

    @abstractmethod
    def direct(self, goals: Goals, message: Message) -> None:
        """A plain owner message to this destination, recorded where it holds requests."""

    @abstractmethod
    def conversation_key(self, sender: str, snapshot: RegistryProvenance,
                         channel: type[ChannelConversation], direct: type[DMConversation]) -> Conversation: ...

    @abstractmethod
    def pending_count(
        self, counts: dict[str, int], snapshot: RegistryProvenance,
        require_channel: Callable[[str], Channel],
    ) -> int: ...

    @abstractmethod
    def changed_threads(
        self, names: Iterable[str], snapshot: RegistryProvenance,
        catalog: CatalogDocument, activity: ActivityClocks,
    ) -> tuple[str, ...]: ...

    @abstractmethod
    def mark_view_read(self, views: HistoryViews, worktree: str) -> None: ...

    def canonical_name(self, canonical):
        return self.name

    def require_channel(self):
        raise ValueError(f"{self.name!r} is not a channel target.")

    def require_dm_peer(self):
        raise ValueError("A DM page requires a registered peer.")

    def direct_peer(self, snapshot):
        return next((snapshot.threads.get(snapshot.canonical_name(name)) for name in self.direct_names), None)

    def inbound_relations(self, sender):
        return (sender, *(name for name in self.names if name not in self.direct_names))

    def response_recipients(self, recipients):
        return tuple(member for member in recipients if not self.direct_names or member.canonical_thread in self.direct_names)

    def response_policy(self, message):
        return DirectPolicy.instance()

    def conversation(self, sender, viewer, canonical):
        target = self.canonical_name(canonical)
        return self.reply_target(canonical(sender)) if target == viewer else target

    def wake_audience(self, mentions):
        return MessageAudience.MENTIONED if mentions else self.default_audience

    def effective_mentions(self, message, audience):
        return frozenset()

    def addressed(self, threads: tuple[str, ...], body: str) -> str:
        """One recipient is its own audience."""
        return body

    def require_frozen_recipients(self, message, audience):
        """Non-direct targets impose no exact-recipient requirement."""


class ThreadTarget(NamedTarget):
    sigil = "@"
    history_result_type = DmHistoryPageResult
    kind = "thread"
    incoming_scope = "direct message"

    def require_frozen_recipients(self, message, audience):
        message.role.require_recipients(self, audience)

    @property
    def direct_names(self) -> tuple[str, ...]:
        return self.names

    def canonical_name(self, canonical):
        return canonical(self.name)

    def require_dm_peer(self):
        return self.name

    def direct(self, goals: Goals, message: Message) -> None:
        """A registered thread holds owner requests; a peer the registry lacks holds none."""
        if self.name in goals.registry:
            goals.direct(self.name, message)

    def history_view(self, viewer: DisplayViewer, require_channel: Callable[[str], Channel],
                     channel: type[ChannelHistory], direct: type[DMHistory]) -> HistoryView:
        return viewer.direct_history(direct, self.name)

    def history_result(self, require_channel: Callable[[str], Channel], messages: tuple[Message, ...],
                       has_older: bool) -> HistoryPageResult:
        return self.history_result_type.for_target(self.name, messages, has_older)

    def contexts(self, declaration, channel):
        return declaration.selection_channels(channel, self.name)

    def available(self, snapshot) -> bool:
        return self.canonical_name(snapshot.canonical_name) in snapshot.threads

    def pending_count(
        self, counts: dict[str, int], snapshot: RegistryProvenance,
        require_channel: Callable[[str], Channel],
    ) -> int:
        return counts.get(snapshot.require(self.name).name, 0)

    def resolve(self, snapshot, require_channel) -> ResolvedThread:
        thread = snapshot.require(self.name)
        return ResolvedThread(thread, snapshot.status(thread.name))

    def bindings(self, declaration, comms, channel, resolved: ResolvedThread, *, snapshot, catalog):
        return declaration.thread_bindings(comms, resolved.thread, resolved.status, channel, catalog=catalog)

    def validate(self, message):
        if message.sender == self.name and message.type.conversation_message:
            raise RelationViolationError(f"Thread {message.sender!r} cannot message itself.")
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
        if not set(self.name) <= allowed:
            raise ValueError(f"Message target {self.name!r} is not a thread name or #channel.")

    def reply_target(self, sender):
        return sender

    def wake_policy(self, recipient, mentions):
        return FullWake()

    def require_publication(self, message, snapshot, require_channel):
        target = self.canonical_name(snapshot.canonical_name)
        if target not in snapshot.threads:
            raise UnregisteredThreadError(f"Target {self.name!r} is not a registered thread.")
        if target == snapshot.canonical_name(message.sender) and message.type.conversation_message:
            raise RelationViolationError(f"Thread {target!r} cannot message itself.")

    def initial_delivery(self, sender, message, snapshot, require_channel):
        target = self.canonical_name(snapshot.canonical_name)
        recipient = snapshot.require(target)
        snapshot.status(target).require_visible()
        if target == sender:
            raise RelationViolationError("A thread cannot message itself.")
        message, names = recipient.role.initial_delivery(message, target)
        return ThreadTarget(target), message, names

    def display_scope(self, viewer, snapshot, require_channel, direct_scope):
        return direct_scope.capture(viewer, snapshot.require(self.name).name, snapshot)

    def conversation_key(self, sender, snapshot, channel, direct):
        names = sorted({snapshot.canonical_name(sender), self.canonical_name(snapshot.canonical_name)})
        return direct(participants=tuple(snapshot.participant(name).incarnation for name in names))

    def changed_threads(
        self, names: Iterable[str], snapshot: RegistryProvenance,
        catalog: CatalogDocument, activity: ActivityClocks,
    ) -> tuple[str, ...]:
        return (self.canonical_name(snapshot.canonical_name),)

    def mark_view_read(self, views: HistoryViews, worktree: str) -> None:
        views.mark_thread_user_read(self.name, worktree=worktree)


class ChannelTarget(NamedTarget):
    needs_channel_state = True
    batchable = True
    kind = "channel"
    default_audience = MessageAudience.COLLECTIVE

    sigil = "#"

    @classmethod
    def address(cls, name: str) -> str:
        return cls.normalize_name(name)

    @classmethod
    def names_channel(cls, name: str) -> bool:
        return name.startswith(cls.sigil)

    @classmethod
    def normalize_name(cls, name: str) -> str:
        """The channel address of a catalog name (a tag or saved view); an address is kept."""
        return name if cls.names_channel(name) else f"{cls.sigil}{name}"

    @classmethod
    def bare_name(cls, name: str) -> str:
        """The catalog name a channel address carries: its tag or saved view name."""
        return name.removeprefix(cls.sigil)

    @property
    def incoming_scope(self):
        return self.name

    @property
    def guidance_scope(self):
        return f"; delivery and history remain {self.name}"

    def require_channel(self):
        return self

    def direct(self, goals: Goals, message: Message) -> None:
        """A channel holds no owner requests."""

    def history_view(self, viewer: DisplayViewer, require_channel: Callable[[str], Channel],
                     channel: type[ChannelHistory], direct: type[DMHistory]) -> HistoryView:
        return channel(require_channel(self.name).history_scope())

    def history_result(self, require_channel: Callable[[str], Channel], messages: tuple[Message, ...],
                       has_older: bool) -> HistoryPageResult:
        result_type = require_channel(self.name).history_result_type
        return result_type.for_target(self.name, messages, has_older)

    def pending_count(
        self, counts: dict[str, int], snapshot: RegistryProvenance,
        require_channel: Callable[[str], Channel],
    ) -> int:
        return require_channel(self.name).pending_count(counts)

    def resolve(self, snapshot, require_channel) -> ResolvedChannel:
        channel = require_channel(self.name)
        members = tuple(ResolvedThread(thread, snapshot.status(thread.name))
                        for thread in snapshot.channel_audience()
                        if channel.matches(thread.tags))
        return ResolvedChannel(channel, members)

    def bindings(self, declaration, comms, channel, resolved: ResolvedChannel, *, snapshot, catalog):
        return declaration.channel_bindings(comms, resolved, snapshot=snapshot, catalog=catalog)

    def validate(self, sender=None):
        from .channels import BuiltinChannelView

        if self.name not in BuiltinChannelView.addresses():
            Tag.validate(self.name[1:], label=f"Channel {self.name!r}")

    def response_policy(self, message):
        if message.mentions:
            return MentionedOnlyPolicy.instance()
        return message.sender_role.channel_policy()

    def reply_target(self, sender):
        return self.name

    def effective_mentions(self, message, audience):
        return frozenset(mention.thread for mention in message.mentions).intersection(audience.canonical_members)

    def addressed(self, threads: tuple[str, ...], body: str) -> str:
        """Keep collective posts unchanged; prefix mentions for a named audience."""
        return " ".join((*(f"@{thread}" for thread in threads), body))

    def wake_policy(self, recipient, mentions):
        if mentions:
            return FullWake() if recipient.canonical_thread in mentions else UnmentionedWake()
        return BoundedTriageWake()

    def require_publication(self, message, snapshot, require_channel):
        require_channel(self.name).require_routable()

    def initial_delivery(self, sender, message, snapshot, require_channel):
        channel = require_channel(self.name)
        names = [thread.name for thread in snapshot.channel_audience()
                 if thread.name != sender and channel.matches(thread.tags)]
        return self, message, names

    def display_scope(self, viewer, snapshot, require_channel, direct_scope):
        return require_channel(self.name).history_scope()

    def conversation_key(self, sender, snapshot, channel, direct):
        return channel(target=self.name)

    def changed_threads(
        self, names: Iterable[str], snapshot: RegistryProvenance,
        catalog: CatalogDocument, activity: ActivityClocks,
    ) -> tuple[str, ...]:
        channel = catalog.require_channel(self.name, activity)
        return tuple(name for name in names if (thread := snapshot.threads.get(name)) is not None
                     and channel.matches(thread.tags))

    def mark_view_read(self, views: HistoryViews, worktree: str) -> None:
        from .presentation import ReadAll

        views.presentation.mark_channel_read(
            self.name, viewer=views.messaging.user_identity(worktree).name, mark=ReadAll(),
        )


@dataclass(frozen=True)
class ProjectTarget(CommandTarget):
    project: str

    @property
    def name(self) -> str:
        return self.project

    @property
    def names(self) -> tuple[str, ...]:
        return ()

    def resolve(self, snapshot, require_channel):
        return self.project

    def bindings(self, declaration, comms, channel, resolved, *, snapshot, catalog):
        return declaration.project_bindings(comms, resolved)


_TAG_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789-_")


@dataclass(frozen=True, slots=True)
class Tag:
    name: str

    def __post_init__(self) -> None:
        self.validate(self.name)

    @staticmethod
    def validate(name: str, *, label: str = "Tags") -> None:
        from .channels import BuiltinChannelView

        if (not name or not set(name) <= _TAG_CHARS
                or ChannelTarget.normalize_name(name) in BuiltinChannelView.addresses()):
            raise ValueError(f"{label} must be lowercase alphanumeric with hyphens/underscores; built-in channel names are reserved.")


class DeclaredTag(DeclaredFamily, affix="Tag"):
    """A tag every bus has by declaration: its name is its class name, and its channel
    resolves before any thread carries it. Membership is still by tag."""

    @classmethod
    def channel(cls) -> str:
        return ChannelTarget.normalize_name(cls.declared_name)


class CommsTag(DeclaredTag):
    """The channel the fleet coordinates on: installer reports, Core's strike announcements
    and the commander's posts address it."""
