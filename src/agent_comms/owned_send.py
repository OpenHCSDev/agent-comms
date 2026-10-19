"""Check an ordinary owner turn and hold its locks through native stdin.write."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .channel_coding_tools import CodingToolOwner
from .messages import ClaimNoticeKind

from .ordinary_turn_rules import (
    AcceptedInputCheck,
    InputKeysCheck,
    MissingAcceptedInputCheck,
    MissingTurnOwnerCheck,
    OrdinaryContextCheck,
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
            self.goal_permit.check_grant(self.goal_store)

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

    def claim_resource(self, store, turn, resource, slot, prior):
        from .store_files import _store_lock

        with _store_lock(self.comms._wire_lock_path):
            snapshot = self.comms.registry.snapshot()
            self.verify(store, turn, snapshot)
            owner = snapshot.require(self.owner_name)
            canonical = resource.normalized(Path(self.thread.worktree))
            with self.comms.bus.log.certified_read() as source:
                if prior is not None and source.claim(canonical) == prior:
                    return prior
            committed = self.comms.bus.publisher.publish_claim_envelope(
                ClaimNoticeKind.claimed(owner),
                worktree=Path(self.thread.worktree),
                incarnation=str(owner.created_at),
                claims=(resource,),
            )
            return committed.require_claim_transition().claimed(canonical)

    def native_start(self, public_id: str | None, native_id: str, sent_text: str) -> bool:
        source = self.source(public_id)
        return source.valid_keys(sent_text) and all(
            self.inputs.dispositions.started(
                key, turn_id=self.turn.value, native_id=native_id, text=sent_text
            )
            for key in source.keys
        )
