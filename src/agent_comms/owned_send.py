"""Check an ordinary owner turn and hold its locks through native stdin.write."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .ordinary_turn_rules import (
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
from .send_outcome import SendOutcome, GrantedSend, RefusedSend, AuthorityChangedSend
from .input_attempt import InputAlreadyCleared
from .thread_identity import TurnId
from .turn_input_source import (
    OriginalTurnInput,
    RoutedFollowingInput,
    TurnInputSource,
)

if TYPE_CHECKING:
    from .comms import Comms
    from .goal_attempts import GoalAttemptStore, GoalTurnPermit
    from .goal_waits import GoalWait
    from .input_drain import InputDrain
    from .registry_document import RegistrySnapshot
    from .threads import Thread

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class OwnedSend:
    comms: Comms
    inputs: InputDrain
    goal_store: GoalAttemptStore
    session_id: str
    thread: Thread
    turn: TurnId
    claim_scope: int
    original: OriginalTurnInput
    goal_permit: GoalTurnPermit

    def source(self, public_id: str | None) -> TurnInputSource:
        if public_id is None:
            return self.original
        source = self.inputs.state(self.session_id).following.get(public_id)
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
            claim_scope=self.claim_scope,
            turn=self.turn,
        ).require_valid()
        InputKeysCheck(source=source, text=text).require_valid()
        if source.accepted_id is None:
            return
        accepted = self.inputs.state(self.session_id).queued.get(source.accepted_id)
        if accepted is None:
            for row in self.inputs.dispositions.read(source.keys).rows.values():
                row.require_not_cleared()
            MissingAcceptedInputCheck().require_valid()
        AcceptedInputCheck(
            acknowledgement=accepted,
            current=current,
            claim_scope=self.claim_scope,
            input_id=source.accepted_id,
            keys=source.keys,
            accepted_source=self.inputs.state(self.session_id).following.get(
                source.accepted_id
            ),
        ).require_valid()

    def _require_context(
        self, source: TurnInputSource, current: Thread, wait: GoalWait,
    ) -> None:
        OrdinaryContextCheck(
            source=source, owner=current, goal=current.goal, wait=wait, comms=self.comms
        ).require_valid()
        if not source.bypasses_goal_permit:
            self.goal_permit.perform(lambda permit: OrdinaryGoalGrantCheck(
                permit=permit, store=self.goal_store,
            ).require_valid())

    def _refusal(self, rule: ReservationRule, defer: bool) -> SendOutcome:
        _LOG.info(
            "Ordinary input %s: %s",
            "deferred" if defer else "refused",
            ReservationViolationError(rule),
        )
        return RefusedSend() if defer else AuthorityChangedSend()

    def _prepare(
        self, public_id: str | None, native_id: str, sent_text: str, *, already_bound: bool = False
    ) -> SendOutcome:
        # Backend input handling holds shared locks through this check and write.
        snapshot = self.comms.registry.snapshot()
        canonical = snapshot.canonical_name(self.thread.name)
        current = snapshot.threads.get(canonical)
        source = self.source(public_id)
        try:
            self._check_owner(current, snapshot, canonical, source, sent_text)
        except ReservationViolationError as error:
            return self._refusal(error.rule, False)
        wait = self.comms.goals.goal_wait(canonical)
        defer = source.defers_for_goal(self.thread.goal, current.goal)
        try:
            self._require_context(source, current, wait)
        except ReservationViolationError as error:
            return self._refusal(error.rule, defer)
        # Binding failures may be uncertain. Exceptions here still propagate;
        # never reinterpret a failed durable operation as a retryable policy refusal.
        if not self.inputs.dispositions.bind_turn_input(
            source.keys,
            claim_scope=self.claim_scope,
            turn_id=self.turn.value,
            native_id=native_id,
            text=sent_text,
            already_bound=already_bound,
        ):
            return self._refusal(UnboundOrdinaryInputRule(), defer)
        if not source.consume_wait(self.comms, canonical, wait):
            return self._refusal(UnconsumedDependencyRule(), defer)
        self.comms.transcripts.routes.record_input_display(
            native_id,
            source.display(self.inputs.dispositions, sent_text),
            sent_text=sent_text,
            routing=TurnRouting(
                tuple(origin.reference for origin in source.origins), None
            ) if source.origins else None,
        )
        return GrantedSend()

    @contextmanager
    def __call__(
        self, public_id: str | None, native_id: str, sent_text: str, *, already_bound: bool = False
    ) -> Iterator[SendOutcome]:
        try:
            decision = self._prepare(public_id, native_id, sent_text, already_bound=already_bound)
        except InputAlreadyCleared:
            decision = RefusedSend()
        yield decision

    def native_start(self, public_id: str | None, native_id: str, sent_text: str) -> bool:
        source = self.source(public_id)
        return source.valid_keys(sent_text) and all(
            self.inputs.dispositions.started(
                key, turn_id=self.turn.value, native_id=native_id, text=sent_text
            )
            for key in source.keys
        )
