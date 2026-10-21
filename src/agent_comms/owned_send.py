"""Check an ordinary owner turn and hold its locks through native stdin.write."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .channel_coding_tools import CodingToolOwner
from .coordination_session import NativeBoundary
from .coordinator import Coordination
from .envelope_claim_transitions import NativeClaim
from .input_attempt import InputAlreadyCleared
from agent_comms.ordinary_turn_rules import (
    AcceptedInputCheck,
    InputKeysCheck,
    MissingAcceptedInputCheck,
    MissingAcceptedInputRule,
    MissingTurnOwnerCheck,
    OrdinaryContextCheck,
    OrdinaryOwnerCheck,
    UnboundOrdinaryInputRule,
    UnconsumedDependencyRule,
)
from .reservation_rules import ReservationRule, ReservationViolationError
from .routing import TurnRouting
from agent_comms.send_outcome import AuthorityChangedSend, DeferredSend, GrantedSend, RefusedSend, SendOutcome
from .thread_identity import TurnId
from .turn_input_source import (
    OriginalTurnInput,
    TurnInputSource,
)

if TYPE_CHECKING:
    from agent_comms.goal_attempts import GoalAttemptStore
    from agent_comms.goal_turn_permit import GoalTurnPermit
    from .goal_waits import GoalWait
    from .input_drain import InputDrain
    from .registry_document import RegistrySnapshot
    from .threads import Thread
    from .turn_goal_permission import TurnGoalPermission

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class OwnedSend(CodingToolOwner):
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
        try:
            return self.inputs.state(self.session_id).following[public_id]
        except KeyError as error:
            raise ReservationViolationError(MissingAcceptedInputRule()) from error

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

    def _require_context(
        self, source: TurnInputSource, current: Thread, wait: GoalWait,
    ) -> None:
        OrdinaryContextCheck(
            source=source, owner=current, goal=current.goal, wait=wait, comms=self.comms
        ).require_valid()
        if not source.bypasses_goal_permit:
            self.goal_permit.check_grant(self.goal_store)

    def _refusal(self, rule: ReservationRule) -> SendOutcome:
        _LOG.info("Ordinary input refused: %s", ReservationViolationError(rule))
        return AuthorityChangedSend()

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
            # The queue grants a send, not a running tool's lifetime. Once Pi
            # starts this exact input, StartedInput removes its queue entry.
            if source.accepted_id is not None:
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
                    inputs=self.inputs,
                    session_id=self.session_id,
                ).require_valid()
        except ReservationViolationError as error:
            return self._refusal(error.rule)
        if source.defers_for_goal(self.original.goal_permission, current.goal):
            return DeferredSend()
        wait = self.comms.goals.goal_wait(canonical)
        try:
            self._require_context(source, current, wait)
        except ReservationViolationError as error:
            return self._refusal(error.rule)
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
            return self._refusal(UnboundOrdinaryInputRule())
        if not source.consume_wait(self.comms, canonical, wait):
            return self._refusal(UnconsumedDependencyRule())
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

    @property
    def goal_permission(self) -> TurnGoalPermission:
        return self.original.goal_permission

    def verify(self, store, turn, snapshot) -> None:
        """Recheck the running original without binding it or consuming its wait again."""
        from .native_tool_call import SelectedToolDenied

        if not turn.started_input or not turn.accepts_output:
            raise SelectedToolDenied("Native tool has no started original input")
        canonical = snapshot.canonical_name(self.thread.name)
        current = snapshot.threads.get(canonical)
        try:
            self._check_owner(current, snapshot, canonical, self.original, turn.command.message)
            self._require_context(self.original, current, self.comms.goals.goal_wait(canonical))
        except (ReservationViolationError, InputAlreadyCleared) as error:
            raise SelectedToolDenied(str(error)) from error

    def claim_resource(self, store, turn, resource, slot):
        with NativeBoundary(self.comms.bus) as boundary:
            snapshot = boundary.registry
            self.verify(store, turn, snapshot)
            owner = snapshot.require(self.owner_name)
            canonical = resource.normalized(Path(owner.worktree))
            scope = NativeClaim(owner_claimed_generation=self.claim_scope,
                                operation_id=slot, turn_id=self.turn.value,
                                input_id=turn.command.input_id)
            return boundary.bus.publisher._publish_acquisition_unlocked(
                boundary.bus_lock, snapshot, owner, canonical, scope, activity=boundary.activity)

    async def release_resources(self) -> None:
        """The ordinary turn owns its acquisitions through native/tool retirement."""
        def release() -> None:
            with NativeBoundary(self.comms.bus) as boundary:
                boundary.bus.publisher.release_resources(
                    boundary, tuple(self.claims.values()), "Released files after an ordinary turn"
                )

        await Coordination.run_worker(release)

    def native_start(self, public_id: str | None, native_id: str, sent_text: str) -> bool:
        source = self.source(public_id)
        return source.valid_keys(sent_text) and all(
            self.inputs.dispositions.started(
                key, turn_id=self.turn.value, native_id=native_id, text=sent_text
            )
            for key in source.keys
        )
