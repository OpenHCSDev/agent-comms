"""Named destinations own route behavior; strings are decoded at the boundary."""
from __future__ import annotations

from dataclasses import dataclass, replace
from abc import ABC, abstractmethod
from typing import ClassVar
from enum import StrEnum

from .errors import RelationViolationError, UnregisteredThreadError
from .response_policy import CollectivePolicy, DirectPolicy, InformationalPolicy, MentionedOnlyPolicy
from .wake_policy import BoundedTriageWake, FullWake, MessageAudience


class BuiltinChannel(StrEnum):
    ANY = "#any"
    NONE = "#none"
    ALL = "#all"

    @classmethod
    def lookup(cls, name: str) -> BuiltinChannel | None:
        channel = cls._value2member_map_.get(name)
        # Value aliases are not declared channel targets.
        return channel if channel is not None and name == channel.value else None

    @classmethod
    def exact_stored_target(cls, name: str) -> bool:
        channel = cls.lookup(name)
        return channel is None or not channel.aggregate

    @property
    def aggregate(self) -> bool:
        return self is self.ANY

    def matches(self, tags: frozenset[str]) -> bool:
        return self is not self.NONE or not tags

    @property
    def history_targets(self) -> frozenset[str] | None:
        return None if self.aggregate else frozenset((self.value,))


class CommandTarget(ABC):
    """A command destination owns its binding and catalog participation."""
    needs_catalog: ClassVar[bool] = False

    @staticmethod
    def named(name: str | CommandTarget) -> CommandTarget:
        if isinstance(name, CommandTarget):
            return name
        if not isinstance(name, str) or not name:
            raise ValueError("Select a named target")
        return ChannelTarget(name) if name.startswith("#") else ThreadTarget(name)

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
    def bindings(self, declaration, comms, channel, snapshot, catalog): ...

    def contexts(self, declaration, channel):
        return (None,)


@dataclass(frozen=True)
class NamedTarget(CommandTarget):
    name: str
    batchable = False
    direct_names = ()
    guidance_scope = ""
    default_audience = MessageAudience.DIRECT

    @property
    def names(self) -> tuple[str, ...]:
        return (self.name,)

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

    def require_frozen_recipients(self, message, audience):
        size = len(audience.recipients)
        if not self.direct_names or (message.notice and size == 0):
            return
        if size != len(self.direct_names):
            raise ValueError("a direct message must have exactly one frozen eligible recipient.")
        if tuple(member.canonical_thread for member in audience.recipients) != self.direct_names:
            raise ValueError("direct frozen recipient must match the exact stored target.")


class ThreadTarget(NamedTarget):
    kind = "thread"
    incoming_scope = "direct message"

    @property
    def direct_names(self):
        return self.names

    def canonical_name(self, canonical):
        return canonical(self.name)

    def require_dm_peer(self):
        return self.name

    def contexts(self, declaration, channel):
        return declaration.selection_channels(channel, self.name)

    def available(self, snapshot) -> bool:
        return self.canonical_name(snapshot.canonical_name) in snapshot.threads

    def pending_count(self, counts, snapshot, catalog):
        return counts.get(snapshot.require(self.name).name, 0)

    def bindings(self, declaration, comms, channel, snapshot, catalog):
        thread = snapshot.require(self.name)
        return declaration.thread_bindings(comms, thread, snapshot.status(thread.name),
                                           channel, catalog=catalog)

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

    def require_publication(self, message, snapshot, catalog):
        target = self.canonical_name(snapshot.canonical_name)
        if target not in snapshot.threads:
            raise UnregisteredThreadError(f"Target {self.name!r} is not a registered thread.")
        if target == snapshot.canonical_name(message.sender) and message.type.conversation_message:
            raise RelationViolationError(f"Thread {target!r} cannot message itself.")

    def initial_delivery(self, sender, message, snapshot, catalog):
        target = self.canonical_name(snapshot.canonical_name)
        recipient = snapshot.threads.get(target)
        if recipient is not None and snapshot.statuses[target].visible:
            if target == sender:
                raise RelationViolationError("A thread cannot message itself.")
            if recipient.role.executable:
                return ThreadTarget(target), message, [target]
            return ThreadTarget(target), replace(message, notice=True), []
        raise RelationViolationError("Initial direct target must be visible.")

    def display_scope(self, viewer, snapshot, catalog, channel_scope, direct_scope):
        return direct_scope.capture(viewer, snapshot.require(self.name).name, snapshot)

    def conversation_key(self, sender, snapshot, constructor):
        names = sorted({snapshot.canonical_name(sender), self.canonical_name(snapshot.canonical_name)})
        return constructor(participants=tuple(snapshot.participant(name).incarnation for name in names))

    def changed_threads(self, names, snapshot, catalog):
        return (self.canonical_name(snapshot.canonical_name),)

    def mark_view_read(self, views, worktree):
        views.mark_thread_view_read(self.name, worktree=worktree)


