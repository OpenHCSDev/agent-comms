"""Real native Pi and owner socket: cancelled goals need an explicit fresh grant."""

import asyncio
import json
import os

import pytest
from acp.schema import SessionNotification

from agent_comms.field_codec import FieldCodec
from agent_comms.goal_attempts import GoalAttemptStore
from agent_comms.goal_generation import BlockedGeneration, ReadyGeneration, ReservedGeneration
from agent_comms.goals import Goal
from agent_comms.input_attempt import StartedInput
from agent_comms.runtime import RuntimeProxy, socket_path
from compaction_loopback import LoopbackProvider
from native_backend_fixture import native_backend_fixture
from stored_inputs import stored_inputs


class PausedChunksProvider(LoopbackProvider):
    completion_gate: asyncio.Event

    async def after_chunk(self) -> None:
        await self.completion_gate.wait()


@pytest.mark.skipif(os.name == "nt", reason="POSIX owner socket")
async def test_native_cancelled_goal_resume_requires_retry_before_fresh_input(tmp_path, native_runtime_package):
    async with native_backend_fixture(
        tmp_path, package=native_runtime_package, provider_type=PausedChunksProvider,
    ) as native:
        release, finish_goal = asyncio.Event(), asyncio.Event()
        first_chunk, continued = asyncio.Event(), asyncio.Event()
        updates = []

        class Client:
            async def session_update(self, session_id, update):
                value = SessionNotification(session_id=session_id, update=update).update.model_dump(
                    by_alias=True, exclude_none=True,
                )
                updates.append(value)
                text = value.get("content", {}).get("text", "")
                if "CANCELLED_RESPONSE" in text:
                    first_chunk.set()
                if "FRESH_RESPONSE" in text:
                    continued.set()

        provider = native.provider
        provider.text = "CANCELLED_RESPONSE"
        provider.completion_gate = release
        requests = provider.requests
        async with native.open_owner(runtime_enabled=True, client=Client()) as (owner, session):
            comms = owner._comms
            proxy = RuntimeProxy(owner, session, socket_path(comms.root, os.getpid()))
            await proxy.subscribe()
            created = await proxy.request("set_goal", text="Continue the owner objective")
            goal_id = created["goal"]["id"]
            store = comms.goals.open_goal_store()
            owner.inputs.dispositions.record(
                "acp:uncertain-sentinel", seq=None, owner=session, claim_scope=1,
                target=session, text="UNCERTAIN_INPUT_MUST_NOT_REPLAY",
            )
            sentinel_before = stored_inputs(owner.inputs.dispositions.root).rows.get("acp:uncertain-sentinel")
            comms.threads.claim_thread("sender", tags=frozenset(), worktree=str(native.project))
            comms.messaging.send("sender", session, "ORIGINAL_CANCELLED_INPUT")
            await owner.inputs.drain_inbox(session)
            turn = owner.inputs.state(session).wake_task
            assert turn is not None
            fresh_turn = None
            try:
                await asyncio.wait_for(first_chunk.wait(), 15)
                claimed = store.snapshot(goal_id)
                assert claimed.lifecycle == ReservedGeneration()
                assert len(requests) == 1
                await asyncio.wait_for(proxy.request("cancel"), 10)
                await asyncio.wait_for(asyncio.gather(turn, return_exceptions=True), 10)
                assert turn.done() and not owner.turns.session_busy(session)
                release.set()
                paused = comms.registry.require(session).goal
                failed = GoalAttemptStore(store.root).snapshot(goal_id)
                assert paused.state.declared_name == "paused"
                assert failed.lifecycle == BlockedGeneration() and failed.attempt_id == claimed.attempt_id
                # Resume cannot authorize replay of the cancelled attempt.
                with pytest.raises(RuntimeError):
                    await proxy.request(
                        "update_goal", status="active", goal_id=goal_id,
                        expected_revision=paused.revision,
                    )
                blocked = comms.registry.require(session).goal
                assert blocked.state.declared_name == "blocked"
                assert store.snapshot(goal_id) == failed
                assert len(requests) == 1
                retried = await proxy.request(
                    "retry_goal", goal_id=goal_id, expected_revision=blocked.revision,
                )
                assert FieldCodec.decode(Goal, retried["goal"]).state.declared_name == "active"
                ready = store.snapshot(goal_id)
                assert ready.number == failed.number + 1 and ready.lifecycle == ReadyGeneration()
                assert ready.attempt_id is None
                provider.text = "FRESH_RESPONSE"
                provider.completion_gate = finish_goal
                comms.messaging.send("sender", session, "FRESH_OWNER_INPUT")
                await owner.inputs.drain_inbox(session)
                fresh_turn = owner.inputs.state(session).wake_task
                assert fresh_turn is not None
                await asyncio.wait_for(continued.wait(), 15)
                fresh_claim = store.snapshot(goal_id)
                assert fresh_claim.lifecycle == ReservedGeneration()
                assert fresh_claim.attempt_id != failed.attempt_id
                current = comms.registry.require(session).goal
                await proxy.request(
                    "update_goal", status="paused", goal_id=goal_id,
                    expected_revision=current.revision,
                )
                finish_goal.set()
                await asyncio.wait_for(fresh_turn, 10)
                assert not owner.turns.session_busy(session)
                final = GoalAttemptStore(store.root).snapshot(goal_id)
                assert final.number == ready.number + 1 and final.lifecycle == ReadyGeneration()
                assert len(requests) == 2
                assert stored_inputs(owner.inputs.dispositions.root).rows.get("acp:uncertain-sentinel") == sentinel_before
                assert "UNCERTAIN_INPUT_MUST_NOT_REPLAY" not in json.dumps(requests)
                accepted = [row for row in stored_inputs(owner.inputs.dispositions.root).rows.values()
                            if isinstance(row, StartedInput)]
                assert len(accepted) == 2
                assert len({row.native_id for row in accepted}) == 2
                users = native.saved_inputs()
                assert sum("ORIGINAL_CANCELLED_INPUT" in json.dumps(row) for row in users) == 1
                assert sum("FRESH_OWNER_INPUT" in json.dumps(row) for row in users) == 1
                assert not any("[agent error]" in row.get("content", {}).get("text", "") for row in updates)
            finally:
                release.set()
                finish_goal.set()
                await owner.shutdown()
                await asyncio.gather(*(task for task in (turn, fresh_turn) if task), return_exceptions=True)
                await proxy.close()
