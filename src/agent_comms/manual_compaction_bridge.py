"""Explicit idle-owner /compact: one turn that asks the thread's backend to compact now."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from functools import partial
from typing import TYPE_CHECKING

from agent_comms.coordination_errors import RequestRefused
from acp.schema import AgentMessageChunk, PromptResponse, TextContentBlock

from . import agent_events as events
from .field_codec import TurnId
from .acp_extension import CompactionCommittedUpdate, TranscriptChangedUpdate, encode_updates
from .agent_backend import (
    CompactionOutcome,
    FailedCompaction,
    NothingToCompaction,
    PlacedCompaction,
    StaleCompaction,
)
from .agent_event_updates import TurnEventConsumer
from .coordinator import Coordination
from .mro_dispatch import MroDispatch, handles
from .pi_vocabulary import ManualCompactionReason
from .turn_phase import CompactionPhase, PublishingPhase

if TYPE_CHECKING:
    from .turn_runner import TurnRunner


class CompactionReply(MroDispatch):
    """The ACP reply to /compact for each outcome the backend reports."""

    def reply(self, outcome: CompactionOutcome) -> PromptResponse:
        handler = next(self.handlers_for(outcome), None)
        if handler is None:
            raise TypeError(f"/compact has no reply for {type(outcome).__name__}")
        return handler(outcome)

    @handles(PlacedCompaction)
    def placed(self, outcome: PlacedCompaction) -> PromptResponse:
        return PromptResponse(
            stop_reason="end_turn",
            field_meta=encode_updates(CompactionCommittedUpdate(outcome.summary, outcome.first_kept)),
        )

    @handles(NothingToCompaction)
    def nothing(self, outcome: NothingToCompaction) -> PromptResponse:
        raise RequestRefused.internal_error(
            {"reason": "nothing_to_compact"}, message="Nothing to compact."
        )

    @handles(FailedCompaction)
    def failed(self, outcome: FailedCompaction) -> PromptResponse:
        raise RequestRefused.internal_error({"reason": outcome.message}, message=outcome.message)

    @handles(StaleCompaction)
    def stale(self, outcome: StaleCompaction) -> PromptResponse:
        raise RequestRefused.internal_error(
            {"reason": "stale"}, message="The history moved past the summary."
        )


async def compact_context(
    runner: TurnRunner, session_id: str, instructions: str | None = None
) -> CompactionOutcome:
    """Run the backend's own compaction now; called from a UI /compact only."""
    if instructions is not None and not isinstance(instructions, str):
        return FailedCompaction("Compaction instructions are invalid.")
    thread_name = await runner.sessions.sync_identity(session_id)
    lock = runner.turn_locks.setdefault(session_id, asyncio.Lock())
    if lock.locked() or await Coordination.run_worker(partial(runner.session_busy, session_id)):
        return FailedCompaction("Wait for the current response before compacting.")
    async with lock:
        if await Coordination.run_worker(partial(runner.session_busy, session_id)):
            return FailedCompaction("Wait for the current response before compacting.")
        thread = await Coordination.run_worker(partial(runner.comms.registry.require, thread_name))
        return await thread.writable_runtime.compact(partial(
            _compact_started, runner, session_id, thread_name, instructions,
        ))


async def _compact_started(runner, session_id, thread_name, instructions):
    task = asyncio.current_task()
    assert task is not None
    async with AsyncExitStack() as resources:
        owner = await Coordination.run_worker(partial(
            runner.acquire_turn, resources, session_id, thread_name,
            TurnId.mint(), "Compacting context", task=task,
        ))
        runner.turn_tasks[session_id] = task
        await runner.transition_turn(
            session_id, owner.turn_lease, CompactionPhase(resume=PublishingPhase())
        )
        consumer = TurnEventConsumer(
            runner.effects, session_id, runner.runtime, owner.turn_lease.turn_id
        )
        await consumer.dispatch(events.CompactionStart(reason=ManualCompactionReason))
        outcome = FailedCompaction("Compaction did not finish.")
        try:
            outcome = await runner.backend_for(session_id, owner.thread).compact(
                owner.thread, instructions.strip() if instructions else None,
            )
        finally:
            completion = outcome.completion_event(events.CompactionEnd, ManualCompactionReason)
            await consumer.dispatch(completion)
        if not completion.aborted:
            await runner.runtime.publish_update(
                session_id=session_id,
                update=AgentMessageChunk(
                    session_update="agent_message_chunk",
                    content=TextContentBlock(type="text", text=""),
                    field_meta=encode_updates(TranscriptChangedUpdate(None)),
                ),
            )
        return outcome
