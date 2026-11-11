"""Live dependency reply review and recovery above the original wait records."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

from agent_comms.planning.goal_wait_target import GoalWaitTarget
from agent_comms.registry.agent_activity import AgentActivity
from agent_comms.errors import UnregisteredThreadError
from agent_comms.execution.native_input_owner import RegistryOwner
from agent_comms.execution.turn_lease import TurnCompletion, TurnLease
from agent_comms.input_attempt import StoredInput, UnresolvedInput
from agent_comms.execution.input_key import InputKey
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.messaging.published_bus import PublishedBus
from agent_comms.nominal.field_codec import GoalId
from agent_comms.planning.goal_waits import (
    ExcludedInput,
    GoalWait,
    GoalWaits,
    NotADirectReplyFromDeclaredDependenciesExclusion,
    OwnerInputWithoutBusSequenceExclusion,
    StandbyReview,
)
from agent_comms.registry.registration import Registration
from agent_comms.registry.registry_document import RegistrySnapshot
from agent_comms.registry.registry_store import RegistryEdit
from agent_comms.registry.thread_identity import RecordedThreadIncarnation
from agent_comms.registry.threads import Thread


@dataclass(frozen=True, slots=True)
class GoalWaitReview:
    """The existing registry, turn and wait observed before reading reply history."""

    snapshot: RegistrySnapshot
    owner: Thread
    wait: GoalWait
    turn: TurnLease

    @staticmethod
    def capture(goals: GoalWaitRecovery, incarnation: RecordedThreadIncarnation) -> GoalWaitReview:
        with goals.registry.store.current_snapshot() as cut:
            snapshot = cut.snapshot
            for owner in snapshot.threads_for(incarnation):
                return GoalWaitReview(
                    snapshot=snapshot, owner=owner, wait=goals.waits.resolve(owner.goal), turn=owner.turn_lease,
                )
            raise UnregisteredThreadError("The reviewed thread incarnation was removed.")

    def current_for(self, goals: GoalWaitRecovery) -> bool:
        """Caller holds the publication lock; no reply decoding belongs in this check."""
        snapshot = goals.registry.snapshot()
        return any(
            self._current_goal(owner)
            and owner.turn_lease == self.turn
            and goals.waits.resolve(owner.goal) == self.wait
            for owner in snapshot.threads_for(self.owner.incarnation.require_recorded())
            if snapshot.same_thread(owner, self.snapshot, self.owner)
        )

    def _current_goal(self, owner: Thread) -> bool:
        """A retained wait follows edits of its active goal, not a frozen goal value."""
        return any(
            self.wait.current_for(goal, owner.incarnation.require_recorded())
            for goal in owner.goal.active()
        )

    def require_current(self, goals: GoalWaitRecovery, owner: Thread) -> None:
        if owner.incarnation != self.owner.incarnation or not self.current_for(goals):
            raise ValueError(
                "Goal, dependency or wait changed during reply review; inspect it again."
            )

    def publish_unanswered(
        self, goals: GoalWaitRecovery, publish: Callable[[RegistryEdit], tuple[str, ...]],
    ) -> tuple[str, ...]:
        """Publish once through the checked edit after an unlocked read proves silence."""
        owner = self.owner.incarnation.require_recorded()
        review = self
        while True:
            with goals.bus.bus.reading() as source:
                replied = self.wait.has_reply(source, owner)
                through_seq = source.revision()
            if replied:
                return ()
            with goals.registry.store.editing() as edit:
                if not review.current_for(goals):
                    try:
                        review = GoalWaitReview.capture(goals, owner)
                    except UnregisteredThreadError:
                        return ()
                    if review.wait != self.wait or not review._current_goal(review.owner):
                        return ()
                    # A new owner/turn fence needs a fresh silence read for this
                    # same wait. Re-evaluate here; a terminal callback runs once.
                    continue
                # The existing indexed sequence query reads at most one metadata row.
                # Any new candidate needs another unlocked policy read, not a stale release.
                if goals.bus.bus.received_after(owner.lookup, through_seq):
                    continue
                return publish(edit)

    @property
    def goal_id(self) -> GoalId:
        return self.owner.goal.require().id

    @property
    def owners(self) -> frozenset[str]:
        return self.snapshot.names_for(self.owner.name)


@dataclass(frozen=True, slots=True)
class GoalInputReview(GoalWaitReview):
    """Reply eligibility and the exact bus cut inspected before publication."""

    through_seq: int
    targets: tuple[GoalWaitTarget, ...]
    unknown: tuple[StoredInput, ...]
    eligible_keys: frozenset[InputKey]

    def _current_goal(self, owner: Thread) -> bool:
        """Publishing an input review still requires the exact prepared goal."""
        return owner.goal == self.owner.goal

    def current_for(self, goals: GoalWaitRecovery) -> bool:
        if not super(GoalInputReview, self).current_for(goals):
            return False
        snapshot = goals.registry.snapshot()
        before = self.snapshot
        return all(any(
            snapshot.same_thread(actual, before, before.require(target.incarnation.name))
            for actual in snapshot.threads_for(target.incarnation)
        ) for target in self.targets)

    def standby_review(self) -> StandbyReview:
        eligible: list[UnresolvedInput] = []
        reviewed: list[UnresolvedInput] = []
        excluded: list[ExcludedInput] = []
        for row in self.unknown:
            if row.key not in self.eligible_keys:
                excluded.append(ExcludedInput.excluded(row, reason=(
                    OwnerInputWithoutBusSequenceExclusion if row.sequence is None
                    else NotADirectReplyFromDeclaredDependenciesExclusion)))
            elif row.reviewed_for_goal(self.goal_id):
                reviewed.append(UnresolvedInput.of(row))
            else:
                eligible.append(UnresolvedInput.of(row))
        return StandbyReview(
            self.goal_id,
            tuple(target.incarnation.name for target in self.targets),
            tuple(item.input_id for item in eligible),
            tuple(eligible),
            tuple(reviewed),
            tuple(excluded),
        )


class GoalWaitRecovery:
    """Release and consume dependency waits against their original goal and input state."""

    registry: Registration
    bus: PublishedBus
    agents: AgentActivity
    waits: GoalWaits

    def goal_wait(self, name: str) -> GoalWait:
        goal = self.registry.require(name).goal
        return self.waits.resolve(goal)

    def recover_replied_goal_wait(self, name: str) -> tuple[str, ...]:
        """Release a standby whose declared target sent a qualifying direct reply."""
        try:
            incarnation = self.registry.require(name).incarnation.require_recorded()
            review = GoalWaitReview.capture(self, incarnation)
        except UnregisteredThreadError:
            return ()
        owner = review.owner
        if not any(
            review.wait.current_for(goal, owner.incarnation.require_recorded())
            for goal in owner.goal.active()
        ):
            return ()
        owner_incarnation = owner.incarnation.require_recorded()
        with self.bus.bus.reading() as source:
            if not review.wait.has_reply(source, owner_incarnation):
                return ()
        def release(edit: RegistryEdit) -> tuple[str, ...]:
            snapshot = edit.original.snapshot
            actual_owner, = snapshot.threads_for(owner_incarnation)
            note = (
                "A declared dependency sent a qualifying direct reply. "
                "Standby was released."
            )
            if self._release_reviewed_wait(edit, review, note):
                return (actual_owner.name,)
            return ()
        with self.registry.store.editing() as edit:
            if not review.current_for(self):
                return ()
            return release(edit)

    def recover_closed_goal_wait(self, name: str) -> tuple[str, ...]:
        """Read silence outside the registry, then release only the observed wait."""
        try:
            incarnation = self.registry.require(name).incarnation.require_recorded()
            review = GoalWaitReview.capture(self, incarnation)
        except UnregisteredThreadError:
            return ()
        owner = review.owner
        snapshot = review.snapshot
        if self.agents.is_working(owner.incarnation, snapshot) or not any(
            review.wait.current_for(goal, owner.incarnation.require_recorded())
            for goal in owner.goal.active()
        ):
            return ()
        closed = GoalWaits.closed_wait_group(
            owner.name, review.wait.targets, self.waits.read(), snapshot, self.agents,
        )
        if not closed:
            return ()
        # A failed read is a diagnostic, never evidence of silence.
        def release(edit: RegistryEdit) -> tuple[str, ...]:
            snapshot = edit.original.snapshot
            owner, = snapshot.threads_for(review.owner.incarnation.require_recorded())
            if self.agents.is_working(owner.incarnation, snapshot):
                return ()
            closed = GoalWaits.closed_wait_group(
                owner.name, review.wait.targets, self.waits.read(), snapshot, self.agents,
            )
            if not closed:
                return ()
            names = ", ".join(f"@{member}" for member in closed)
            note = (
                f"Dependency wait group ({names}) has no independent worker. "
                "Standby was released; inspect dependencies and continue useful work."
            )
            return closed if self._release_reviewed_wait(edit, review, note) else ()

        return review.publish_unanswered(self, release)

    def release_waits_after_terminal_turn(self, fence: TurnCompletion) -> tuple[str, ...]:
        """A completed turn may release silence observed outside the registry lock."""
        snapshot = self.registry.snapshot()
        sources = fence.idle_sources(snapshot.idle_after)
        if not sources:
            return ()
        source, = sources
        released: list[str] = []
        for wait in self.waits.targeting(source.incarnation.require_recorded()):
            for owner in snapshot.threads_for(wait.owner):
                try:
                    review = GoalWaitReview.capture(self, owner.incarnation.require_recorded())
                except UnregisteredThreadError:
                    continue
                if review.wait == wait:
                    released.extend(self._release_finished_wait(review, fence))
        return tuple(released)

    def _release_finished_wait(
        self, review: GoalWaitReview, fence: TurnCompletion,
    ) -> tuple[str, ...]:
        wait, owner = review.wait, review.owner
        if not any(wait.current_for(goal, owner.incarnation.require_recorded())
                   for goal in owner.goal.active()):
            return ()
        if any(self.agents.is_working(target.incarnation, review.snapshot)
               for target in wait.targets):
            return ()
        def release(edit: RegistryEdit) -> tuple[str, ...]:
            current = edit.original.snapshot
            sources = fence.idle_sources(current.idle_after)
            if not sources or any(self.agents.is_working(target.incarnation, current)
                                  for target in wait.targets):
                return ()
            actual_source, = sources
            note = (
                f"Declared dependency @{actual_source.name} finished without a qualifying direct "
                "reply, and no declared dependency has an active turn. "
                "Standby was released; inspect messages and UNKNOWN inputs, then "
                "continue independent work or coordinate the next step with its context owner. "
                "No input was replayed."
            )
            if self._release_reviewed_wait(edit, review, note):
                actual_owner, = current.threads_for(owner.incarnation.require_recorded())
                return (actual_owner.name,)
            return ()

        return review.publish_unanswered(self, release)

    def _release_reviewed_wait(self, edit: RegistryEdit, review: GoalWaitReview, note: str) -> bool:
        """Publish a validated silent wait through the review's existing registry edit."""
        snapshot = edit.original.snapshot
        owner, = snapshot.threads_for(review.owner.incarnation.require_recorded())
        goal = owner.goal.require_active()
        progress = f"{goal.progress}\n\n{note}" if goal.progress else note
        change = edit.document.prepare_registration(
            replace(owner, goal=replace(goal, progress=progress, revision=goal.revision + 1)),
            snapshot.status(owner.name), new_owner=False,
        )
        self.registry._commit_registration(edit, change)
        return review.wait.clear(self.waits)

    def consume_reply_wait(self, owner: RegistryOwner, reference: MessageReference) -> bool:
        """Read the exact completed delivery before checking its original owner fence."""
        with self.registry.store.current_snapshot():
            owner.require_registry(self.registry)
            review = GoalWaitReview.capture(self, owner.thread.incarnation.require_recorded())
        incarnation = review.owner.incarnation.require_recorded()
        if not review.wait.owner_matches(incarnation):
            return False
        original, = self.bus.bus.deliveries_for_references((reference,))
        if not review.wait.matches(original, incarnation):
            return False
        with self.registry.store.current_snapshot() as cut:
            owner.require_registry(self.registry)
            if not review.current_for(self):
                return False
            current, = cut.snapshot.threads_for(incarnation)
            goal = current.continuation_goal(owner.thread)
            return any(review.wait.current_for(actual, incarnation)
                       and review.wait.clear(self.waits) for actual in goal)
