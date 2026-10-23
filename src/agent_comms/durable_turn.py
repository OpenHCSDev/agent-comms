"""Persist actual native turn observations under the current attempt fence.

S2 owns excursion interpretation. This consumer projects its model/tool/compact
phases onto the existing durable lifecycle; it never sends, retries or executes
tools. Recovery-only historical phases remain readable without manufacturing
recovery events from a successful result.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial

from agent_comms.reason_codes import GivenReasonCode
from . import pi_events as pi
from .agent_events import NativePhaseChanged, PromptAccepted
from .attempt_states import (
    AttemptFailedAttempt,
    CompactionAttempt,
    ModelRunningAttempt,
    PromptAcceptedAttempt,
    SettlingAttempt,
    ToolRunningAttempt,
)
from .attempt_store import AttemptStore
from .coordination_errors import IdentityConflict
from .coordination_tables.attempts import ReplayFact
from .coordination_tables.executions import ReplayAssessments
from .coordination_session import NativeBoundary
from .coordinator import Coordination
from .mro_dispatch import MroDispatch, handles
from .owner_fence import OwnerFence
from .turn_failure import TurnFailure
from .turn_phase import CompactionPhase, ModelWaitPhase, ToolRunningPhase


@dataclass(eq=False, repr=False)
class DurableTurn(MroDispatch):
    attempts: AttemptStore
    fence: OwnerFence
    pointer_revision: int
    input_id: str

    @contextmanager
    def using_attempts(self, attempts):
        """Borrow only this operation's connection; keep the original fence owner."""
        previous = self.attempts
        self.attempts = attempts
        try:
            yield
        finally:
            self.attempts = previous

    async def consume_handlers(self, event, handlers, *args, **kwargs):
        return await Coordination.run_async(
            self.attempts.session.path, partial(self.observe_owned, event, handlers, *args, **kwargs)
        )

    def observe_owned(self, event, handlers, *args, **kwargs):
        """Consume one native observation through joined, worker-owned SQLite."""
        *context, resource = args
        with self.using_attempts(resource):
            return self.consume_handlers_sync(event, handlers, *context, **kwargs)

    @property
    def current(self):
        return type(self.attempts.require_fence(self.fence)[1].lifecycle)

    @property
    def model_started(self):
        return self.current.has_model_context

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
        if self.current.starting:
            self.advance(PromptAcceptedAttempt)

    @handles(pi.ContextCommitted)
    def context(self, event):
        if event.input_id != self.input_id or self.model_started:
            return
        if self.current.starting:
            self.advance(PromptAcceptedAttempt)
        self.advance(ModelRunningAttempt)

    def record_response_fact(self, fact: ReplayFact) -> None:
        """Retain output evidence with the attempt facts that already own replay safety."""
        ReplayAssessments.accumulate(
            self.attempts.session._connection, self.fence.execution_id, fact
        )

    @handles(pi.MessageUpdate)
    def response_output(self, event):
        delta = event.assistant_message_event
        if delta.response_progress and delta.progress:
            self.record_response_fact(ReplayFact.ASSISTANT_OUTPUT)

    @handles(pi.ToolExecutionStart)
    def tool_output(self, event):
        self.record_response_fact(ReplayFact.TOOL_CALL_STARTED)

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

    def fail_unknown(self, boundary: NativeBoundary):
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
