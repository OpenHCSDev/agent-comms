"""What goal actions read and write: the stores a goal changes against, below the actions."""

from __future__ import annotations

from collections.abc import Sequence
from functools import cached_property
from pathlib import Path
from agent_comms.nominal.field_codec import GoalId
from .agent_activity import AgentActivity
from .goal_attempts import GoalAttemptFile, GoalAttemptStore
from .goal_waits import (
    GoalInputReview,
    GoalWaitRecovery,
    GoalWaitReview,
    GoalWaits,
)
from .registration import Registration
from .goal_presentation import GoalWaitTarget
from .input_attempt import (
    ReservedInput,
)
from .input_disposition import InputDispositions
from .message_bus import MessageBus
from .threads import Thread
from .todos import TodoStore
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .messages import Message


class GoalRecords(GoalWaitRecovery):
    def __init__(self, root: Path, registry: Registration, bus: MessageBus, agents: AgentActivity):
        self.root = root
        self.registry = registry
        self.bus = bus
        self.agents = agents
        self.waits = GoalWaits(root)
        self.dispositions = InputDispositions(root)
        self.goal_store: GoalAttemptFile = GoalAttemptFile(GoalAttemptStore.path_in(
            GoalAttemptStore.directory_in(root)))

    @cached_property
    def todos(self) -> TodoStore:
        return TodoStore(self.root)

    def open_goal_store(self) -> GoalAttemptStore:
        store = self.goal_store.open()
        self.goal_store = store
        return store

    def _goal_input_review(
        self, thread: Thread, goal_id: GoalId, wait_for: Sequence[str]
    ) -> GoalInputReview:
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
                    InputDispositions.bus_key(original.message, thread): original.message
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
        return GoalInputReview(
            captured.snapshot,
            thread,
            captured.wait,
            captured.turn,
            through_seq,
            targets,
            tuple(sorted(unknown.values(), key=lambda row: row.order)),
            frozenset(eligible),
        )
