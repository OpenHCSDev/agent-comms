"""Named destinations own route behavior; strings are decoded at the boundary.

A bus event names routes that changed. Each route's target owns which of its
open threads need a content decode and which need only the notification pass.
ThreadChange carries that decision so the observation loop dispatches on it
instead of treating every flagged thread the same way.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar

from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.errors import (
    HumanSendRefusedError,
    MentionOutsideChannelError,
    RelationViolationError,
    UnregisteredThreadError,
)
from agent_comms.messaging.activity_clocks import ActivityClocks
from agent_comms.messaging.frozen_recipient import FrozenRecipient
from agent_comms.messaging.projection_source import ProducedRow, ProjectionSource
from agent_comms.messaging.response_policy import DirectPolicy, MentionedPolicy
from agent_comms.nominal.class_fact import ClassFact
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import NullMember, SingleFieldValue, SubclassUnion
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.thread_identity import ThreadIncarnation
from agent_comms.storage.typed_table import sql_literal
from agent_comms.waking.wake_policy import BoundedTriageWake, FullWake, MessageAudience, WakePolicy

if TYPE_CHECKING:
    from agent_comms.cli_verbs.cli_command import CliCommand
    from agent_comms.comms import Comms
    from agent_comms.history_views import HistoryViews
    from agent_comms.messaging.audience_manifest import AudiencedMessage
    from agent_comms.messaging.channels import Channel
    from agent_comms.messaging.historical_views import (
        ChannelHistory,
        ConversationHistoryView,
        DMHistory,
        HistoryView,
    )
    from agent_comms.messaging.history_result import HistoryPageResult
    from agent_comms.messaging.messages import Message, MessageInput
    from agent_comms.messaging.read_basis import (
        ChannelConversation,
        Conversation,
        DisplayViewer,
        DMConversation,
        DMDisplayScope,
        MessageDisplayScope,
    )
    from agent_comms.messaging.response_policy import ResponsePolicy
    from agent_comms.planning.goal_management import Goals
    from agent_comms.registry.catalog_document import CatalogAccess, CatalogDocument
    from agent_comms.registry.registry_document import (
        RegistrySnapshot,
        ResolvedChannel,
        ResolvedThread,
    )
    from agent_comms.registry.registry_provenance import RegistryProvenance
    from agent_comms.registry.threads import Thread


@dataclass(frozen=True)
class ThreadChange:
    """What a bus event means for one open thread's observation.

    ContentChange: the thread's own transcript or delivery state changed — the
    observer must decode its presentation. MetadataChange: the thread's
    notification context changed (a channel message arrived for other members) —
    the existing notification pass detects and publishes the change without a
    full transcript decode.
    """
    name: str

    def contribute(self, content_changed: set[str]) -> None:
        content_changed.add(self.name)


@dataclass(frozen=True)
class ContentChange(ThreadChange):
    pass


@dataclass(frozen=True)
class MetadataChange(ThreadChange):
    def contribute(self, content_changed: set[str]) -> None:
        pass


class ChannelSelection(SubclassUnion, affix="Channel"):
    """Decoded menu context; its members cross a boundary by shape: none is null, one
    surrounding channel is its name, and per-target channels are their mapping."""
    def requires_catalog(self) -> bool:
        return True

    @abstractmethod
    def channels_for(self, target: str) -> tuple[SurroundingChannel, ...]: ...

    def require_unbound(self) -> None:
        """Ordinary thread actions may ignore one surrounding channel."""


class SurroundingChannel(ChannelSelection, declared_name=None):
    """The one channel a command line names around its targets, or none: null or the name."""

    def names_for_binding(self) -> tuple[str, ...]:
        return ()

    def scope(self) -> str:
        """How a refusal names this channel after its target; no channel adds nothing."""
        return ""


@dataclass(frozen=True)
class NoChannel(NullMember, SurroundingChannel):
    def requires_catalog(self) -> bool:
        return False

    def channels_for(self, target: str) -> tuple[SurroundingChannel, ...]:
        return (self,)


@dataclass(frozen=True)
class OneChannel(SingleFieldValue, SurroundingChannel):
    name: str

    def channels_for(self, target: str) -> tuple[SurroundingChannel, ...]:
        return (self,)

    def names_for_binding(self) -> tuple[str, ...]:
        return (self.name,)

    def scope(self) -> str:
        return f" in {self.name}"


@dataclass(frozen=True)
class PerTargetChannels(SingleFieldValue, ChannelSelection):
    mapping: dict[str, tuple[str, ...]]

    def channels_for(self, target: str) -> tuple[SurroundingChannel, ...]:
        return tuple(OneChannel(name) for name in self.mapping.get(target, ())) or (NoChannel(),)

    def require_unbound(self) -> None:
        raise ValueError('This action does not accept per-target channels')


def exact_stored_target(name: str) -> bool:
    from agent_comms.messaging.channels import BuiltinChannelView

    return (BuiltinChannelView.addressed(name).exact_stored_target()
            if name in BuiltinChannelView.addresses() else True)


class ResolvedTarget(ABC):
    """An acquired value owns its binding operation, not a second type dispatcher."""

    @abstractmethod
    def bindings[C: CliCommand](
        self, declaration: type[C], comms: Comms, channel: SurroundingChannel,
        *, project: Checkout, snapshot: RegistrySnapshot, catalog: CatalogAccess,
    ) -> tuple[C, ...]: ...


@dataclass(frozen=True)
class ResolvedProject(ResolvedTarget):
    project: Checkout

    def bindings[C: CliCommand](
        self, declaration: type[C], comms: Comms, channel: SurroundingChannel,
        *, project: Checkout, snapshot: RegistrySnapshot, catalog: CatalogAccess,
    ) -> tuple[C, ...]:
        return declaration.project_bindings(comms, self.project)


class CommandTarget(DeclaredFamily, ProjectionSource, affix="Target"):
    """A command destination owns resolution and catalog participation."""

    @ClassFact
    def writes(cls) -> tuple[type[ProducedRow], ...]:
        from agent_comms.messaging.bus_display_index import BusDisplayIndex
        from agent_comms.messaging.bus_tables import AwaitingAnswerSource
        return (BusDisplayIndex, AwaitingAnswerSource)

    @property
    def needs_channel_state(self) -> bool:
        return False

    def display_name(self) -> str:
        """The selected destination's name, including an unresolved destination."""
        return ", ".join(self.destination_names)

    @property
    @abstractmethod
    def name(self) -> str: ...

    @staticmethod
    def named(name: str) -> NamedTarget:
        if not name:
            raise ValueError("Select a named target")
        name = name.removeprefix(ThreadTarget.sigil)
        if not name:
            raise ValueError("Select a named target")
        return ChannelTarget(name) if ChannelTarget.names_channel(name) else ThreadTarget(name)

    @staticmethod
    def arguments(names: tuple[str, ...], project: Checkout) -> tuple[CommandTarget, ...]:
        return tuple(CommandTarget.named(name) for name in names) or (ProjectTarget(project),)

    @property
    @abstractmethod
    def destination_names(self) -> tuple[str, ...]: ...

    def available(self, snapshot: RegistrySnapshot) -> bool:
        return True

    @abstractmethod
    def resolve(
        self, snapshot: RegistrySnapshot, require_channel: Callable[[str], Channel],
    ) -> ResolvedTarget: ...

    def history_view(self, viewer: DisplayViewer, require_channel: Callable[[str], Channel],
                     channel: type[ChannelHistory], direct: type[DMHistory]) -> HistoryView:
        raise ValueError("History requires a #channel or DM peer.")

    def history_result(self, require_channel: Callable[[str], Channel],
                       direct: type[HistoryPageResult], messages: tuple[Message, ...],
                       has_older: bool) -> HistoryPageResult:
        raise ValueError("History requires a #channel or DM peer.")

    def contexts(
        self, declaration: type[CliCommand[CliResult]], channel: ChannelSelection,
    ) -> tuple[SurroundingChannel, ...]:
        return (NoChannel(),)


