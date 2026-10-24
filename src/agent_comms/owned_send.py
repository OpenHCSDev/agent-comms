"""Check an ordinary owner turn and hold its locks through native stdin.write."""

from __future__ import annotations

import logging
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agent_comms.ordinary_turn_rules import (
    InputKeysCheck,
    MissingAcceptedInputRule,
    MissingTurnOwnerCheck,
    OrdinaryContextCheck,
    OrdinaryOwnerCheck,
)
from agent_comms.send_outcome import AuthorityChangedSend, SendBoundary, SendOutcome

from . import agent_events as events
from .agent_backend import InputStartListener
from .channel_coding_tools import CodingToolOwner
from .coordination_session import NativeBoundary
from .coordinator import Coordination
from .envelope_claim_transitions import NativeClaim
from .field_codec import ClaimId, InputId, TurnId
from .input_attempt import InputAlreadyCleared
from .reservation_rules import ReservationRule, ReservationViolationError
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
class OwnedSend(CodingToolOwner, InputStartListener, SendBoundary):
    started_events = (events.OriginalInputStarted(),)

    inputs: InputDrain
    goal_store: GoalAttemptStore
    session_id: str
    thread: Thread
    turn: TurnId
    claim_generation: int
    original_source: OriginalTurnInput
    goal_permit: GoalTurnPermit

    def source(self, public_id: InputId) -> TurnInputSource:
        try:
            return self.inputs.state(self.session_id).following[public_id]
        except KeyError as error:
            raise ReservationViolationError(MissingAcceptedInputRule()) from error

    def require_owner(self, snapshot: RegistrySnapshot, canonical: str) -> Thread:
        try:
            return snapshot.threads[canonical]
        except KeyError:
            MissingTurnOwnerCheck().require_valid()
            raise

    def _check_owner(
        self,
        current: Thread,
        snapshot: RegistrySnapshot,
        canonical: str,
        source: TurnInputSource,
        text: str,
    ) -> None:
        OrdinaryOwnerCheck(
            expected=self.thread,
            current=current,
            registry=snapshot,
            canonical=canonical,
            claim_generation=self.claim_generation,
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

    def send_original(self, native_id: InputId, text: str) -> AbstractContextManager[SendOutcome]:
        return self.original_source.prepare_send(self, native_id, text)

    def send_following(
        self, public_id: InputId, native_id: InputId, text: str,
    ) -> AbstractContextManager[SendOutcome]:
        return self.source(public_id).prepare_send(self, native_id, text)

    def send_interrupt(
        self, public_id: InputId, native_id: InputId, text: str,
    ) -> AbstractContextManager[SendOutcome]:
        return self.source(public_id).prepare_send(
            self, native_id, text, already_bound=True,
        )

    @property
    def goal_permission(self) -> TurnGoalPermission:
        return self.original_source.goal_permission

    def verify(self, store, turn, snapshot) -> None:
        """Recheck the running original without binding it or consuming its wait again."""
        from .tool_denial import SelectedToolDenied

        if not turn.started_input or not turn.accepts_output:
            raise SelectedToolDenied("Native tool has no started original input")
        canonical = snapshot.canonical_name(self.thread.name)
        try:
            current = self.require_owner(snapshot, canonical)
            self._check_owner(
                current, snapshot, canonical, self.original_source, turn.command.message,
            )
            self._require_context(
                self.original_source, current, self.comms.goals.goal_wait(canonical),
            )
        except (ReservationViolationError, InputAlreadyCleared) as error:
            raise SelectedToolDenied(str(error)) from error

    def claim_resource(self, store, turn, resource, slot: ClaimId):
        with NativeBoundary(self.comms.bus) as boundary:
            snapshot = boundary.registry
            self.verify(store, turn, snapshot)
            owner = snapshot.require(self.owner_name)
            canonical = resource.normalized(Path(owner.worktree))
            scope = NativeClaim(owner_claimed_generation=self.claim_generation,
                                operation_id=slot, turn_id=self.turn,
                                input_id=turn.command.input_id)
            return boundary.bus.publisher._publish_acquisition_unlocked(
                snapshot, owner, canonical, scope, activity=boundary.activity)

    async def release_resources(self) -> None:
        """The ordinary turn owns its acquisitions through native/tool retirement."""
        def release() -> None:
            with NativeBoundary(self.comms.bus) as boundary:
                boundary.bus.publisher.release_resources(
                    boundary, tuple(self.claims.values()), "Released files after an ordinary turn"
                )

        await Coordination.run_worker(release)

    def original(self, native_id: InputId, text: str) -> bool:
        return self._started(self.original_source, native_id, text)

    def following(self, public_id: InputId, native_id: InputId, text: str) -> bool:
        return self._started(self.source(public_id), native_id, text)

    def _started(self, source: TurnInputSource, native_id: InputId, sent_text: str) -> bool:
        return source.valid_keys(sent_text) and all(
            self.inputs.dispositions.started(
                key, turn_id=self.turn, native_id=native_id, text=sent_text
            )
            for key in source.keys
        )
