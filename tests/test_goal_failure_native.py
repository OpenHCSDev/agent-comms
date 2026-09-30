"""Saved native history -> ACP goal failure -> passive read, without replay."""

import asyncio
import json
import os
from dataclasses import replace

import pytest
from acp import RequestError
from acp.agent.router import build_agent_router

from agent_comms.comms import Comms
from agent_comms.acp_extension import (
    CompactionChangedUpdate,
    CompactionPublishedUpdate,
    decode_updates,
)
from agent_comms.agent_events import CompactionStart, CompactionEnd
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.compaction_records import SelectedSummaryAttempt
from agent_comms.compaction_states import LinkedSummary, CommittedOperation
from agent_comms.acp_failure import PromptFailureReceipt
from agent_comms.goal_actions import GoalPrecondition, OwnerInvocable, PausedGoalAction, SetGoalAction
from agent_comms.goal_attempts import GoalAttemptStore, UnresolvedAttemptError
from agent_comms.goal_failure_observation import read_failed_turn_projection
from agent_comms.goal_generation import BlockedGeneration, ReadyGeneration
from agent_comms.runtime import RuntimeProxy, socket_path
from delivery_owner_fixture import canonical_agent
from test_backend_native_lifecycle import native_backend as native_backend


async def test_saved_native_autonomous_goal_compacts_before_original_input(
    native_backend, monkeypatch
):
    """Cold saved history -> ordinary scheduler -> selected journal -> one real input.

    Only the localhost provider response is controlled. The goal grant, wake
    runner, ACP updates, input reservation, Pi context decision and native writer
    are production owners, including the hard stored-context admission backstop.
    """
    native = native_backend
    native.provider.text = "Retained history for the compaction boundary.\n" * 1000
    assert (await native.run("Older retained history"))[-1].ok
    native.provider.text = "Recent retained answer."
    assert (await native.run("Recent retained question"))[-1].ok
    await native.persistent.close()
    history = native.session.read_bytes()
    models = json.loads((native.config / "models.json").read_text())
    models["providers"]["response-local"]["models"][0]["contextWindow"] = 10000
    models["providers"]["response-local"]["models"][0]["maxTokens"] = 1000
    (native.config / "models.json").write_text(json.dumps(models))
    (native.config / "settings.json").write_text(
        json.dumps(
            {
                "compaction": {"enabled": True, "reserveTokens": 1000, "keepRecentTokens": 100},
                "retry": {"enabled": False},
            }
        )
    )
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "response-local/fixture")
    comms = Comms(native.root)
    agent = canonical_agent(
        comms,
        auto_wake=False,
        runtime_enabled=True,
        agent_args=[
            "--provider=response-local",
            "--model=fixture",
            "--thinking=off",
            "--offline",
            "--no-extensions",
            "--no-skills",
            "--no-context-files",
            "--no-prompt-templates",
            "--no-tools",
        ],
    )
    updates = []

    class Client:
        async def session_update(self, **kwargs):
            updates.append(kwargs["update"])

    agent.on_connect(Client())
    route = build_agent_router(agent)
    try:
        async with asyncio.timeout(60):
            created = await route(
                "session/new", {"cwd": str(native.project), "mcpServers": []}, False
            )
            sid = created.session_id
            # Bound the journey to exactly one scheduler launch; observers are
            # independent of the explicit wake exercised below.
            drain = agent.inputs.drain_tasks.pop(sid)
            drain.cancel()
            await asyncio.gather(drain, return_exceptions=True)
            comms.registry.register(replace(comms.registry.require(sid), auto_title_pending=False))
            comms.threads.attach_session(sid, str(native.session))
            await route(
                "session/load",
                {"cwd": str(native.project), "sessionId": sid, "mcpServers": []},
                False,
            )
            drain = agent.inputs.drain_tasks.pop(sid)
            drain.cancel()
            await asyncio.gather(drain, return_exceptions=True)
            goal = comms.goals.update_goal(
                sid, SetGoalAction(text="One journaled autonomous continuation")
            )
            store = agent.turns.goals.open_goal_store()
            store.create_goal(goal.id)
            admission = comms.registry.snapshot().admission_generations[sid]
            preserved = "acp:unrelated-unattempted"
            agent.inputs.dispositions.record(
                preserved,
                seq=None,
                owner="other-owner",
                admission=admission,
                target="other-owner",
                text="Never replay this unrelated reserved input",
            )
            unknown = agent.inputs.dispositions.read().lookup(preserved)
            before_posts = native.provider.posts
            native.provider.text = "Journaled local summary and successful goal continuation."
            agent.inputs.auto_wake = True
            agent.turns.goals.schedule_goal(sid)
            await agent.inputs.wake_tasks[sid]
            agent.inputs.auto_wake = False
            assert store.snapshot(goal.id).lifecycle == ReadyGeneration()
            assert len(native.saved_inputs()) == 3
            assert native.saved_inputs()[-1]["content"][0]["text"].endswith(
                "Continue working toward the active goal."
            )
            # Summary generation may use several budgeted chunks. Only the
            # original native user input is required to start exactly once.
            assert native.provider.posts >= before_posts + 2
            assert native.session.read_bytes().startswith(history)
            rows = agent.inputs.dispositions.read()
            originals = [row for row in rows.rows.values() if row.key.startswith("turn:")]
            assert len(originals) == 1 and originals[0].has_started
            assert originals[0].source_text == originals[0].sent_text
            assert rows.lookup(preserved) == unknown
            entries = [json.loads(line) for line in native.session.read_text().splitlines()]
            compact = [index for index, row in enumerate(entries) if row["type"] == "compaction"]
            assert len(compact) == 1
            started = next(
                index
                for index, row in enumerate(entries)
                if row.get("message", {}).get("inputId") == originals[0].native_id
            )
            assert compact[0] < started
            journal = CompactionJournal(comms.root / "compaction-commits.sqlite3")
            assert not journal.summaries.blocking(str(native.session))
            with journal.transaction() as db:
                summaries = SelectedSummaryAttempt.select(
                    db, where="session_file=?", parameters=(str(native.session),)
                )
            assert len(summaries) == 1 and isinstance(summaries[0].state, LinkedSummary)
            assert isinstance(
                journal.operations.get(summaries[0].state.commit_id).state, CommittedOperation
            )
            decoded = [item for update in updates for item in decode_updates(update.field_meta)]
            compact_events = [
                update.event for update in decoded if isinstance(update, CompactionChangedUpdate)
            ]
            assert any(isinstance(event, CompactionStart) for event in compact_events)
            assert any(
                isinstance(event, CompactionEnd) and not event.aborted for event in compact_events
            )
            assert any(isinstance(update, CompactionPublishedUpdate) for update in decoded)
            print(
                f"autonomous_goal_originals=1 journaled_compactions=1 provider_posts={native.provider.posts-before_posts} original_after_commit=yes"
            )
    finally:
        await agent.shutdown()


