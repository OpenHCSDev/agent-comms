"""A project change during a turn is recorded, and the next turn runs in the new project.

Real registry and coordination stores in temporary directories. The native
test runs the real Pi child against a local provider.
"""
import asyncio
import json
import os
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import wire
from agent_comms.coordination_errors import StaleFence
from agent_comms.coordinator import Coordination
from agent_comms.field_codec import FieldCodec
from agent_comms.messages import Message, InfoKind
from agent_comms.threads import Thread
from agent_comms.turn_phase import CancellingPhase
from agent_comms.schedule_rules import ProjectResumeCheck, WakeScheduleCheck
from native_backend_fixture import native_backend_fixture


def declared_owner(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    comms = wire(tmp_path / "wire")
    comms.registry.declare(Thread(
        "owner", frozenset(), str(old), process_identity=ProcessIdentity.capture(os.getpid()),
    ))
    return comms, old, new


def test_project_change_while_idle_owes_no_turn(tmp_path):
    comms, _old, new = declared_owner(tmp_path)
    assert comms.threads.set_project("owner", str(new)).changed
    assert not comms.registry.require("owner").project_resume_due


def test_project_change_during_turn_is_recorded_until_the_next_turn_starts(tmp_path):
    comms, old, new = declared_owner(tmp_path)
    turn = comms.agents.begin_turn("owner", "changing-turn")
    comms.threads.set_project("owner", str(new))
    assert comms.registry.require("owner").project_resume_due
    assert comms.agents.finish_turn(turn.turn_lease)
    # The turn ended and nothing ran afterwards: the registry still says so,
    # also for an owner that registers this thread again later.
    comms.registry.declare(Thread(
        "owner", frozenset(), str(new), process_identity=ProcessIdentity.capture(os.getpid()),
    ))
    current = comms.registry.require("owner")
    assert current.project_resume_due and current.worktree == str(new)
    assert json.loads(comms.registry.store.path.read_text())["threads"]["owner"]["project_resume_due"]
    following = comms.agents.begin_turn("owner", "next-turn")
    assert following.thread.worktree == str(new)
    assert not comms.registry.require("owner").project_resume_due
    assert comms.agents.finish_turn(following.turn_lease)
    assert "project_resume_due" not in json.loads(
        comms.registry.store.path.read_text())["threads"]["owner"]


async def test_selected_wake_queues_resume_before_retiring_its_task(tmp_path):
    comms, _old, new = declared_owner(tmp_path)
    turn = comms.agents.begin_turn("owner", "changing-turn")
    comms.threads.set_project("owner", str(new))
    assert comms.agents.finish_turn(turn.turn_lease)

    current = asyncio.current_task()
    assert current is not None
    launched = []

    class Runtime:
        def background(self, work):
            launched.append(work)
            work.close()
            return current

    class Inputs:
        closing = False
        background_wakes_disabled = False
        backend_inboxes = {}
        pending_turns = {}
        wake_tasks = {"owner": current}
        sessions = SimpleNamespace(require=lambda _session: "owner")
        runtime = Runtime()

        async def _own_entry(self, _session):
            return comms.registry.entry("owner")

    inputs = Inputs()
    turns = SimpleNamespace(turn_tasks={})
    await ProjectResumeCheck(
        session_id="owner", inputs=inputs, turns=turns,
    ).schedule()
    assert len(inputs.pending_turns["owner"]) == 1
    assert str(new) in inputs.pending_turns["owner"][0].prompt
    assert not launched  # The selected wake still owns this session.

    inputs.wake_tasks.pop("owner")
    WakeScheduleCheck(session_id="owner", inputs=inputs).schedule()
    assert len(launched) == 1


def test_cancelled_turn_withdraws_the_recorded_resume(tmp_path):
    comms, _old, new = declared_owner(tmp_path)
    turn = comms.agents.begin_turn("owner", "changing-turn")
    comms.threads.set_project("owner", str(new))
    comms.agents.transition_turn(turn.turn_lease, CancellingPhase())
    assert comms.agents.finish_turn(turn.turn_lease)
    current = comms.registry.require("owner")
    assert not current.project_resume_due and current.worktree == str(new)


def test_started_turn_still_finishes_after_its_project_changes(tmp_path):
    comms, old, new = declared_owner(tmp_path)
    turn = comms.agents.begin_turn("owner", "changing-turn")
    try:
        comms.threads.set_project("owner", str(new))
        # Recording this turn's result and replies is allowed; new input is not.
        turn.require_running_turn(comms.registry)
        with pytest.raises(StaleFence, match="registry_worktree"):
            turn.require_registry(comms.registry)
        # A directory this conversation never used is still refused.
        elsewhere = replace(turn, thread=replace(turn.thread, worktree=str(tmp_path)))
        with pytest.raises(StaleFence, match="registry_worktree"):
            elsewhere.require_running_turn(comms.registry)
    finally:
        assert comms.agents.finish_turn(turn.turn_lease)
    with pytest.raises(StaleFence, match="no active turn"):
        replace(turn, thread=comms.registry.require("owner")).require_running_turn(comms.registry)


@pytest.mark.skipif(
    not os.environ.get("PI_COMPACTION_TEST_PACKAGE"),
    reason="Set PI_COMPACTION_TEST_PACKAGE to the immutable native bundle",
)
async def test_message_after_project_change_starts_a_turn_in_the_new_project(tmp_path):
    """The live failure: a direct message's turn changed project, then no turn ever started."""
    async with native_backend_fixture(tmp_path) as native:
        await native.author_history()
        second = tmp_path / "second"
        second.mkdir()
        async with native.open_owner(runtime_enabled=True, auto_wake=True) as (agent, session):
            comms = agent._comms
            comms.threads.claim_thread("sender", tags=frozenset(), worktree=str(native.project))
            sent = []

            def reply(message):
                return json.dumps(FieldCodec.encode((Message(
                    session, message.exact_reply_target, f"Reply {len(sent)}",
                    InfoKind, timestamp=0,
                ),)))

            def change_project_and_send_again():
                changed = comms.threads.set_project(session, str(second))
                sent.append(comms.messaging.send_message("sender", session, "Second message"))
                return changed

            class ChangeProjectDuringFirstTurn:
                changed = None

                async def wait(self):
                    if self.changed is None:
                        assert comms.registry.require(session).executing
                        self.changed = await Coordination.run_worker(change_project_and_send_again)
                    else:
                        native.provider.text = reply(sent[1])
                    return True

            sent.append(comms.messaging.send_message("sender", session, "First message"))
            native.provider.text = reply(sent[0])
            gate = native.provider.response_gate = ChangeProjectDuringFirstTurn()
            try:
                await agent.inputs.drain_inbox(session)
                async with asyncio.timeout(60):
                    while native.provider.posts < 2 or agent.turns.turn_state(session).busy:
                        await asyncio.sleep(0.01)
            finally:
                native.provider.response_gate = None
            assert gate.changed.changed
            thread = comms.registry.require(session)
            assert thread.worktree == str(second) and not thread.executing
            # The turn that changed project recorded its result: this owner's
            # coordination pointer is free, so later messages can start turns.
            with Coordination(str(comms.root / "coordination.sqlite3")) as store:
                participant = store.participants.get(RecordedThreadIncarnation("fixture", thread.created_at).lookup)
                assert participant.pointer.execution_id is None
            # The second message's turn started in the new project.
            assert str(second) in json.dumps(native.provider.requests[-1])
            assert not thread.project_resume_due