class ReplyRoute(ABC):
    """One reply destination, projected as a value or an indexed SQL selection."""

    @abstractmethod
    def target(self, target_name: str, sender: str) -> str: ...

    @abstractmethod
    def sql(
        self, column: str, sender: str, names: tuple[str, ...],
    ) -> tuple[str, tuple[str, ...]]: ...


class SenderReply(ReplyRoute):
    def target(self, target_name: str, sender: str) -> str:
        return sender

    def sql(
        self, column: str, sender: str, names: tuple[str, ...],
    ) -> tuple[str, tuple[str, ...]]:
        marks = ",".join("?" for _ in names)
        return (
            f"{self.target(column, sender)} IN ({marks}) AND NOT ({ChannelReply.channel_sql(column)})",
            names,
        )


class ChannelReply(ReplyRoute):
    def target(self, target_name: str, sender: str) -> str:
        return target_name

    def sql(
        self, column: str, sender: str, names: tuple[str, ...],
    ) -> tuple[str, tuple[str, ...]]:
        return f"{self.target(column, sender)}=?", names

    @staticmethod
    def channel_sql(column: str) -> str:
        sigil = ChannelTarget.sigil
        return f"substr({column},1,{len(sigil)})={sql_literal(sigil)}"


@dataclass(frozen=True, slots=True)
class NamedTarget(CommandTarget):
    name: str = field()
    sigil: ClassVar[str]
    route: ClassVar[ReplyRoute]

    def accepts_batch(self, seq: int) -> bool:
        return False

    @property
    def direct_names(self) -> tuple[str, ...]:
        return ()

    guidance_scope = ""
    default_audience = MessageAudience.DIRECT

    @abstractmethod
    def display_history(self, views: HistoryViews, project: Checkout) -> ConversationHistoryView:
        """The history this conversation is read through by the human user of `project`: its
        pages and its revisions both come from it."""

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
    def destination_names(self) -> tuple[str, ...]:
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
    ) -> tuple[ThreadChange, ...]: ...

    @abstractmethod
    def mark_view_read(self, views: HistoryViews, worktree: Checkout) -> None: ...

    def canonical_name(self, canonical: Callable[[str], str]) -> str:
        return self.name

    def selected_name(self, snapshot: RegistryProvenance) -> str:
        """Bind this destination inside the registry cut used by its consumer."""
        return self.canonical_name(snapshot.canonical_name)

    @abstractmethod
    def validate(self, message: Message) -> None: ...

    def reply_target(self, sender: str) -> str:
        return self.route.target(self.name, sender)

    def automatic_reply_targets(self, sender: str) -> tuple[str, ...]:
        """Shared destinations require an explicit send, never a session's final text."""
        return ()

    def answered_sql(
        self, target: str, sender: str, snapshot: RegistryProvenance,
    ) -> tuple[str, tuple[str, ...]]:
        """Select originals answered by this destination, using the same registry cut."""
        names = tuple(snapshot.names_for(self.canonical_name(snapshot.canonical_name)))
        return self.route.sql(target, sender, names)

    @abstractmethod
    def wake_policy(self, recipient: FrozenRecipient, mentions: frozenset[str]) -> WakePolicy: ...

    @abstractmethod
    def require_publication(
        self,
        message: Message,
        snapshot: RegistryProvenance,
        require_channel: Callable[[str], Channel],
    ) -> None: ...

    @abstractmethod
    def initial_delivery(
        self, sender: str, message: Message, snapshot: RegistrySnapshot,
        require_channel: Callable[[str], Channel],
    ) -> tuple[NamedTarget, Message, list[str]]: ...

    def require_channel(self):
        raise ValueError(f"{self.name!r} is not a channel target.")

    def require_dm_peer(self):
        raise ValueError("A DM page requires a registered peer.")

    def direct_peer(self, snapshot: RegistrySnapshot) -> Thread | None:
        return next((snapshot.threads.get(snapshot.canonical_name(name)) for name in self.direct_names), None)

    def inbound_relations(self, sender: str):
        return (sender, *(name for name in self.destination_names if name not in self.direct_names))

    def response_recipients(self, recipients: tuple[FrozenRecipient, ...]) -> tuple[FrozenRecipient, ...]:
        return tuple(member for member in recipients if not self.direct_names or member.canonical_thread in self.direct_names)

    def response_policy(self, message: MessageInput) -> ResponsePolicy:
        return DirectPolicy.instance()

    def conversation(self, sender: str, viewer: str, canonical: Callable[[str], str]) -> str:
        target = self.canonical_name(canonical)
        return self.reply_target(canonical(sender)) if target == viewer else target

    def wake_audience(self, mentions: frozenset[str]) -> MessageAudience:
        return MessageAudience.MENTIONED if mentions else self.default_audience

    def effective_mentions(self, message: AudiencedMessage):
        return frozenset()

    def addressed(self, threads: tuple[str, ...], body: str) -> str:
        """One recipient is its own audience."""
        return body

    def require_frozen_recipients(self, message: AudiencedMessage):
        """Non-direct targets impose no exact-recipient requirement."""