class ChannelTarget(NamedTarget):
    needs_catalog = True
    batchable = True
    kind = "channel"
    default_audience = MessageAudience.COLLECTIVE

    @property
    def incoming_scope(self):
        return self.name

    @property
    def guidance_scope(self):
        return f"; delivery and history remain {self.name}"

    def require_channel(self):
        return self

    def pending_count(self, counts, snapshot, catalog):
        return catalog.resolve(self.name).pending_count(counts)

    def bindings(self, declaration, comms, channel, snapshot, catalog):
        return declaration.channel_bindings(comms, catalog.resolve(self.name), snapshot=snapshot)

    def validate(self, sender=None):
        if BuiltinChannel.lookup(self.name) is None:
            Tag.validate(self.name[1:], label=f"Channel {self.name!r}")

    def response_policy(self, message):
        if message.mentions:
            return MentionedOnlyPolicy.instance()
        return InformationalPolicy.instance() if message.sender_role.executable else CollectivePolicy.instance()

    def reply_target(self, sender):
        return self.name

    def effective_mentions(self, message, audience):
        return frozenset(mention.thread for mention in message.mentions).intersection(audience.canonical_members)

    def wake_policy(self, recipient, mentions):
        if mentions:
            return FullWake() if recipient.canonical_thread in mentions else None
        return BoundedTriageWake()

    def require_publication(self, message, snapshot, catalog):
        catalog().resolve(self.name).require_routable()

    def initial_delivery(self, sender, message, snapshot, catalog):
        channel = catalog.resolve(self.name)
        names = []
        for name, thread in snapshot.threads.items():
            if name != sender and thread.role.executable:
                if snapshot.statuses[name].visible and channel.matches(thread.tags):
                    names.append(name)
        return self, message, names

    def display_scope(self, viewer, snapshot, catalog, channel_scope, direct_scope):
        return channel_scope(self.name, catalog.resolve(self.name).history_targets)

    def conversation_key(self, sender, snapshot, constructor):
        return constructor(target=self.name)

    def changed_threads(self, names, snapshot, catalog):
        channel = catalog.resolve(self.name)
        return tuple(name for name in names if (thread := snapshot.threads.get(name)) is not None
                     and channel.matches(thread.tags))

    def mark_view_read(self, views, worktree):
        views.mark_channel_view_read(self.name, worktree=worktree)


@dataclass(frozen=True)
class ProjectTarget(CommandTarget):
    project: str

    @property
    def name(self) -> str:
        return self.project

    @property
    def names(self) -> tuple[str, ...]:
        return ()

    def bindings(self, declaration, comms, channel, snapshot, catalog):
        return declaration.project_bindings(comms, self.project)


_TAG_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789-_")


@dataclass(frozen=True, slots=True)
class Tag:
    name: str

    def __post_init__(self) -> None:
        self.validate(self.name)

    @staticmethod
    def validate(name: str, *, label: str = "Tags") -> None:
        if not name or not set(name) <= _TAG_CHARS or BuiltinChannel.lookup(f"#{name}"):
            raise ValueError(f"{label} must be lowercase alphanumeric with hyphens/underscores; built-in channel names are reserved.")
