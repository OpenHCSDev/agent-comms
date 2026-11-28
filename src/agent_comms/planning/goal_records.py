from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from agent_comms.execution.input_key import InputKey
from agent_comms.execution.reported_turn import ReportedTurnState
from agent_comms.input_attempt import ReservedInput
from agent_comms.input_disposition import InputDispositions
from agent_comms.messaging.published_bus import PublishedBus
from agent_comms.messaging.messages import Message
from agent_comms.nominal.field_codec import GoalId, GoalWaitId
from agent_comms.nominal.lifecycle_capability import Settled
from agent_comms.planning.attempt_resolution import ComplianceDecision, RetryDecision
from agent_comms.planning.goal_action_store import GoalActionStore, PreparedStandbyReview
from agent_comms.planning.goal_store_access import GoalStoreAccess
from agent_comms.planning.goal_wait_recovery import (
    GoalInputReview,
    GoalWaitRecovery,
    GoalWaitReview,
)
from agent_comms.planning.goal_wait_target import GoalWaitTarget
from agent_comms.planning.goal_waits import GoalWaits, RecordedGoalWait
from agent_comms.planning.goals import Goal
from agent_comms.registry.agent_activity import AgentActivity
from agent_comms.registry.registration import Registration
from agent_comms.registry.registration_change import RegistrationChange
from agent_comms.registry.registry_document import RegistrySnapshot
from agent_comms.registry.registry_store import RegistryEdit
from agent_comms.registry.threads import Thread


@dataclass(frozen=True, slots=True, kw_only=True)
class RecordedGoalInputReview(GoalInputReview, PreparedStandbyReview):
    """A prepared dependency-reply review bound to the store that will publish its wait."""

    goals: GoalRecords = field(repr=False, compare=False)

    def commit_standby(
        self,
        goal: Goal,
        thread: Thread,
        reported_turn: ReportedTurnState,
        wait_for: tuple[str, ...],
        reviewed_inputs: tuple[InputKey, ...],
    ) -> None:
        self.require_current(self.goals, thread)
        wait_targets = self.targets
        aliases = self.owners
        dispositions = self.goals.dispositions
        unknown = {row.key: row for row in self.unknown}
        # An input delivered into this recipient's turn after the inbox cut is already handled.
        delivered = dispositions.read(
            tuple(key for key in reviewed_inputs if key not in unknown)
        ).started_for(aliases)
        reviewed_keys = tuple(
            key for key in dict.fromkeys(reviewed_inputs) if key not in delivered
        )
        if any(key not in unknown or unknown[key].sequence is None for key in reviewed_keys):
            raise ValueError("Review only this recipient's exact unresolved bus input keys.")
        if not set(reviewed_keys) <= self.eligible_keys:
            raise ValueError("Review only direct inputs from these declared dependencies.")
        pending = next(
            (
                row
                for row in self.unknown
                if row.key in self.eligible_keys
                and row.key not in reviewed_keys
                and not row.reviewed_for_goal(goal.id)
            ),
            None,
        )
        if pending is not None:
            sequence = pending.sequence
            raise ValueError(
                f"Dependency reply {sequence} is already pending or UNKNOWN. "
                f"Call comms_inbox with standby={{'goal_id': {goal.id!r}, "
                f"'wait_for': {list(wait_for)!r}}}. Inspect standby_review.messages, then "
                "pass only standby_review.reviewed_inputs to comms_goal to wait "
                "for a later reply. Do not pass excluded owner or other dependency inputs. "
                "This does not mark them STARTED or replay them."
            )
        snapshot = self.goals.registry.snapshot()
        if not any(
            self.goals.agents.answers_later(target.incarnation, snapshot)
            for target in wait_targets
        ):
            names = ", ".join(f"@{target.incarnation.name}" for target in wait_targets)
            raise ValueError(
                f"No thread this wait names will answer ({names}): none has a live bridge or "
                "process. Inspect its registered process before declaring standby."
            )
        closed = GoalWaits.closed_wait_group(
            thread.name,
            wait_targets,
            self.goals.waits.read(),
            snapshot,
            self.goals.agents,
        )
        if closed:
            names = ", ".join(f"@{name}" for name in closed)
            raise ValueError(
                f"Standby would close a dependency wait group ({names}). "
                "At least one agent must remain able to work or reply. "
                "Continue independent work or change the dependencies."
            )
        dispositions.review_for_goal(
            reviewed_keys,
            observed=tuple(unknown[key] for key in reviewed_keys),
            owners=aliases,
            goal_id=goal.id,
            goal_revision=goal.revision,
            turn_id=reported_turn,
        )
        # Commit scheduling authority first. A crash before the registry
        # progress update must leave this same goal waiting, not runnable.
        reporting_lease = thread.turn_lease
        self.goals.waits.record(
            RecordedGoalWait(
                goal.id,
                GoalWaitId.mint(),
                goal.revision,
                self.through_seq,
                wait_targets,
                owner=thread.incarnation.require_recorded(),
                report_turn_id=reporting_lease.stored_turn_id(),
                report_turn_generation=reporting_lease.stored_generation(),
                target_turn_generations=tuple(
                    snapshot.threads[snapshot.canonical_name(target.incarnation.name)]
                    .turn_lease.stored_generation()
                    for target in wait_targets
                ),
            )
        )


