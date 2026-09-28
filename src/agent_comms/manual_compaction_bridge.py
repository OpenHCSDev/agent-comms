"""Explicit idle-owner bridge for saved-session compaction (not a prompt turn)."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from acp.schema import AgentMessageChunk, TextContentBlock

from . import agent_events as events
from .acp_extension import TranscriptChangedUpdate, encode_updates
from .activity import ActivityState
from .compaction_journal import CompactionJournalError
from .transcript_updates import StartedTranscriptUpdate

if TYPE_CHECKING:
    from .turn_runner import TurnRunner


async def compact_context(
    runner: TurnRunner, session_id: str, instructions: str | None = None
) -> dict[str, Any]:
    """Call from a UI /compact handler only; no autonomous compaction/retry."""
    if instructions is not None and not isinstance(instructions, str):
        return {"ok": False, "error": "Compaction instructions are invalid."}
    thread_name = await runner.sessions.sync_identity(session_id)
    lock = runner.turn_locks.setdefault(session_id, asyncio.Lock())
    if lock.locked() or session_id in runner.active_turns:
        return {"ok": False, "error": "Wait for the current response before compacting."}
    async with lock:
        if session_id in runner.active_turns:
            return {"ok": False, "error": "Wait for the current response before compacting."}
        runner.effects._private_nk_marker()
        thread = runner.comms.registry.require(thread_name)
        if not thread.session_file:
            return {"ok": False, "error": "This thread has no saved session to compact."}
        turn_id = f"compaction-{uuid4().hex}"
        task = asyncio.current_task()
        assert task is not None
        turn_lease = runner.comms.agents.begin_turn(thread_name, turn_id, "Compacting context")
        runner.active_turns[session_id] = turn_id
        runner.turn_tasks[session_id] = task
        started = False
        terminal_attempted = False
        try:
            runner.comms.agents.set_activity(
                thread_name, ActivityState.WORKING, "Compacting context"
            )
            active = runner.comms.registry.require(thread_name).active_turn
            assert active is not None and active.id == turn_id
            await runner.effects._emit_event(
                session_id,
                StartedTranscriptUpdate(
                    turn_id=turn_id,
                    started_at=active.started_at,
                    activity="working",
                    activity_detail="Compacting context",
                ),
            )
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

            info = await runner.prepare_selected_session(session_id, thread)
            result = await compact_manual_owner(runner, session_id, thread_name, info, instructions)
            success = result.get("ok") is True
            # A client may receive this terminal event then raise. Do not send
            # a contradictory abort after an uncertain delivery.
            terminal_attempted = True
            await runner.effects._emit_event(
                session_id,
                events.ManualCompactionEnd(
                    aborted=not success,
                    summary=result.get("summary", "") if success else result.get("error", ""),
                ),
            )
            if success:
                await runner.runtime.session_update(
                    session_id=session_id,
                    update=AgentMessageChunk(
                        session_update="agent_message_chunk",
                        content=TextContentBlock(type="text", text=""),
                        field_meta=encode_updates(TranscriptChangedUpdate(None)),
                    ),
                )
            return result
        except (ValueError, CompactionJournalError) as error:
            return {"ok": False, "error": str(error)}
        finally:
            if started and not terminal_attempted:
                with suppress(Exception, asyncio.CancelledError):
                    await runner.effects._emit_event(
                        session_id, events.ManualCompactionEnd(aborted=True)
                    )
            await runner.settle_turn(session_id, thread_name, turn_id, turn_lease, task=task)