@pytest.mark.parametrize("owner_pauses", [False, True])
async def test_saved_native_acp_failed_goal_remains_passive(native_backend, monkeypatch, owner_pauses):
    native = native_backend
    assert (await native.run("Retained history before goal failure"))[-1].ok
    await native.persistent.close()
    history = native.session.read_bytes()
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "response-local/fixture")
    comms = Comms(native.root)
    agent = canonical_agent(
        comms, auto_wake=False, runtime_enabled=True,
        agent_args=[
            "--provider=response-local", "--model=fixture", "--thinking=off", "--offline",
            "--no-extensions", "--no-skills", "--no-context-files",
            "--no-prompt-templates", "--no-tools",
        ],
    )
    route = build_agent_router(agent)
    try:
        async with asyncio.timeout(30):
            created = await route("session/new", {"cwd": str(native.project), "mcpServers": []}, False)
            sid = created.session_id
            comms.registry.register(replace(comms.registry.require(sid), auto_title_pending=False))
            comms.threads.attach_session(sid, str(native.session))
            await route("session/load", {"cwd": str(native.project), "sessionId": sid, "mcpServers": []}, False)
            response = await route("session/prompt", {
                "sessionId": sid,
                "prompt": [{"type": "text", "text": "One explicit ACP input before the goal"}],
            }, False)
            assert response.stop_reason == "end_turn"
            assert len(native.saved_inputs()) == 2
            goal = comms.goals.update_goal(sid, SetGoalAction(text="Exercise one explicit failed attempt"))
            store = agent.turns.goals.open_goal_store()
            store.create_goal(goal.id)
            admission = comms.registry.snapshot().admission_generations[sid]
            key = "acp:retained-unknown"
            agent.inputs.dispositions.record(
                key, seq=None, owner=sid, admission=admission,
                target=sid, text="Never replay this earlier uncertain input",
            )
            unknown = agent.inputs.dispositions.read().rows[key]
            native.provider.status = 503
            if owner_pauses:
                class PauseAtResponse:
                    async def wait(self):
                        # Only the localhost response is controlled. Pause at
                        # the actual provider request of the tracked native turn.
                        current = comms.registry.require(sid)
                        assert current.executing
                        paused = comms.goals.update_goal(
                            sid, PausedGoalAction(expect=GoalPrecondition(goal_id=goal.id)),
                            actor=OwnerInvocable,
                        )
                        assert paused.state.protected
                        return True

                native.provider.response_gate = PauseAtResponse()
            # Direct user inputs intentionally do not spend a goal grant.
            # Run the normal autonomous owner entrypoint against the same loaded
            # session; the real backend, input journal and settlement remain live.
            with pytest.raises(RequestError) as error:
                await agent.turns.run_agent_turn(
                    sid, sid, "One new failed goal input", autonomous_goal=True
                )
            receipt = PromptFailureReceipt.from_error(
                error.value.code, str(error.value), error.value.data
            )
            assert receipt.notification_published
            assert store.snapshot(goal.id).lifecycle == BlockedGeneration()
            assert len(native.saved_inputs()) == 3
            assert native.saved_inputs()[-1]["content"][0]["text"].endswith("One new failed goal input")
            assert native.session.read_bytes().startswith(history)
            assert agent.inputs.dispositions.read().rows[key] == unknown
            registry = comms.registry.snapshot()
            owner = registry.threads[sid]
            before = store.path.read_bytes()
            projected = read_failed_turn_projection(
                store.path, owner=owner, owner_status=registry.statuses[sid],
                admission=registry.admission_generations[sid],
            )
            assert projected.state == ("owner_paused" if owner_pauses else "backend_suspended"), projected
            assert projected.reason != "missing_binding"
            assert store.path.read_bytes() == before
            with pytest.raises(UnresolvedAttemptError):
                GoalAttemptStore(store.root).resume(goal.id, 1)
            agent.turns.goals.schedule_goal(sid)
            assert not agent.inputs.pending_turns.get(sid)
            assert len(native.saved_inputs()) == 3
            if owner_pauses:
                reopened = Comms(comms.root)
                paused = reopened.registry.require(sid).goal
                assert paused.state.pause_source.instruction()
                assert reopened.goals.goal_history(sid)[-1].after == paused
                assert not (comms.root / "goal_pause_events.json").exists()
                assert agent.inputs.dispositions.read().rows[key] == unknown
                return
            # Explicit owner Retry traverses the socket control, new scheduler,
            # existing wake runner and native journal. The failed/UNKNOWN input
            # above is retained; only one new goal continuation is dispatched.
            native.provider.status = 200
            agent.inputs.auto_wake = True
            blocked = comms.registry.require(sid).goal
            proxy = RuntimeProxy(agent, sid, socket_path(comms.root, os.getpid()))
            try:
                await proxy.request("retry_goal", goal_id=goal.id, expected_revision=blocked.revision)
                await agent.inputs.wake_tasks[sid]
                agent.inputs.auto_wake = False  # bound this isolated journey to one retry
            finally:
                await proxy.close()
            assert store.snapshot(goal.id).number == 3
            assert store.snapshot(goal.id).lifecycle == ReadyGeneration()  # verified native progress
            assert len(native.saved_inputs()) == 4
            assert native.saved_inputs()[-1]["content"][0]["text"].endswith(
                "Continue working toward the active goal."
            )
            assert native.session.read_bytes().startswith(history)
            assert agent.inputs.dispositions.read().rows[key] == unknown
            assert not agent.inputs.pending_turns.get(sid)
            print(f"native_failed_goal_projection={projected.to_primitive()} provider_posts={native.provider.posts} retry_generation=2 next_ready_generation=3 saved_inputs=4")
    finally:
        await agent.shutdown()
