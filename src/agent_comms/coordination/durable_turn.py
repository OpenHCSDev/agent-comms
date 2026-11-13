"""Persist actual native turn observations under the current attempt fence.

S2 owns excursion interpretation. This consumer projects its model/tool/compact
phases onto the existing durable lifecycle; it never sends, retries or executes
tools. Recovery-only historical phases remain readable without manufacturing
recovery events from a successful result.
"""

from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial

from metaclass_registry.mro_dispatch import Handler, MroDispatch, Value, handles

from agent_comms.agent_events import PromptAccepted
from agent_comms.attempt_store import AttemptStore
from agent_comms.coordination.attempt_states import (
    AttemptFailedAttempt,
    CompactionAttempt,
    ModelRunningAttempt,
    SettlingAttempt,
    ToolRunningAttempt,
)
from agent_comms.coordination.coordinator import Coordination
from agent_comms.coordination.errors import IdentityConflict
from agent_comms.coordination.owner_fence import OwnerFence
from agent_comms.coordination.reason_codes import GivenReasonCode
from agent_comms.coordination.session import NativeCapture
from agent_comms.coordination.tables.attempts import ReplayFact
from agent_comms.coordination.tables.executions import ReplayAssessments
from agent_comms.execution import pi_events as pi
from agent_comms.execution.turn_failure import TurnFailure
from agent_comms.execution.turn_phase import (
    CompactionPhase,
    ModelWaitPhase,
    NativePhaseChanged,
    ToolRunningPhase,
)
from agent_comms.nominal.field_codec import InputId


class ReplayEvidence(MroDispatch):
    """The replay fact one native observation proves, read from the observation alone."""

    @handles(object)
    @classmethod
    def nothing(cls, event) -> ReplayFact:
        return ReplayFact.NONE

    @handles(pi.MessageUpdate)
    @classmethod
    def reply_text(cls, event) -> ReplayFact:
        delta = event.assistant_message_event
        proved = delta.response_progress and delta.progress
        return ReplayFact.ASSISTANT_OUTPUT if proved else ReplayFact.NONE

    @handles(pi.ToolExecutionStart)
    @classmethod
    def tool_call(cls, event) -> ReplayFact:
        return ReplayFact.TOOL_CALL_STARTED

    @classmethod
    def of(cls, event) -> ReplayFact:
        return cls.handler_for(type(event))(event)


@dataclass(eq=False, repr=False)
class DurableTurn(MroDispatch):
    attempts: AttemptStore
    fence: OwnerFence
    pointer_revision: int
    input_id: InputId
    joined: ReplayFact = ReplayFact.NONE
    """Replay facts this turn has joined to its execution. The assessment row only
    accumulates them and is never deleted (ReplayAssessments), so more evidence of a joined
    fact has nothing left to record: a streamed reply proves its output once, not per token."""

    @contextmanager
    def using_attempts(self, attempts):
        """Borrow only this operation's connection; keep the original fence owner."""
        previous = self.attempts
        self.attempts = attempts
        try:
            yield
        finally:
            self.attempts = previous

    async def dispatch(self, event: Value, *args, **kwargs) -> Value | None:
        proved = ReplayEvidence.of(event) & ~self.joined
        if proved:
            await Coordination.run_async(
                self.attempts.session.path, partial(self.join_owned, proved))
        return await super().dispatch(event, *args, **kwargs)

    def join_owned(self, proved: ReplayFact, resource) -> None:
        """Retain output evidence with the attempt facts that already own replay safety."""
        with self.using_attempts(resource):
            ReplayAssessments.accumulate(
                self.attempts.session._connection, self.fence.execution_id, proved)
        self.joined |= proved

    async def consume_handlers(
        self, event: Value, handlers: Iterable[Handler], *args, **kwargs,
    ) -> Value | None:
        return await Coordination.run_async(
            self.attempts.session.path, partial(self.observe_owned, event, handlers, *args, **kwargs)
        )

    def observe_owned(
        self, event: Value, handlers: Iterable[Handler], *args, **kwargs,
    ) -> Value | None:
        """Consume one native observation through joined, worker-owned SQLite."""
        *context, resource = args
        with self.using_attempts(resource):
            return self.consume_handlers_sync(event, handlers, *context, **kwargs)

    @property
    def current(self):
        return type(self.attempts.require_fence(self.fence)[1].lifecycle)

    @property
    def model_started(self):
        return self.attempts.require_fence(self.fence)[1].lifecycle.has_model_context

    def advance(self, state):
        if state is self.current:
            return
        result = self.attempts.advance(
            self.fence,
            state,
            expected_pointer_revision=self.pointer_revision,
            progress=True,
        ).value
        self.fence = result.fence

    @handles(PromptAccepted)
    def accepted(self, event):
        self.current.advance_on_accepted(self.advance)

    @handles(pi.ContextCommitted)
    def context(self, event):
        if event.input_id != self.input_id or self.model_started:
            return
        self.current.advance_on_accepted(self.advance)
        self.advance(ModelRunningAttempt)

    @handles(NativePhaseChanged)
    def excursion(self, event):
        if self.model_started:
            self.dispatch_sync(event.phase)

    @handles(ModelWaitPhase)
    def model(self, phase):
        self.advance(ModelRunningAttempt)

    @handles(ToolRunningPhase)
    def tool(self, phase):
        self.advance(ToolRunningAttempt)

    @handles(CompactionPhase)
    def compaction(self, phase):
        self.advance(CompactionAttempt)

    def finish(self):
        """Native child is reaped and any explicit owner effect has completed."""
        if not self.model_started:
            raise IdentityConflict("native completion lacks model progress")
        self.fence = self.attempts.advance(
            self.fence,
            SettlingAttempt,
            expected_pointer_revision=self.pointer_revision,
            backend_done=True,
            process_dead=True,
            progress=True,
        ).value.fence
        return self.fence

    def fail_unknown(self, boundary: NativeCapture):
        """Retire the reaped local backend without granting acceptance or replay.

        The caller holds the live registry owner boundary. Native execution has
        returned through child/tool cleanup; possible remote effects stay UNKNOWN.
        Coordination owns the atomic replay/finality/slot mutation.
        """
        return self.attempts.fail_unknown(
            self.fence,
            expected_pointer_revision=self.pointer_revision,
            boundary=boundary,
        ).value

    def fail_terminal(self, failure: TurnFailure):
        """Settle the corroborated failed input after its native child is reaped."""
        final = self.attempts.advance(
            self.fence,
            self.current,
            expected_pointer_revision=self.pointer_revision,
            backend_done=True,
            process_dead=True,
            reason_code=GivenReasonCode("native_terminal_failure"),
        ).value
        return self.attempts.settle_nonpublication(
            final.fence,
            expected_pointer_revision=final.snapshot.pointer_revision,
            outcome=AttemptFailedAttempt(),
            reason_code=GivenReasonCode("native_terminal_failure"),
            failure=failure,
        ).value