class GoalRecords(GoalStoreAccess, GoalWaitRecovery, GoalActionStore):
    def __init__(self, root: Path, registry: Registration, bus: PublishedBus, agents: AgentActivity):
        super().__init__(root, agents)
        self.registry = registry
        self.bus = bus
        self.dispositions = InputDispositions(root)

    def require_thread(self, name: str) -> Thread:
        return self.registry.require(name)

    def is_thread_running(self, name: str) -> bool:
        return self.registry.status(name).is_running()

    def registry_snapshot(self) -> RegistrySnapshot:
        return self.registry.snapshot()

    def commit_registration(self, edit: RegistryEdit, change: RegistrationChange) -> None:
        self.registry._commit_registration(edit, change)

    def bus_revision(self) -> int:
        return self.bus.bus.revision()

    def clear_goal_wait(self, goal_id: GoalId) -> bool:
        return self.waits.clear(goal_id)

    def prepare_standby_review(
        self, thread: Thread, goal_id: GoalId, wait_for: Sequence[str]
    ) -> RecordedGoalInputReview:
        return self._goal_input_review(thread, goal_id, wait_for)

    def resume_goal_generation(self, goal_id: GoalId, executing: bool) -> None:
        generation = self.open_goal_store().require_generation(goal_id)
        generation.lifecycle.resume_status(executing)

    def comply_goal_generation(
        self, goal_id: GoalId, decision: ComplianceDecision, executing: bool
    ) -> None:
        store = self.open_goal_store()
        generation = store.require_generation(goal_id)
        generation.lifecycle.continue_work(store, generation, decision)
        resumed = store.require_generation(goal_id)
        resumed.lifecycle.resume_status(executing)

    def retire_goal_generation(self, goal_id: GoalId) -> None:
        # Revoke the old protected goal before replacing its registry identity.
        attempts = self.open_goal_store()
        generation = attempts.snapshot(goal_id)
        if generation is not None and not generation.lifecycle.items_with(Settled, generation):
            attempts.retire_goal(
                goal_id, expected_generation=generation.number,
                attempt_id=generation.attempt_id,
            )

    def create_goal_generation(self, goal_id: GoalId) -> None:
        self.open_goal_store().create_goal(goal_id)

    def authorize_goal_retry(self, goal_id: GoalId, decision: RetryDecision) -> None:
        self.open_goal_store().authorize_current_retry(goal_id, decision)

    def _goal_input_review(
        self, thread: Thread, goal_id: GoalId, wait_for: Sequence[str]
    ) -> RecordedGoalInputReview:
        captured = GoalWaitReview.capture(self, thread.incarnation.require_recorded())
        thread = captured.owner
        snapshot = captured.snapshot
        goal = thread.goal.require().require_id(goal_id).require_active()
        if not wait_for:
            raise ValueError("Standby requires explicit wait_for thread names.")
        resolved = tuple(snapshot.require(target.removeprefix("@")) for target in wait_for)
        if any(target.incarnation == thread.incarnation for target in resolved):
            raise ValueError("A goal cannot wait for its own thread.")
        targets = tuple(
            dict.fromkeys(GoalWaitTarget(target.incarnation.require_recorded()) for target in resolved)
        )
        owners = captured.owners
        dispositions = self.dispositions
        unknown = {row.key: row for row in
                   dispositions.unresolved((snapshot.entry(thread.name),)).unknown(owners)}
        eligible = set()
        incarnation = thread.incarnation.require_recorded()
        lookup = incarnation.lookup
        after_seq = captured.wait.review_after_seq(
            goal, incarnation, targets
        )
        sequences = tuple({
            sequence for row in unknown.values()
            for sequence in row.bus_sequences_through(after_seq)
        })
        senders = tuple(target.incarnation.lookup for target in targets)
        replies: dict[str, Message] = {}
        with self.bus.bus.reading() as source:
            through_seq = source.revision()
            # Preserve older UNKNOWN rows; new inputs begin at this wait's own cursor.
            # Both selections use the same frozen recipient and sender projections.
            page = source.direct_replies(
                lookup, sequences=sequences, limit=len(sequences), descending=False,
                sender_lookups=senders,
            ) if sequences else ()
            while True:
                replies.update({
                    original.message.bus_key(thread): original.message
                    for original in page
                })
                page = source.direct_replies(
                    lookup, after=after_seq, limit=100, descending=False,
                    sender_lookups=senders,
                )
                if not page:
                    break
                after_seq = page[-1].seq
        # Pure document inspection; only explicit successful review persists it.
        document = dispositions.read(tuple(replies)).record(*(
            ReservedInput(
                key, message.seq, thread.name, snapshot.claim_generation(thread.name),
                message.target, message.body,
            )
            for key, message in replies.items()
        ))
        for pending in document.unknown(owners):
            unknown[pending.key] = pending
            eligible.add(pending.key)
        return RecordedGoalInputReview(
            snapshot=captured.snapshot,
            owner=thread,
            wait=captured.wait,
            turn=captured.turn,
            through_seq=through_seq,
            targets=targets,
            unknown=tuple(sorted(unknown.values(), key=lambda row: row.order)),
            eligible_keys=frozenset(eligible),
            goals=self,
        )
