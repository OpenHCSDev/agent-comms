"""Explicit idle-owner bridge for saved-session compaction (not a prompt turn)."""

from __future__ import annotations

import asyncio
import shutil
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from acp.schema import AgentMessageChunk, TextContentBlock

from . import agent_events as events
from . import backend, manual_compaction
from .activity import ActivityState
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
        # The legacy /compact helper hashes the separately installed Pi and
        # makes Pi commit its own summary. It cannot be an alternate writer of
        # the canonical PR95 root or bypass the owner journal/outbox.
        launcher = shutil.which(runner.agent_bin) or runner.agent_bin
        # Canonical pi-native may be invoked through a renamed symlink. Match
        # the resolved executable just as saved-session reopen does; spelling
        # alone cannot authorize the older unjournaled direct writer.
        if Path(launcher).resolve().name in {"pi-native", "pi-comms-native"}:
            return {
                "ok": False,
                "error": "Canonical native compaction requires the owner journal bridge.",
            }
        thread = runner.comms.registry.require(thread_name)
        if not thread.session_file:
            return {"ok": False, "error": "This thread has no saved session to compact."}
        # Pi holds an in-memory copy of the saved branch while idle. Close it
        # before the compaction writer acquires the session fence and rewrites
        # that branch; the next prompt will load the compacted file anew.
        if persistent := runner.persistent_backends.get(session_id):
            await persistent.close_idle()
        turn_id = f"compaction-{uuid4().hex}"
        task = asyncio.current_task()
        assert task is not None
        turn_claim = runner.comms.agents.begin_turn(thread_name, turn_id, "Compacting context")
        runner.active_turns[session_id] = turn_id
        runner.turn_tasks[session_id] = task
        started = False
        terminal_attempted = False
        try:
            runner.comms.agents.set_activity(thread_name, ActivityState.WORKING, "Compacting context")
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
            result = await manual_compaction.ManualCompaction(
                runner.agent_bin,
                backend.args_for_thinking_level(
                    backend.args_for_model(runner.agent_args, thread.model),
                    thread.thinking_level,
                ),
                thread.session_file,
                thread.worktree,
                instructions.strip() if instructions else None,
            ).run()
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
                        field_meta={"agentComms": {"transcriptChanged": True}},
                    ),
                )
            return result
        finally:
            if started and not terminal_attempted:
                with suppress(Exception, asyncio.CancelledError):
                    await runner.effects._emit_event(
                        session_id, events.ManualCompactionEnd(aborted=True)
                    )
            await runner.settle_turn(session_id, thread_name, turn_id, turn_claim, task=task)
