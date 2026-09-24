"""Owner-side idle compaction bridge; no automatic prompt or provider retry.

Kept separate from the streamed ACP turn parser so its frozen prompt-start
review can run while the explicit idle command is integrated independently.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any
from uuid import uuid4

from acp.schema import AgentMessageChunk, TextContentBlock

from . import manual_compaction
from .declarations import ActivityState


async def compact_context(
    agent: Any, session_id: str, instructions: str | None = None
) -> dict[str, Any]:
    """Run one explicitly requested Pi compact command for an idle ACP owner."""
    if instructions is not None and not isinstance(instructions, str):
        return {"ok": False, "error": "Compaction instructions are invalid or too long."}
    thread_name = await agent._sync_session_identity(session_id)
    lock = agent._turn_locks.setdefault(session_id, asyncio.Lock())
    if lock.locked() or session_id in agent._active_turns:
        return {"ok": False, "error": "Wait for the current response before compacting."}
    async with lock:
        if session_id in agent._active_turns:
            return {"ok": False, "error": "Wait for the current response before compacting."}
        thread = agent._comms.registry.require(thread_name)
        if not thread.session_file:
            return {"ok": False, "error": "This thread has no persisted context to compact."}

        turn_id = f"compaction-{uuid4().hex}"
        owner_task = asyncio.current_task()
        assert owner_task is not None
        agent._comms.begin_turn(thread_name, turn_id, "Compacting context")
        agent._active_turns[session_id] = turn_id
        agent._turn_tasks[session_id] = owner_task
        started = False
        terminal_attempted = False
        try:
            agent._comms.set_activity(thread_name, ActivityState.WORKING, "Compacting context")
            await agent._emit_event(session_id, {"type": "started", "turn_id": turn_id})
            # A prior usage sample cannot remain current after the compaction
            # command starts; even a failure leaves the present usage unknown.
            info = agent._comms.agent_info_of(thread_name)
            agent._comms.set_agent_info(
                thread_name,
                model=info.model if info else thread.model,
                session_name=info.session_name if info else None,
                context_used=None,
                context_size=info.context_size if info else None,
            )
            started = True
            await agent._emit_event(session_id, {"type": "compaction_start", "reason": "manual"})
            result = await manual_compaction.compact_session(
                agent._agent_bin,
                agent._agent_args,
                thread.session_file,
                thread.worktree,
                instructions.strip() if instructions and instructions.strip() else None,
            )
            success = result.get("ok") is True
            # A client can receive the terminal update and then raise. Never
            # manufacture a contradictory abort after delivery is uncertain.
            terminal_attempted = True
            await agent._emit_event(
                session_id,
                {
                    "type": "compaction_end",
                    "reason": "manual",
                    "aborted": not success,
                    "summary": result.get("summary") if success else None,
                    "will_retry": False,
                },
            )
            if success:
                await agent._runtime.session_update(
                    session_id=session_id,
                    update=AgentMessageChunk(
                        session_update="agent_message_chunk",
                        content=TextContentBlock(type="text", text=""),
                        field_meta={"agentComms": {"transcriptChanged": True}},
                    ),
                )
            return result
        finally:
            # Cancellation or an ACP/UI error must not turn an uncertain
            # compaction into a successful notice. Keep cleanup independent of
            # a caller cancelling again while the Pi process is being reaped.
            if started and not terminal_attempted:
                with suppress(Exception, asyncio.CancelledError):
                    await agent._emit_event(
                        session_id,
                        {
                            "type": "compaction_end",
                            "reason": "manual",
                            "aborted": True,
                            "will_retry": False,
                        },
                    )
            if agent._active_turns.get(session_id) == turn_id:
                agent._active_turns.pop(session_id, None)
            if agent._turn_tasks.get(session_id) is owner_task:
                agent._turn_tasks.pop(session_id, None)
            agent._comms.finish_turn(thread_name, turn_id)
            await agent._emit_event(session_id, {"type": "settled", "turn_id": turn_id})
