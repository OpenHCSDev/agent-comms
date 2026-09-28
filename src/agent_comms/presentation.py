"""Display projections depend on wire/read authorities, never the reverse."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .activity import Activity
from .bus_activity_index import ChannelActivity
from .bus_display_index import BusDisplayIndex
from .channels import Channel
from .display_order import ChannelSort
from .goal_presentation import GoalExecution
from .mentions import MentionCandidate
from .messages import Message
from .read_basis import ChannelDisplayScope, ViewUnread
from .read_ledger import ReadLedger
from .runtime_info import AgentRuntimeInfo
from .store_files import file_revision
from .thread_presentation import ThreadPresentation
from .thread_status import ThreadStatus
from .threads import Thread

if TYPE_CHECKING:
    from .messages import Message


class BusPresentation:
    def __init__(self, path: Path):
        self._path = path
        self._view_unread_cache: dict[str, ViewUnread] = {}
        self._display_activity_revision: tuple | None = None
        self._display_activity: dict[str, ChannelActivity] = {}
        self._display_activity_verified = False

    def display_view_metrics(
        self,
        records: Iterator[tuple[Message, int]],
        scopes: tuple[ChannelDisplayScope, ...],
        activity_scopes: tuple[ChannelDisplayScope, ...],
        viewer: str,
        viewer_names: frozenset[str],
        bus_revision: tuple[int, int, int, int] | None,
    ) -> tuple[Mapping[str, ChannelActivity], Mapping[str, int]]:
        """Activity and human unread from one validated, already-opened bus boundary.

        The caller supplies a canonical viewer and alias closure from the same
        captured registry as the scopes. Never recanonicalize during this scan.
        Caches may be reused or published only for an unchanged bus revision.
        """
        activity_key = (bus_revision, activity_scopes)
        unread_key = (bus_revision, viewer_names)
        cached_unread = self._view_unread_cache.get(viewer)
        if (
            bus_revision is not None
            and self._display_activity_revision == activity_key
            and self._display_activity_verified
            and cached_unread is not None
            and cached_unread.revision == unread_key
            and cached_unread.verified_display_boundary
            and cached_unread.scopes == scopes
        ):
            return dict(self._display_activity), dict(cached_unread.counts)

        def scope_key(scope: ChannelDisplayScope) -> list[object]:
            return [
                scope.channel,
                sorted(scope.targets) if scope.targets is not None else None,
                scope.any_mode,
                sorted(scope.participant_names),
                sorted(scope.seen_sequences),
            ]

        def apply(record: Mapping, metrics: tuple[dict, dict]) -> None:
            message = Message.from_wire(record)
            clocks, unread = metrics
            for scope in activity_scopes:
                if scope.includes(message):
                    last_message, last_user = clocks[scope.channel]
                    clocks[scope.channel] = (
                        max(last_message, message.timestamp),
                        (
                            max(last_user, message.timestamp)
                            if ReadLedger.human(message.sender_role)
                            else last_user
                        ),
                    )
            if message.sender not in viewer_names:
                for scope in scopes:
                    if scope.unread(message):
                        unread[scope.channel] += 1

        initial = (
            {scope.channel: (0.0, 0.0) for scope in activity_scopes},
            dict.fromkeys((scope.channel for scope in scopes), 0),
        )
        projected = BusDisplayIndex(self._path, viewer).snapshot(
            bus_revision,
            [
                [scope_key(scope) for scope in scopes],
                [scope_key(scope) for scope in activity_scopes],
                sorted(viewer_names),
            ],
            initial,
            apply,
        )
        if projected is not None:
            activity = {name: ChannelActivity(*clocks) for name, clocks in projected[0].items()}
            counts = projected[1]
        else:
            activity = {scope.channel: ChannelActivity() for scope in activity_scopes}
            counts = dict.fromkeys((scope.channel for scope in scopes), 0)
            for message, _ in records:
                for scope in activity_scopes:
                    if scope.includes(message):
                        activity[scope.channel] = activity[scope.channel].observe(message)
                if message.sender in viewer_names:
                    continue
                for scope in scopes:
                    if scope.unread(message):
                        counts[scope.channel] += 1
        if bus_revision is not None and file_revision(self._path) == bus_revision:
            self._display_activity = activity
            self._display_activity_revision = activity_key
            self._display_activity_verified = True
            self._view_unread_cache[viewer] = ViewUnread(
                unread_key, scopes, counts, verified_display_boundary=True
            )
        return activity, counts


@dataclass(frozen=True, slots=True)
class ChannelView:
    channel: Channel
    members: tuple[str, ...]
    last_activity: float = 0
    last_user_input: float = 0
    pinned_members: frozenset[str] = frozenset()

    def to_wire(self) -> dict[str, object]:
        return {
            **self.channel.to_wire(),
            "members": list(self.members),
            "last_activity": self.last_activity,
            "last_user_input": self.last_user_input,
            "pinned_members": sorted(self.pinned_members),
        }


@dataclass(frozen=True, slots=True)
class ThreadView:
    thread: Thread
    status: ThreadStatus
    activity: Activity
    runtime: AgentRuntimeInfo | None
    last_seen: float
    goal_execution: GoalExecution | None = None

    @property
    def presentation(self) -> ThreadPresentation:
        """One declaration-owned interpretation for every thread view."""
        if (
            self.status.active
            and not self.activity.state.busy
            and self.goal_execution is not None
            and self.goal_execution.state.view.waiting
        ):
            return self.goal_execution.presentation(self.thread.title or self.thread.name)
        return self.status.presentation(self.thread.title or self.thread.name, self.activity)

    def to_wire(self) -> dict[str, object]:
        return {
            **self.thread.to_wire(),
            "status": self.status.declared_name,
            "is_fork": self.thread.is_fork,
            "resumable": bool(self.thread.session_file),
            "last_seen": self.last_seen,
            "last_activity": self.activity.timestamp,
            "activity": self.activity.state.value,
            "activity_detail": self.activity.detail,
            "goal_execution": asdict(self.goal_execution) if self.goal_execution else None,
            "model": self.runtime.model if self.runtime else self.thread.model,
            "session_name": self.runtime.session_name if self.runtime else None,
            "context_used": self.runtime.context_used if self.runtime else None,
            "context_size": self.runtime.context_size if self.runtime else None,
            "context_percent": self.runtime.context_percent if self.runtime else None,
        }


@dataclass(frozen=True, slots=True)
class CoordinationSnapshot:
    threads: tuple[ThreadView, ...]
    channels: tuple[ChannelView, ...]
    unread: Mapping[str, int]
    last_sent: Mapping[str, float]
    channel_unread: Mapping[str, int] = field(default_factory=dict)
    channel_order: ChannelSort = ChannelSort.NAME
    thread_unread: Mapping[str, int] = field(default_factory=dict)
    show_stopped: bool = True
    show_archived: bool = False
    read_marker_notice: str | None = None

    def participants(self, channel: str) -> tuple[ThreadView, ...]:
        view = next((view for view in self.channels if view.channel.name == channel), None)
        if view is None:
            return ()
        people = {
            person.thread.name: person
            for person in self.threads
            if person.status.active and person.thread.executing
        }
        return tuple(people[name] for name in view.members if name in people)

    def mention_candidates(self, channel: str) -> tuple[MentionCandidate, ...]:
        view = next((view for view in self.channels if view.channel.name == channel), None)
        members = frozenset(view.members) if view else frozenset()
        return tuple(
            sorted(
                (
                    MentionCandidate(person.thread.name, person.presentation.title)
                    for person in self.threads
                    if person.thread.name in members
                ),
                key=lambda candidate: candidate.name.casefold(),
            )
        )


@dataclass(frozen=True, slots=True)
class WireRevision:
    files: tuple[tuple[int, int, int, int] | None, ...]
    expiry_tick: int
