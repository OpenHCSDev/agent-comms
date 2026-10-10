"""Ordinary owner-turn admission: named checks held through native stdin.write."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .ordinary_admission_rules import (
    AcceptedInputCheck,
    InputKeysCheck,
    MissingAcceptedInputCheck,
    MissingTurnOwnerCheck,
    OrdinaryContextCheck,
    OrdinaryGoalGrantCheck,
    OrdinaryOwnerCheck,
    UnboundOrdinaryInputRule,
    UnconsumedDependencyRule,
)
from .reservation_rules import ReservationRule, ReservationViolationError
from .routing import TurnRouting
from .thread_identity import TurnId
from .turn_input_source import (
    OriginalTurnInput,
    RoutedFollowingInput,
    TurnInputSource,
)

if TYPE_CHECKING:
    from .comms import Comms
    from .goal_attempts import GoalAttemptStore, LaunchPermit
    from .goal_waits import GoalWait
    from .input_drain import InputDrain
    from .registry_document import RegistrySnapshot
    from .threads import Thread

_LOG = logging.getLogger(__name__)


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

    def source(self, public_id: str | None) -> TurnInputSource:
        if public_id is None:
            return self.original
        source = self.inputs.following_sources.get(self.session_id, {}).get(public_id)
        return (
            source
            if source is not None
            else RoutedFollowingInput(
                keys=(), accepted_id=public_id, goal_permission=self.original.goal_permission
            )
        )

    def _check_owner(
        self,
        current: Thread | None,
        snapshot: RegistrySnapshot,
        canonical: str,
        source: TurnInputSource,
        text: str,
    ) -> None:
        if current is None:
            MissingTurnOwnerCheck().require_valid()
        OrdinaryOwnerCheck(
            expected=self.thread,
            current=current,
            registry=snapshot,
            canonical=canonical,
            admission=self.admission,
            turn=self.turn,
        ).require_valid()
        InputKeysCheck(source=source, text=text).require_valid()
        if source.accepted_id is None:
            return
        accepted = self.inputs.queued_inputs.get(self.session_id, {}).get(source.accepted_id)
        if accepted is None:
            MissingAcceptedInputCheck().require_valid()
        AcceptedInputCheck(
            receipt=accepted,
            current=current,
            admission=self.admission,
            input_id=source.accepted_id,
            keys=source.keys,
            accepted_source=self.inputs.following_sources.get(self.session_id, {}).get(
                source.accepted_id
            ),
        ).require_valid()

    def _require_context(
        self, source: TurnInputSource, current: Thread, wait: GoalWait | None,
    ) -> None:
        OrdinaryContextCheck(
            source=source, goal=current.goal, wait=wait, comms=self.comms
        ).require_valid()
        if not source.bypasses_goal_permit and self.goal_permit is not None:
            assert self.goal_store is not None
            OrdinaryGoalGrantCheck(permit=self.goal_permit, store=self.goal_store).require_valid()

    def _refusal(self, rule: ReservationRule, defer: bool) -> bool | None:
        _LOG.info(
            "Ordinary input %s: %s",
            "deferred" if defer else "refused",
            ReservationViolationError(rule),
        )
        return None if defer else False

    @contextmanager
    def __call__(
        self, public_id: str | None, native_id: str, sent_text: str, *, already_bound: bool = False
    ) -> Iterator[bool | None]:
        # Backend ingress already owns shared custody through this check and write.
        snapshot = self.comms.registry.snapshot()
        canonical = snapshot.canonical_name(self.thread.name)
        current = snapshot.threads.get(canonical)
        wait = self.comms.goals.goal_wait(canonical) if current is not None else None
        source = self.source(public_id)
        try:
            self._check_owner(current, snapshot, canonical, source, sent_text)
        except ReservationViolationError as error:
            yield self._refusal(error.rule, False)
            return
        defer = source.defers_for_goal(self.thread.goal, current.goal)
        try:
            self._require_context(source, current, wait)
        except ReservationViolationError as error:
            yield self._refusal(error.rule, defer)
            return
        # Binding failures may be uncertain. Exceptions here still propagate;
        # never reinterpret a failed durable operation as a retryable policy refusal.
        if not self.inputs.dispositions.bind_turn_input(
            source.keys,
            admission=self.admission,
            turn_id=self.turn.value,
            native_id=native_id,
            text=sent_text,
            already_bound=already_bound,
        ):
            yield self._refusal(UnboundOrdinaryInputRule(), defer)
            return
        if not source.consume_wait(self.comms, canonical, wait):
            yield self._refusal(UnconsumedDependencyRule(), defer)
            return
        self.comms.transcripts.routes.record_input_display(
            native_id,
            source.display(self.inputs.dispositions, sent_text),
            sent_text=sent_text,
            routing=TurnRouting(
                tuple(origin.reference for origin in source.origins), None
            ) if source.origins else None,
        )
        yield True

    def native_start(self, public_id: str | None, native_id: str, sent_text: str) -> bool:
        source = self.source(public_id)
        return source.valid_keys(sent_text) and all(
            self.inputs.dispositions.started(
                key, turn_id=self.turn.value, native_id=native_id, text=sent_text
            )
            for key in source.keys
        )