class ThreadTarget(NamedTarget):
    def automatic_reply_targets(self, sender: str) -> tuple[str, ...]:
        return (self.reply_target(sender),)

    def display_history(self, views: HistoryViews, project: Checkout) -> ConversationHistoryView:
        return views.dm_history(self, worktree=project)

    sigil = "@"
    route: ClassVar[ReplyRoute] = SenderReply()
    kind = "thread"
    incoming_scope = "direct message"

    def require_frozen_recipients(self, message: AudiencedMessage):
        message.role.require_recipients(self, message)

    @property
    def direct_names(self) -> tuple[str, ...]:
        return self.destination_names

    def canonical_name(self, canonical: Callable[[str], str]) -> str:
        return canonical(self.name)

    def require_dm_peer(self):
        return self.name

    def direct(self, goals: Goals, message: Message) -> None:
        """A registered thread holds owner requests; a peer the registry lacks holds none."""
        if self.name in goals.registry:
            goals.direct(self.name, message)

    def history_view(self, viewer: DisplayViewer, require_channel: Callable[[str], Channel],
                     channel: type[ChannelHistory], direct: type[DMHistory]) -> HistoryView:
        return viewer.direct_history(direct, self)

    def history_result(self, require_channel: Callable[[str], Channel],
                       direct: type[HistoryPageResult], messages: tuple[Message, ...],
                       has_older: bool) -> HistoryPageResult:
        return direct.for_target(self.name, messages, has_older)

    def contexts(
        self, declaration: type[CliCommand[CliResult]], channel: ChannelSelection,
    ) -> tuple[SurroundingChannel, ...]:
        return declaration.selection_channels(channel, self.name)

    def available(self, snapshot: RegistrySnapshot) -> bool:
        return self.selected_name(snapshot) in snapshot.threads

    def pending_count(
        self, counts: dict[str, int], snapshot: RegistryProvenance,
        require_channel: Callable[[str], Channel],
    ) -> int:
        return counts.get(snapshot.require(self.selected_name(snapshot)).name, 0)

    def resolve(self, snapshot: RegistrySnapshot, require_channel: Callable[[str], Channel]) -> ResolvedThread:
        return snapshot.resolve_thread(self.selected_name(snapshot))

    def validate(self, message: Message) -> None:
        if message.sender == self.name and message.type.is_conversation_message():
            raise RelationViolationError(f"Thread {message.sender!r} cannot message itself.")
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
        if not set(self.name) <= allowed:
            raise ValueError(f"Message target {self.name!r} is not a thread name or #channel.")

    def wake_policy(self, recipient: FrozenRecipient, mentions: frozenset[str]) -> FullWake:
        return FullWake()

    def require_publication(
        self,
        message: Message,
        snapshot: RegistryProvenance,
        require_channel: Callable[[str], Channel],
    ) -> None:
        target = self.selected_name(snapshot)
        if target not in snapshot.threads:
            raise UnregisteredThreadError(f"Target {self.name!r} is not a registered thread.")
        if target == snapshot.canonical_name(message.sender) and message.type.is_conversation_message():
            raise RelationViolationError(f"Thread {target!r} cannot message itself.")

    def initial_delivery(
        self, sender: str, message: Message, snapshot: RegistrySnapshot,
        require_channel: Callable[[str], Channel],
    ) -> tuple[NamedTarget, Message, list[str]]:
        target = self.selected_name(snapshot)
        recipient = snapshot.require(target)
        snapshot.status(target).require_visible()
        if target == sender:
            raise RelationViolationError("A thread cannot message itself.")
        message, names = recipient.role.initial_delivery(message, target)
        return ThreadTarget(target), message, names

    def display_scope(
        self, viewer: str, snapshot: RegistryProvenance,
        require_channel: Callable[[str], Channel], direct_scope: type[DMDisplayScope],
    ) -> MessageDisplayScope:
        return direct_scope.capture(
            viewer, snapshot.require(self.selected_name(snapshot)).name, snapshot)

    def conversation_key(self, sender: str, snapshot: RegistryProvenance, channel: type[ChannelConversation], direct: type[DMConversation]) -> Conversation:
        names = sorted({snapshot.canonical_name(sender), self.selected_name(snapshot)})
        return direct(participants=tuple(snapshot.participant(name).incarnation for name in names))

    def changed_threads(
        self, names: Iterable[str], snapshot: RegistryProvenance,
        catalog: CatalogDocument, activity: ActivityClocks,
    ) -> tuple[ThreadChange, ...]:
        return (ContentChange(self.selected_name(snapshot)),)

    def mark_view_read(self, views: HistoryViews, worktree: Checkout) -> None:
        views.mark_thread_user_read(self.name, worktree=worktree)


