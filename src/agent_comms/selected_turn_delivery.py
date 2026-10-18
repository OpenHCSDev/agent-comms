"""Bind a selected send claim to the backend's normal turn entry."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .agent_backend import InputContent, TurnDelivery, TurnInbox
from .agent_events import AgentEvent, UnresolvedSelectedResult
from .coordination_errors import IdentityConflict
from .coordinator import Coordination
from .diagnostics import record_request_progress
from .extension_ui import NoUiRequest
from .following_inputs import FollowingInputs
from .maintenance_barrier import MaintenanceBarrier
from .native_arguments import SelectedModel
from .native_pi import NativeTurnResult
from .native_startup import NativeStartup
from .tracked_turn import TrackedDone, TrackedTurnSession

if TYPE_CHECKING:
    from .coordinated_runtime import SelectedExecution
    from .native_session_reopen import NativeSessionIdentity
    from .private_send_claim import PrivateSendClaim
    from .threads import Thread
    from .tracked_turn import NativeObserver
from .send_outcome import GrantedSend


@dataclass(frozen=True, kw_only=True)
class SelectedTurnDelivery(TurnDelivery):
    claim: PrivateSendClaim
    execution: SelectedExecution
    model: SelectedModel
    observe_event: NativeObserver
    following: FollowingInputs

    def request_id(self) -> str:
        return self.claim.input_id

    async def prepare_identity(self, identity: NativeSessionIdentity) -> None:
        await Coordination.run_worker(partial(self.claim._start_once, identity))

    async def run_native(
        self, backend: Any, thread: Thread, content: InputContent
    ) -> AsyncIterator[AgentEvent]:
        claim = self.claim
        # The tracked turn owns the native child. Its callback crosses into this
        # stream one event at a time, after the selected observer has consumed it.
        pending: asyncio.Queue[tuple[AgentEvent, asyncio.Future[None]] | None] = asyncio.Queue()

        async def publish(event: AgentEvent):
            acknowledged = asyncio.get_running_loop().create_future()
            await pending.put((event, acknowledged))
            await acknowledged

        async def observe(event):
            await event.observe_selected(self.observe_event, publish)

        async def produce():
            try:
                result = await TrackedTurnSession.execute(
                    self.execution.native_package,
                    input_id=claim.input_id,
                    prompt=content.text,
                    context_contributions=content.contributions,
                    worktree=Path(thread.worktree).absolute(),
                    session=claim.session,
                    model=self.model,
                    thinking_level=thread.thinking_level,
                    environment=self.environment,
                    ingress=MaintenanceBarrier(claim.bus._registry.store.path),
                    startup=NativeStartup(
                        claim.bus.log.path.parent, measurements=claim._measurements
                    ),
                    selected_tool_mode=self.selected_tool_mode,
                    observe_event=observe,
                    request_observer=partial(
                        record_request_progress,
                        claim.bus.log.path.parent,
                        thread.require_turn_lease(),
                    ),
                    prompt_send_boundary=claim,
                    execution=self.execution,
                    following=self.following,
                )
                await observe(TrackedDone(text=result.text, ok=True, proof=result.context))
            finally:
                pending.put_nowait(None)

        task = asyncio.create_task(produce())
        try:
            while (item := await pending.get()) is not None:
                event, acknowledged = item
                try:
                    yield event
                finally:
                    if not acknowledged.done():
                        acknowledged.set_result(None)
            await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    @staticmethod
    @contextmanager
    def boundary(
        claim: PrivateSendClaim, public_id: str | None, native_id: str, text: str
    ) -> Iterator[GrantedSend]:
        if public_id is not None or native_id != claim.input_id or text != claim.prompt.text:
            raise IdentityConflict("Selected write differs from its original claim")
        yield GrantedSend()

    @classmethod
    async def execute(
        cls,
        claim: PrivateSendClaim,
        execution: SelectedExecution,
        *,
        model: SelectedModel,
        selected_tool_mode: Any,
        observe_event: NativeObserver,
        following: FollowingInputs,
    ) -> NativeTurnResult:
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
            ui_request=NoUiRequest(),
            selected_tool_mode=selected_tool_mode,
        )
        result: UnresolvedSelectedResult | NativeTurnResult = UnresolvedSelectedResult()
        async for event in execution.backend.run_turn(
            thread,
            InputContent(text=claim.prompt.text, contributions=claim.prompt.contributions),
            delivery,
        ):
            await observe_event(event)
            result = event.selected_result(claim.session.path, result)
        return result.require()
