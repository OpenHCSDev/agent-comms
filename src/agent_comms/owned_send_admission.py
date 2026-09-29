"""Ordinary owner-turn input admission, held through the native stdin write."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from .goal_attempt_phase import ClaimedAttempt
from .goal_attempts import Generation, LaunchPermit
from .goal_generation import ReservedGeneration
from .routing import TurnRouting
from .store_files import _store_lock
from .thread_identity import TurnId
from .turn_goal_permission import AcceptedGoalPermission
from .turn_input_binding import OrdinaryTurnBinding, SelectedOriginalBinding, TurnInputBinding
from .turn_input_source import (
    AcceptedFollowingInput,
    OriginalTurnInput,
    RoutedFollowingInput,
    TurnInputSource,
)

if TYPE_CHECKING:
    from .comms import Comms
    from .goal_attempts import GoalAttemptStore
    from .goal_waits import GoalWait
    from .input_drain import InputDrain
    from .registry_document import RegistrySnapshot
    from .threads import Thread


@dataclass(frozen=True, kw_only=True)
class OwnedSendAdmission:
    comms: Comms
    inputs: InputDrain
    goal_store: GoalAttemptStore | None
    session_id: str
    thread: Thread
    turn: TurnId
    admission: int
    original: OriginalTurnInput
    goal_permit: LaunchPermit | None
    _maintenance_wire_locked: ClassVar[bool] = True

    def source(self, public_id: str | None) -> TurnInputSource:
        if public_id is None:
            return self.original
        key = self.inputs.steering_input_keys.get(self.session_id, {}).get(public_id)
        keys = (key,) if key else ()
        goals = self.inputs.steering_goal_ids.get(self.session_id, {})
        if public_id in goals:
            return AcceptedFollowingInput(
                keys=keys,
                accepted_id=public_id,
                goal_permission=AcceptedGoalPermission(goals[public_id]),
            )
        return RoutedFollowingInput(
            keys=keys, accepted_id=public_id, goal_permission=self.original.goal_permission
        )

    def _current_owner(
        self, current: Thread | None, registry: RegistrySnapshot, canonical: str
    ) -> bool:
        return (
            current is not None
            and self.thread.incarnation.current(registry)
            and registry.statuses[canonical].running
            and registry.admission_generations.get(canonical) == self.admission
            and current.process_identity == self.thread.process_identity
            and current.worktree == self.thread.worktree
            and current.active_turn is not None
            and TurnId(current.active_turn.id) == self.turn
        )

    def _accepted(self, source: TurnInputSource, current: Thread, wait: GoalWait | None) -> bool:
        if source.accepted_id is None:
            return True
        accepted = self.inputs.queued_inputs.get(self.session_id, {}).get(source.accepted_id)
        return (
            accepted is not None
            and accepted.current(current, self.admission, wait)
            and source.keys == (f"acp:{source.accepted_id}",)
            and self.inputs.steering_input_keys.get(self.session_id, {}).get(source.accepted_id)
            == source.keys[0]
        )

    def _binding(self, source: TurnInputSource) -> TurnInputBinding:
        selected = source.selected_admission(self.inputs, self.session_id)
        if selected is None:
            return OrdinaryTurnBinding(root=self.comms.root, dispositions=self.inputs.dispositions)
        return SelectedOriginalBinding(
            root=self.comms.root,
            dispositions=self.inputs.dispositions,
            selected=selected,
            inputs=self.inputs,
        )

    def _goal_launch_current(self, source: TurnInputSource) -> bool:
        if source.bypasses_goal_permit or self.goal_permit is None:
            return True
        attempt = self.goal_permit.reservation
        assert self.goal_store is not None
        return self.goal_store._is_attempt(
            attempt,
            ClaimedAttempt(),
            Generation(
                attempt.goal_id, attempt.generation, ReservedGeneration(), attempt.attempt_id
            ),
        )

    @contextmanager
    def __call__(
        self, public_id: str | None, native_id: str, sent_text: str, *, already_bound: bool = False
    ) -> Iterator[bool | None]:
        # No await/provider/ACK while held: this same lock spans final checks and write.
        with _store_lock(self.comms._wire_lock_path):
            self.comms.owners.maintenance.assert_open_unlocked()
            snapshot = self.comms.registry.snapshot()
            canonical = snapshot.aliases.get(self.thread.name, self.thread.name)
            current = snapshot.threads.get(canonical)
            goal = current.goal if current is not None else None
            wait = self.comms.goals.goal_wait(canonical) if current is not None else None
            source = self.source(public_id)
            binding = self._binding(source)
            owner_ok = (
                self._current_owner(current, snapshot, canonical)
                and source.valid_keys(sent_text)
                and self._accepted(source, current, wait)
            )
            defer_for_goal = owner_ok and source.defers_for_goal(self.thread.goal, goal)
            allowed = (
                owner_ok
                and source.allows_context(goal, wait, snapshot)
                and binding.available(current)
                and self._goal_launch_current(source)
            )
            if not allowed:
                binding.invalidate()
            else:
                allowed = binding.bind(
                    current=current,
                    keys=source.keys,
                    admission=self.admission,
                    turn=self.turn,
                    native_id=native_id,
                    text=sent_text,
                    already_bound=already_bound,
                )
            if allowed:
                allowed = source.consume_wait(self.comms, canonical, wait)
            if allowed:
                self.comms.transcripts.routes.record_input_display(
                    native_id,
                    source.display(self.inputs.dispositions, sent_text),
                    sent_text=sent_text,
                    routing=TurnRouting(source.origins, None) if source.origins else None,
                )
            yield True if allowed else None if defer_for_goal else False

    def native_start(self, public_id: str | None, native_id: str, sent_text: str) -> bool:
        source = self.source(public_id)
        return source.valid_keys(sent_text) and all(
            self.inputs.dispositions.started(
                key, turn_id=self.turn.value, native_id=native_id, text=sent_text
            )
            for key in source.keys
        )
