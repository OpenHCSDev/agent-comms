"""Persist actual native turn observations under the current attempt fence.

S2 owns excursion interpretation. This consumer projects its model/tool/compact
phases onto the existing durable lifecycle; it never sends, retries or executes
tools. Recovery-only historical phases remain readable without manufacturing
recovery events from a successful result.
"""

from . import pi_events as pi
from .agent_events import NativePhaseChanged
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
from .mro_dispatch import MroDispatch, handles
from .turn_phase import CompactionPhase, ModelWaitPhase, ToolRunningPhase


class DurableTurn(MroDispatch):
    def __init__(self, attempts: AttemptStore, fence, pointer_revision, input_id):
        self.attempts = attempts
        self.fence = fence
        self.pointer_revision = pointer_revision
        self.input_id = input_id

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

    @handles(pi.Response)
    async def accepted(self, event):
        if event.id == "native-prompt" and event.success and self.current.starting:
            self.advance(PromptAcceptedAttempt)

    @handles(pi.ContextCommitted)
    async def context(self, event):
        if event.input_id != self.input_id or self.model_started:
            return
        if self.current.starting:
            self.advance(PromptAcceptedAttempt)
        self.advance(ModelRunningAttempt)

    @handles(NativePhaseChanged)
    async def excursion(self, event):
        if self.model_started:
            await self.dispatch(event.phase)

    @handles(ModelWaitPhase)
    async def model(self, phase):
        self.advance(ModelRunningAttempt)

    @handles(ToolRunningPhase)
    async def tool(self, phase):
        self.advance(ToolRunningAttempt)

    @handles(CompactionPhase)
    async def compaction(self, phase):
        self.advance(CompactionAttempt)

    def finish(self):
        """Native child is reaped and any explicit owner effect has completed."""
        if not self.model_started:
            raise IdentityConflict("native completion lacks model progress")
        self.advance(SettlingAttempt)
        self.fence = self.attempts.advance(
            self.fence,
            SettlingAttempt,
            expected_pointer_revision=self.pointer_revision,
            backend_done=True,
            process_dead=True,
        ).value.fence
        return self.fence

    def fail_unknown(self):
        """Retire the reaped local backend without granting acceptance or replay.

        The caller holds the live registry owner boundary. Native execution has
        returned through child/tool cleanup; possible remote effects stay UNKNOWN.
        Coordination owns the atomic replay/finality/slot mutation.
        """
        return self.attempts.fail_unknown(
            self.fence,
            expected_pointer_revision=self.pointer_revision,
        ).value

    def fail_terminal(self):
        """Settle the corroborated failed input after its native child is reaped."""
        final = self.attempts.advance(
            self.fence,
            self.current,
            expected_pointer_revision=self.pointer_revision,
            backend_done=True,
            process_dead=True,
            reason_code="native_terminal_failure",
        ).value
        return self.attempts.settle_nonpublication(
            final.fence,
            expected_pointer_revision=final.snapshot.pointer_revision,
            outcome=AttemptFailedAttempt(),
            reason_code="native_terminal_failure",
        ).value
