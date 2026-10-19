"""Restored native sessions use per-input tools for goal and DM turns."""
import asyncio
import json

from agent_comms.tracked_turn import TrackedTurnSession
from agent_comms.native_tools import NativeToolDeclaration
from delivery_owner_fixture import reply_turn_finished


async def test_goal_continuation_calls_comms_goal(native_backend, monkeypatch):
    native = native_backend
    await native.author_history()
    original = native.session.read_bytes()
    goal_id = ""

    def chunks():
        assert native.provider.posts <= 2, "Blocked goal must not start another input"
        if native.provider.posts == 1:
            yield {"tool_calls": [{"index": 0, "id": "goal-connection-check", "type": "function",
                "function": {"name": "comms_goal", "arguments": json.dumps({
                    "goal_id": goal_id, "status": "blocked",
                    "progress": "Private socket verification finished."
                })}}]}, None
            yield {}, "tool_calls"
        else:
            yield {"content": "The private goal was blocked through comms_goal."}, None
            yield {}, "stop"

    monkeypatch.setattr(native.provider, "response_chunks", chunks)
    async with native.open_owner(runtime_enabled=True, auto_wake=True,
                                 native_options=NativeToolDeclaration.extension_arguments(native.package.path)) as (owner, session):
        goal = await owner.turns.goals.set_goal(session, "Verify one native comms_goal call, then block this private goal.")
        goal_id = goal.id
        await asyncio.wait_for(reply_turn_finished(owner, session), 30)
        current = owner._comms.goals.goal_snapshot(owner.sessions.require(session)).goal
        assert current.state.declared_name == "blocked"
        rows = [json.loads(line) for line in native.session.read_text().splitlines()]
        results = [row["message"] for row in rows if row.get("message", {}).get("role") == "toolResult"]
        assert len(results) == 1 and not results[0].get("isError"), results
        assert native.session.read_bytes().startswith(original)
        print(json.dumps({"goal_id": goal_id, "tool": "comms_goal", "state": "blocked",
                          "original_preserved": True, "provider": "controlled localhost"}))


async def test_preopened_native_process_reads_through_input_connection(native_backend, monkeypatch):
    native = native_backend
    await native.author_history()
    original = native.session.read_bytes()
    (native.project / "connection-read.txt").write_text("P608 input connection reached the read tool\n")
    pids = []
    acquire = TrackedTurnSession.acquire_native

    async def preopened(turn, resources, *, reuse):
        before = await turn.native_session.open(
            turn.launch, reuse=True, startup=turn.startup, watchdog=turn.watchdog,
        )
        turn.native = before
        turn.watchdog.reading()
        await turn.attest()
        assert turn.native_session.retain(before, before.attestation.identity)
        after = await acquire(turn, resources, reuse=reuse)
        assert before is after
        pids.append(before.proc.pid)
        return after

    def chunks():
        if native.provider.posts == 1:
            yield {"tool_calls": [{"index": 0, "id": "input-connection-read", "type": "function",
                "function": {"name": "read", "arguments": json.dumps({"path": "connection-read.txt"})}}]}, None
            yield {}, "tool_calls"
        else:
            yield {"content": "Read completed through the input connection."}, None
            yield {}, "stop"

    monkeypatch.setattr(TrackedTurnSession, "acquire_native", preopened)
    monkeypatch.setattr(native.provider, "response_chunks", chunks)
    async with native.open_owner(runtime_enabled=True, auto_wake=True,
                                 native_options=NativeToolDeclaration.extension_arguments(native.package.path)) as (owner, session):
        comms = owner._comms
        comms.threads.claim_thread("verifier", tags=frozenset(), worktree=str(native.project))
        comms.messaging.send("verifier", session, "Fresh per-input connection verification").message
        await owner.inputs.drain_inbox(session)
        await asyncio.wait_for(reply_turn_finished(owner, session), 30)
        rows = [json.loads(line) for line in native.session.read_text().splitlines()]
        results = [row["message"] for row in rows if row.get("message", {}).get("role") == "toolResult"]
        assert len(results) == 1 and not results[0].get("isError"), results
        assert "P608 input connection reached the read tool" in results[0]["content"][0]["text"]
        assert len(pids) == 1
        assert native.session.read_bytes().startswith(original)
        print(json.dumps({"preexisting_native_pid": pids[0], "fresh_dms": 1, "tool": "read",
                          "result": "P608 input connection reached the read tool",
                          "original_preserved": True, "provider": "controlled localhost"}))
