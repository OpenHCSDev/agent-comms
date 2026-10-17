"""Named destinations own route behavior; strings are decoded at the boundary."""
from __future__ import annotations

from dataclasses import dataclass, replace
from abc import ABC, abstractmethod
from typing import ClassVar
from enum import StrEnum

from .errors import RelationViolationError, UnregisteredThreadError
from .response_policy import CollectivePolicy, DirectPolicy, InformationalPolicy, MentionedOnlyPolicy
from .wake_policy import BoundedTriageWake, FullWake, MessageAudience, PassiveWake


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
        return declaration.selection_channels(channel, self.name)


@dataclass(frozen=True)
class NamedTarget(CommandTarget):
    name: str
    batchable = False
    direct_names = ()
    guidance_scope = ""

    @property
    def names(self) -> tuple[str, ...]:
        return (self.name,)

    def canonical_name(self, snapshot):
        return self.name

    def require_channel(self):
        raise ValueError(f"{self.name!r} is not a channel target.")

    def require_dm_peer(self):
        raise ValueError("A DM page requires a registered peer.")

    def direct_peer(self, snapshot):
        return None

    def inbound_relations(self, sender):
        return (sender,)

    def response_recipients(self, recipients):
        return recipients

    def response_policy(self, message):
        if message.notice or message.membership is not None:
            return InformationalPolicy.instance()
        return self.ordinary_response_policy(message)

    def effective_mentions(self, message, audience):
        return frozenset()

    def require_frozen_recipients(self, message, audience):
        pass

    def wake_policy(self, message, recipient, control, mentions):
        if message.notice or message.membership is not None or control.passive:
            return PassiveWake()
        return self.ordinary_wake_policy(recipient, mentions)


class ThreadTarget(NamedTarget):
    kind = "thread"
    incoming_scope = "direct message"

    @property
    def direct_names(self):
        return (self.name,)

    def canonical_name(self, snapshot):
        return snapshot.canonical_name(self.name)

    def require_dm_peer(self):
        return self.name

    def direct_peer(self, snapshot):
        return snapshot.threads.get(self.canonical_name(snapshot))

    def available(self, snapshot) -> bool:
        return self.canonical_name(snapshot) in snapshot.threads

    def pending_count(self, counts, snapshot, catalog):
        return counts.get(snapshot.require(self.name).name, 0)

    def bindings(self, declaration, comms, channel, snapshot, catalog):
        thread = snapshot.require(self.name)
        return declaration.thread_bindings(comms, thread, snapshot.status(thread.name),
                                           channel, catalog=catalog)

    def validate(self, sender=None):
        if sender == self.name:
            raise RelationViolationError(f"Thread {sender!r} cannot message itself.")
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
        if not set(self.name) <= allowed:
            raise ValueError(f"Message target {self.name!r} is not a thread name or #channel.")

    def ordinary_response_policy(self, message):
        return DirectPolicy.instance()

    def starts_turn_for(self, message, name, aliases):
        return self.response_policy(message).starts_turn

    def reply_target(self, sender):
        return sender

    def response_recipients(self, recipients):
        return tuple(member for member in recipients if member.canonical_thread == self.name)

    def require_frozen_recipients(self, message, audience):
        if message.notice and not audience.recipients:
            return
        if len(audience.recipients) != 1:
            raise ValueError("a direct message must have exactly one frozen eligible recipient.")
        if audience.recipients[0].canonical_thread != self.name:
            raise ValueError("direct frozen recipient must match the exact stored target.")

    def wake_audience(self, mentions):
        return MessageAudience.DIRECT

    def ordinary_wake_policy(self, recipient, mentions):
        return FullWake()

    def require_publication(self, sender, snapshot, catalog):
        target = self.canonical_name(snapshot)
        if target not in snapshot.threads:
            raise UnregisteredThreadError(f"Target {self.name!r} is not a registered thread.")
        if target == sender:
            raise RelationViolationError(f"Thread {sender!r} cannot message itself.")

    def initial_delivery(self, sender, message, snapshot, catalog):
        target = self.canonical_name(snapshot)
        recipient = snapshot.threads.get(target)
        if recipient is None or not snapshot.statuses[target].visible:
            raise RelationViolationError("Initial direct target must be visible.")
        if target == sender:
            raise RelationViolationError("A thread cannot message itself.")
        if not recipient.role.executable:
            return ThreadTarget(target), replace(message, notice=True), []
        return ThreadTarget(target), message, [target]

    def display_scope(self, viewer, snapshot, catalog, channel_scope, direct_scope):
        return direct_scope.capture(viewer, snapshot.require(self.name).name, snapshot)

    def conversation(self, sender, viewer, canonical):
        sender, target = canonical(sender), canonical(self.name)
        return sender if target == viewer else target

    def conversation_key(self, sender, snapshot, constructor):
        names = sorted({snapshot.canonical_name(sender), self.canonical_name(snapshot)})
        return constructor(participants=tuple(snapshot.participant(name).incarnation for name in names))

    def changed_threads(self, names, snapshot, catalog):
        return (self.canonical_name(snapshot),)

    def mark_view_read(self, views, worktree):
        views.mark_thread_view_read(self.name, worktree=worktree)