@dataclass(frozen=True)
class IncarnationThreadTarget(ThreadTarget):
    """A retained DM addresses its original birth, never a name's successor."""

    name: str = field(init=False)
    incarnation: ThreadIncarnation

    def __post_init__(self) -> None:
        self.incarnation.require_recorded()
        object.__setattr__(self, "name", self.incarnation.name)

    def selected_name(self, snapshot: RegistryProvenance) -> str:
        if not self.incarnation.current(snapshot):
            raise UnregisteredThreadError(
                f"Original DM peer {self.name!r} is no longer registered.")
        return super().selected_name(snapshot)

    def require_publication(self, message: Message, snapshot: RegistryProvenance,
                            require_channel: Callable[[str], Channel]) -> None:
        try:
            super().require_publication(message, snapshot, require_channel)
        except UnregisteredThreadError as error:
            raise HumanSendRefusedError(error) from error


class ChannelTarget(NamedTarget):
    def display_history(self, views: HistoryViews, project: Checkout) -> ConversationHistoryView:
        return views.channel_history(self, worktree=project)

    @property
    def needs_channel_state(self) -> bool:
        return True

    def accepts_batch(self, seq: int) -> bool:
        return seq > 0
    kind = "channel"
    default_audience = MessageAudience.COLLECTIVE

    sigil = "#"
    route: ClassVar[ReplyRoute] = ChannelReply()

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

    def history_result(self, require_channel: Callable[[str], Channel],
                       direct: type[HistoryPageResult], messages: tuple[Message, ...],
                       has_older: bool) -> HistoryPageResult:
        result_type = require_channel(self.name).history_result_type
        return result_type.for_target(self.name, messages, has_older)

    def pending_count(
        self, counts: dict[str, int], snapshot: RegistryProvenance,
        require_channel: Callable[[str], Channel],
    ) -> int:
        return require_channel(self.name).pending_count(counts)

    def resolve(self, snapshot: RegistrySnapshot, require_channel: Callable[[str], Channel]) -> ResolvedChannel:
        return snapshot.resolve_channel(require_channel(self.name))

    def validate(self, sender: Message | None = None):
        from agent_comms.messaging.channels import BuiltinChannelView

        if self.name not in BuiltinChannelView.addresses():
            Tag.validate(self.name[1:], label=f"Channel {self.name!r}")

    def response_policy(self, message: MessageInput) -> ResponsePolicy:
        if message.mentions:
            return MentionedPolicy.instance()
        return message.sender_role.channel_policy()

    def effective_mentions(self, message: AudiencedMessage):
        return frozenset(mention.thread for mention in message.mentions).intersection(
            message.canonical_members
        )

    def addressed(self, threads: tuple[str, ...], body: str) -> str:
        """Keep collective posts unchanged; prefix mentions for a named audience."""
        return " ".join((*(f"@{thread}" for thread in threads), body))

    def wake_policy(self, recipient: FrozenRecipient, mentions: frozenset[str]) -> WakePolicy:
        if recipient.canonical_thread in mentions:
            return FullWake()
        return BoundedTriageWake()

    def require_publication(
        self,
        message: Message,
        snapshot: RegistryProvenance,
        require_channel: Callable[[str], Channel],
    ) -> None:
        require_channel(self.name).require_routable()

    def initial_delivery(
        self, sender: str, message: Message, snapshot: RegistrySnapshot,
        require_channel: Callable[[str], Channel],
    ) -> tuple[NamedTarget, Message, list[str]]:
        channel = require_channel(self.name)
        names = [recipient.name for thread in snapshot.channel_audience()
                 for recipient in thread.channel_recipients()
                 if recipient.name != sender and channel.matches(recipient.tags)]
        outside = sorted({mention.thread for mention in snapshot.mentions(message.body)}
                         - {sender, *names})
        if outside:
            raise MentionOutsideChannelError(
                f"{self.name} does not deliver to {', '.join(f'@{name}' for name in outside)}, "
                "so this post would never reach them. DM them.")
        return self, message, names

    def display_scope(
        self, viewer: str, snapshot: RegistryProvenance,
        require_channel: Callable[[str], Channel], direct_scope: type[DMDisplayScope],
    ) -> MessageDisplayScope:
        return require_channel(self.name).history_scope()

    def conversation_key(self, sender: str, snapshot: RegistryProvenance, channel: type[ChannelConversation], direct: type[DMConversation]) -> Conversation:
        return channel(target=self.name)

    def changed_threads(
        self, names: Iterable[str], snapshot: RegistryProvenance,
        catalog: CatalogDocument, activity: ActivityClocks,
    ) -> tuple[ThreadChange, ...]:
        channel = catalog.require_channel(self.name, activity)
        return tuple(MetadataChange(name) for name in names
                     if (thread := snapshot.threads.get(name)) is not None
                     and channel.matches(thread.tags))

    def mark_view_read(self, views: HistoryViews, worktree: Checkout) -> None:
        from agent_comms.presentation import ReadAll

        views.presentation.mark_channel_read(
            self.name, viewer=views.messaging.user_identity(worktree).name, mark=ReadAll(),
        )


