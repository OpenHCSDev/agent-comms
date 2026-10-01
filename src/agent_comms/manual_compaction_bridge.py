"""Explicit idle-owner bridge for saved-session compaction (not a prompt turn)."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING
from uuid import uuid4

from . import agent_events as events
from .compaction_errors import CompactionJournalError
from .compaction_result import CompactionResult, RefusedCompactionResult
from .turn_phase import CompactionPhase, PublishingPhase

if TYPE_CHECKING:
    from .turn_runner import TurnRunner


async def compact_context(
    runner: TurnRunner, session_id: str, instructions: str | None = None
) -> CompactionResult:
    """Call from a UI /compact handler only; no autonomous compaction/retry."""
    if instructions is not None and not isinstance(instructions, str):
        return RefusedCompactionResult("Compaction instructions are invalid.")
    thread_name = await runner.sessions.sync_identity(session_id)
    lock = runner.turn_locks.setdefault(session_id, asyncio.Lock())
    if lock.locked() or runner.session_busy(session_id):
        return RefusedCompactionResult("Wait for the current response before compacting.")
    async with lock:
        if runner.session_busy(session_id):
            return RefusedCompactionResult("Wait for the current response before compacting.")
        runner.effects._private_nk_marker()
        thread = runner.comms.registry.require(thread_name)
        if not thread.session_file:
            return RefusedCompactionResult("This thread has no saved session to compact.")
        turn_id = f"compaction-{uuid4().hex}"
        task = asyncio.current_task()
        assert task is not None
        turn_lease = runner.comms.agents.begin_turn(thread_name, turn_id, "Compacting context")
        runner.turn_tasks[session_id] = task
        started = False
        terminal_attempted = False
        try:
            await runner.transition_turn(session_id, turn_lease, CompactionPhase(resume=PublishingPhase()))
            info = runner.comms.agents.agent_info_of(thread_name)
            # An old usage sample cannot describe the context after a manual
            # compaction attempt, including one with an uncertain outcome.
            runner.comms.agents.set_agent_info(
                thread_name,
                model=info.model if info else thread.model,
                session_name=info.session_name if info else None,
                context_used=None,
                context_size=info.context_size if info else None,
            )
            started = True
            await runner.effects._emit_event(session_id, events.CompactionStart(reason="manual"))
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
            await runner.settle_turn(session_id, thread_name, turn_id, turn_lease, task=task)