class ChannelTarget(NamedTarget):
    needs_catalog = True
    batchable = True
    kind = "channel"

    @property
    def incoming_scope(self):
        return self.name

    @property
    def guidance_scope(self):
        return f"; delivery and history remain {self.name}"

    def require_channel(self):
        return self

    def history_targets(self, catalog):
        return catalog.resolve(self.name).history_targets

    def pending_count(self, counts, snapshot, catalog):
        return catalog.resolve(self.name).pending_count(counts)

    def bindings(self, declaration, comms, channel, snapshot, catalog):
        return declaration.channel_bindings(comms, catalog.resolve(self.name), snapshot=snapshot)

    def validate(self, sender=None):
        if BuiltinChannel.lookup(self.name) is None:
            Tag(self.name[1:])

    def ordinary_response_policy(self, message):
        if message.mentions:
            return MentionedOnlyPolicy.instance()
        return CollectivePolicy.instance() if not message.sender_role.executable else InformationalPolicy.instance()

    def starts_turn_for(self, message, name, aliases):
        return name in self.response_policy(message).recipients(message, (name,), aliases=aliases)

    def reply_target(self, sender):
        return self.name

    def effective_mentions(self, message, audience):
        return frozenset(mention.thread for mention in message.mentions).intersection(audience.canonical_members)

    def wake_audience(self, mentions):
        return MessageAudience.MENTIONED if mentions else MessageAudience.COLLECTIVE

    def ordinary_wake_policy(self, recipient, mentions):
        if mentions:
            return FullWake() if recipient.canonical_thread in mentions else None
        return BoundedTriageWake()

    def require_publication(self, sender, snapshot, catalog):
        channel = catalog().resolve(self.name)
        if channel.aggregate or channel.view is not None:
            raise RelationViolationError(f"View {self.name!r} is a projection, not a routable target.")

    def initial_delivery(self, sender, message, snapshot, catalog):
        channel = catalog.resolve(self.name)
        names = []
        for name, thread in snapshot.threads.items():
            if name == sender or not thread.role.executable:
                continue
            if snapshot.statuses[name].visible and channel.matches(thread.tags):
                names.append(name)
        return self, message, names

    def display_scope(self, viewer, snapshot, catalog, channel_scope, direct_scope):
        return channel_scope(self.name, self.history_targets(catalog))

    def conversation(self, sender, viewer, canonical):
        return self.name

    def conversation_key(self, sender, snapshot, constructor):
        return constructor(target=self.name)

    def inbound_relations(self, sender):
        return sender, self.name

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

    def contexts(self, declaration, channel):
        return (None,)

    def bindings(self, declaration, comms, channel, snapshot, catalog):
        return declaration.project_bindings(comms, self.project)


_TAG_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789-_")


@dataclass(frozen=True, slots=True)
class Tag:
    name: str

    def __post_init__(self) -> None:
        if (
            not self.name
            or not set(self.name) <= _TAG_CHARS
            or BuiltinChannel.lookup(f"#{self.name}")
        ):
            raise ValueError(
                "Tags must be lowercase alphanumeric with hyphens/underscores; "
                "built-in channel names are reserved."
            )
