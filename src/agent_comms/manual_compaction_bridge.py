"""Explicit idle-owner bridge for saved-session compaction (not a prompt turn)."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, suppress
from functools import partial
from typing import TYPE_CHECKING
from uuid import uuid4

from . import agent_events as events
from .compaction_errors import CompactionJournalError
from .compaction_result import CompactionResult, RefusedCompactionResult
from .coordinator import Coordination
from .native_input_owner import RegistryOwner
from .pi_vocabulary import ManualCompactionReason
from .turn_phase import CompactionPhase, PublishingPhase

if TYPE_CHECKING:
    from .turn_runner import TurnRunner


def prepare_compaction_turn(
    runner: TurnRunner, resources: AsyncExitStack, session_id: str,
    thread_name: str, turn_id: str, task: asyncio.Task,
) -> RegistryOwner:
    owner = runner.acquire_turn(
        resources, session_id, thread_name, turn_id, "Compacting context", task=task,
    )
    info = runner.comms.agents.agent_info_of(thread_name)
    # A previous usage sample cannot describe this manual attempt, even if
    # its outcome becomes uncertain. The original lease cleanup is already owned.
    runner.comms.agents.set_agent_info(
        thread_name,
        model=info.model if info else owner.thread.model,
        session_name=info.session_name if info else None,
        context_used=None,
        context_size=info.context_size if info else None,
    )
    return owner


async def compact_context(
    runner: TurnRunner, session_id: str, instructions: str | None = None
) -> CompactionResult:
    """Call from a UI /compact handler only; no autonomous compaction/retry."""
    if instructions is not None and not isinstance(instructions, str):
        return RefusedCompactionResult("Compaction instructions are invalid.")
    thread_name = await runner.sessions.sync_identity(session_id)
    lock = runner.turn_locks.setdefault(session_id, asyncio.Lock())
    if lock.locked() or await Coordination.run_worker(partial(runner.session_busy, session_id)):
        return RefusedCompactionResult("Wait for the current response before compacting.")
    async with lock:
        if await Coordination.run_worker(partial(runner.session_busy, session_id)):
            return RefusedCompactionResult("Wait for the current response before compacting.")
        await Coordination.run_worker(runner.effects._private_nk_marker)
        thread = await Coordination.run_worker(partial(runner.comms.registry.require, thread_name))
        if not thread.session_file:
            return RefusedCompactionResult("This thread has no saved session to compact.")
        turn_id = f"compaction-{uuid4().hex}"
        task = asyncio.current_task()
        assert task is not None
        async with AsyncExitStack() as resources:
            started = False
            terminal_attempted = False
            try:
                owner = await Coordination.run_worker(partial(
                    prepare_compaction_turn, runner, resources, session_id,
                    thread_name, turn_id, task,
                ))
                thread, turn_lease = owner.thread, owner.turn_lease
                runner.turn_tasks[session_id] = task
                await runner.transition_turn(session_id, turn_lease, CompactionPhase(resume=PublishingPhase()))
                started = True
                await runner.effects._emit_event(
                    session_id, events.CompactionStart(reason=ManualCompactionReason)
                )
                from .owner_compaction_manual import compact_manual_owner

                prepared = await runner.prepare_selected_session(session_id, thread)
                result = await compact_manual_owner(runner, session_id, thread_name, prepared, instructions)
                # Attempt terminal delivery once; an uncertain delivery cannot emit an abort.
                terminal_attempted = True
                await runner.effects._emit_event(session_id, result.terminal_event())
                await result.after_terminal(runner, session_id)
                return result
            except (ValueError, CompactionJournalError) as error:
                return RefusedCompactionResult(str(error))
            finally:
                if started and not terminal_attempted:
                    with suppress(Exception, asyncio.CancelledError):
                        await runner.effects._emit_event(
                            session_id, events.ManualCompactionEnd(aborted=True)
                        )