@dataclass(frozen=True)
class ProjectTarget(CommandTarget):
    project: Checkout

    def display_name(self) -> str:
        return str(self.project)

    @property
    def name(self) -> str:
        return str(self.project)

    @property
    def destination_names(self) -> tuple[str, ...]:
        return ()

    def resolve(self, snapshot: RegistrySnapshot, require_channel: Callable[[str], Channel]) -> ResolvedProject:
        return ResolvedProject(self.project)


_TAG_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789-_")


@dataclass(frozen=True, slots=True)
class Tag(ProjectionSource):
    name: str

    def __post_init__(self) -> None:
        self.validate(self.name)

    @staticmethod
    def validate(name: str, *, label: str = "Tags") -> None:
        from agent_comms.messaging.channels import BuiltinChannelView

        if (not name or not set(name) <= _TAG_CHARS
                or ChannelTarget.normalize_name(name) in BuiltinChannelView.addresses()):
            raise ValueError(f"{label} must be lowercase alphanumeric with hyphens/underscores; built-in channel names are reserved.")


class DeclaredTag(DeclaredFamily, affix="Tag"):
    """A tag every bus has by declaration: its name is its class name, and its channel
    resolves before any thread carries it. Membership is still by tag."""

    @classmethod
    def channel(cls) -> str:
        return ChannelTarget.normalize_name(cls.declared_name)

    @classmethod
    def inherited(cls) -> bool:
        """Whether a child declared under a carrier starts with this tag: an audience is."""
        return True

    @classmethod
    def for_child(cls, parent: frozenset[str]) -> frozenset[str]:
        """A child's first tags: its parent's created tags and every inherited declared one."""
        return parent - {member.declared_name for member in cls.members_with(cls)
                         if not member.inherited()}


class CommsTag(DeclaredTag):
    """The channel the fleet coordinates on: installer reports, Core's strike announcements
    and the teammate's posts address it."""


class TeammatesTag(DeclaredTag, declared_name="teammates"):
    """A coordination channel identity, never a permission or execution role."""
