"""Explicit idle-owner bridge for saved-session compaction (not a prompt turn)."""

from __future__ import annotations

import asyncio
import shutil
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import uuid4

from acp.schema import AgentMessageChunk, TextContentBlock

from . import backend, manual_compaction
from .declarations import ActivityState


async def _emit_compaction(agent: Any, session_id: str, phase: str, summary: str = "") -> None:
    """Emit ACP metadata directly; published acp.py has no compaction event branch."""
    detail: dict[str, Any] = {
        "phase": phase,
        "status": {"start": "running", "end": "completed", "abort": "aborted"}[phase],
        "reason": "manual",
        "contextUsed": None,
        "contextState": "unknown",
        "willRetry": False,
    }
    if phase in {"end", "abort"} and summary:
        detail["summary"] = summary
    text = {
        "start": "",
        "end": "Context compacted; usage is recalculating.",
        "abort": "Context compaction aborted; usage is unknown.",
    }[phase]
    if phase == "end" and summary:
        text += f" Summary: {summary}"
    elif phase == "abort" and summary:
        text += f" {summary}"
    await agent._runtime.session_update(
        session_id=session_id,
        update=AgentMessageChunk(
            session_update="agent_message_chunk",
            content=TextContentBlock(type="text", text=text),
            field_meta={"agentComms": {"compaction": detail}},
        ),
    )


async def compact_context(
    agent: Any, session_id: str, instructions: str | None = None
) -> dict[str, Any]:
    """Call from a UI /compact handler only; no autonomous compaction/retry."""
    if instructions is not None and not isinstance(instructions, str):
        return {"ok": False, "error": "Compaction instructions are invalid."}
    thread_name = await agent._sync_session_identity(session_id)
    lock = agent._turn_locks.setdefault(session_id, asyncio.Lock())
    if lock.locked() or session_id in agent._active_turns:
        return {"ok": False, "error": "Wait for the current response before compacting."}
    async with lock:
        if session_id in agent._active_turns:
            return {"ok": False, "error": "Wait for the current response before compacting."}
        # The legacy /compact helper hashes the separately installed Pi and
        # makes Pi commit its own summary. It cannot be an alternate writer of
        # the canonical PR95 root or bypass the owner journal/outbox.
        launcher = shutil.which(agent._agent_bin) or agent._agent_bin
        # Canonical pi-native may be invoked through a renamed symlink. Match
        # the resolved executable just as saved-session reopen does; spelling
        # alone cannot authorize the older unjournaled direct writer.
        if Path(launcher).resolve().name == "pi-native":
            return {
                "ok": False,
                "error": "Canonical native compaction requires the owner journal bridge.",
            }
        thread = agent._comms.registry.require(thread_name)
        if not thread.session_file:
            return {"ok": False, "error": "This thread has no saved session to compact."}
        # Pi holds an in-memory copy of the saved branch while idle. Close it
        # before the compaction writer acquires the session fence and rewrites
        # that branch; the next prompt will load the compacted file anew.
        if persistent := getattr(agent, "_persistent_backends", {}).get(session_id):
            await persistent.close_idle()
        turn_id = f"compaction-{uuid4().hex}"
        task = asyncio.current_task()
        assert task is not None
        turn_claim = agent._comms.begin_turn(thread_name, turn_id, "Compacting context")
        agent._active_turns[session_id] = turn_id
        agent._turn_tasks[session_id] = task
        started = False
        terminal_attempted = False
        try:
            agent._comms.set_activity(thread_name, ActivityState.WORKING, "Compacting context")
            active = agent._comms.registry.require(thread_name).active_turn
            assert active is not None and active.id == turn_id
            await agent._emit_event(
                session_id,
                {
                    "type": "started",
                    "turn_id": turn_id,
                    "started_at": active.started_at,
                    "activity": "working",
                    "activity_detail": "Compacting context",
                },
            )
            info = agent._comms.agent_info_of(thread_name)
            # An old usage sample cannot describe the context after a manual
            # compaction attempt, including one with an uncertain outcome.
            agent._comms.set_agent_info(
                thread_name,
                model=info.model if info else thread.model,
                session_name=info.session_name if info else None,
                context_used=None,
                context_size=info.context_size if info else None,
            )
            started = True
            await _emit_compaction(agent, session_id, "start")
            result = await manual_compaction.compact_session(
                agent._agent_bin,
                backend.args_for_thinking_level(
                    backend.args_for_model(agent._agent_args, thread.model),
                    thread.thinking_level,
                ),
                thread.session_file,
                thread.worktree,
                instructions.strip() if instructions else None,
            )
            success = result.get("ok") is True
            # A client may receive this terminal event then raise. Do not send
            # a contradictory abort after an uncertain delivery.
            terminal_attempted = True
            await _emit_compaction(
                agent,
                session_id,
                "end" if success else "abort",
                result.get("summary", "") if success else result.get("error", ""),
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
            if started and not terminal_attempted:
                with suppress(Exception, asyncio.CancelledError):
                    await _emit_compaction(agent, session_id, "abort")
            if agent._active_turns.get(session_id) == turn_id:
                agent._active_turns.pop(session_id, None)
            if agent._turn_tasks.get(session_id) is task:
                agent._turn_tasks.pop(session_id, None)
            agent._comms.finish_turn(thread_name, turn_id, expected=turn_claim)
            await agent._emit_event(session_id, {"type": "settled", "turn_id": turn_id})
