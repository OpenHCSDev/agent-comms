"""Bind a selected send claim to the backend's normal turn entry."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING

from .agent_backend import InputContent, TurnDelivery, TurnInbox
from .agent_events import Done
from .coordination_errors import IdentityConflict
from .coordinator import Coordination
from .diagnostics import record_request_progress
from .following_inputs import FollowingInputs
from .native_arguments import SelectedModel
from .native_pi import NativePiUnavailable, NativeTurnResult

if TYPE_CHECKING:
    from .coordinated_runtime import SelectedExecution
    from .private_send_claim import PrivateSendClaim
    from .tracked_turn import NativeObserver
from .send_outcome import GrantedSend


class NoSelectedAnswer:
    def require(self):
        raise NativePiUnavailable("Selected turn ended without a final answer")


@dataclass(frozen=True, kw_only=True)
class SelectedTurnDelivery(TurnDelivery):
    claim: PrivateSendClaim
    execution: SelectedExecution
    model: SelectedModel
    observe_event: NativeObserver
    following: FollowingInputs

    def request_id(self):
        return self.claim.input_id

    async def prepare_identity(self, identity):
        await Coordination.run_worker(partial(self.claim._start_once, identity))

    @staticmethod
    @contextmanager
    def boundary(claim, public_id, native_id, text):
        if public_id is not None or native_id != claim.input_id or text != claim.prompt.text:
            raise IdentityConflict("Selected write differs from its original claim")
        yield GrantedSend()

    async def native_events(self, backend, thread, content):
        result = await backend.run_tracked_turn(
            self.claim,
            self.execution,
            model=self.model,
            selected_tool_mode=self.selected_tool_mode,
            observe_event=self.observe_event,
            following=self.following,
        )
        yield SelectedDone(text=result.text, result=result)

    @classmethod
    async def execute(
        cls, claim, execution, *, model, selected_tool_mode, observe_event, following
    ):
        thread = replace(
            claim.owner.thread, session_file=str(claim.session.path), model=model.qualified
        )
        registry = await Coordination.run_worker(claim.bus._registry.snapshot)
        environment = thread.native_environment(
            claim.bus.log.path.parent, registry, thread.worktree
        )
        delivery = cls(
            claim=claim,
            execution=execution,
            model=model,
            observe_event=observe_event,
            following=following,
            worktree=thread.worktree,
            environment=environment,
            inbox=TurnInbox(),
            finish_event=asyncio.Event(),
            send_boundary=partial(cls.boundary, claim),
            interrupt_boundary=partial(cls.boundary, claim),
            input_started=lambda public_id, native_id, text: (
                native_id == claim.input_id and text == claim.prompt.text
            ),
            request_progress=partial(
                record_request_progress,
                claim.bus.log.path.parent,
                claim.owner.thread.require_turn_lease(),
            ),
            ui_request=None,
            selected_tool_mode=selected_tool_mode,
        )
        result = NoSelectedAnswer()
        async for event in execution.pi_session.run_turn(
            thread,
            InputContent(text=claim.prompt.text, contributions=claim.prompt.contributions),
            delivery,
        ):
            await observe_event(event)
            result = event.selected_result(claim.session.path, result)
        return result.require()


@dataclass(frozen=True, kw_only=True)
class SelectedDone(Done):
    result: NativeTurnResult

    def selected_result(self, path, current):
        return self.result
