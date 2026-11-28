"""A project change during a turn is recorded, and the next turn runs in the new project.

Real registry and coordination stores in temporary directories. The native
test runs the real Pi child against a local provider.
"""
import asyncio
import json
import os

from agent_comms.comms import wire
from agent_comms.coordination.coordinator import Coordination
from agent_comms.coordination.tables.executions import NoPointedExecution
from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.execution.turn_phase import CancellingPhase, ShutdownPhase
from agent_comms.execution.turn_terminal import InstallStopped, OperatorStopped, OwnerStop
from agent_comms.messaging.messages import InfoKind, Message
from agent_comms.messaging.routing import ScheduledTurn
from agent_comms.nominal.field_codec import FieldCodec, GoalId, TurnId
from agent_comms.planning.goals import Goal
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.threads import NativeThread
from agent_comms.storage.maintenance_barrier import MaintenanceBarrier, MaintenanceOperator
from native_backend_fixture import native_backend_fixture


def declared_owner(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    comms = wire(tmp_path / "wire")
    comms.registry.declare(NativeThread('owner', frozenset(), Checkout(old), process_identity=ProcessIdentity.capture(os.getpid())))
    return comms, old, new


def test_project_change_while_idle_owes_no_turn(tmp_path):
    comms, _old, new = declared_owner(tmp_path)
    assert comms.threads.set_project(comms.registry.require("owner").incarnation, str(new)).changed
    assert not comms.registry.require("owner").project_resume_due


def test_project_change_during_turn_is_recorded_until_the_next_turn_starts(tmp_path):
    comms, old, new = declared_owner(tmp_path)
    turn = comms.agents.begin_turn("owner", TurnId('1056fa0e005902e019b8d1d4fd75d3cb'))
    comms.threads.set_project(comms.registry.require("owner").incarnation, str(new))
    assert comms.registry.require("owner").project_resume_due
    assert comms.agents.finish_turn(turn.turn_lease)
    # The turn ended and nothing ran afterwards: the registry still says so,
    # also for an owner that registers this thread again later.
    comms.registry.declare(NativeThread('owner', frozenset(), Checkout(new), process_identity=ProcessIdentity.capture(os.getpid())))
    current = comms.registry.require("owner")
    assert current.project_resume_due and current.worktree == Checkout(new)
    following = comms.agents.begin_turn("owner", TurnId('bf5eb504b7221133c4f3cf2aa87008a3'))
    assert following.thread.worktree == Checkout(new)
    assert not comms.registry.require("owner").project_resume_due
    assert comms.agents.finish_turn(following.turn_lease)


def test_cancelled_turn_withdraws_the_recorded_resume(tmp_path):
    comms, _old, new = declared_owner(tmp_path)
    turn = comms.agents.begin_turn("owner", TurnId('1056fa0e005902e019b8d1d4fd75d3cb'))
    comms.threads.set_project(comms.registry.require("owner").incarnation, str(new))
    comms.agents.transition_turn(turn.turn_lease, CancellingPhase())
    assert comms.agents.finish_turn(turn.turn_lease)
    current = comms.registry.require("owner")
    assert not current.project_resume_due and not current.install_resume_due and current.worktree == Checkout(new)


def test_install_shutdown_records_resume_until_next_turn_starts(tmp_path):
    comms, old, _new = declared_owner(tmp_path)
    turn = comms.agents.begin_turn("owner", TurnId('1056fa0e005902e019b8d1d4fd75d3cb'))
    comms.agents.transition_turn(turn.turn_lease, ShutdownPhase())
    assert comms.agents.finish_turn(turn.turn_lease)
    current = comms.registry.require("owner")
    assert current.install_resume_due and current.owes_install_resume

    # Re-registering the owner on restart inherits the pending install resume.
    comms.registry.declare(NativeThread('owner', frozenset(), Checkout(old), process_identity=ProcessIdentity.capture(os.getpid())))
    restarted = comms.registry.require("owner")
    assert restarted.install_resume_due and restarted.owes_install_resume

    # An active goal owns its own resume turn, so owes_install_resume stays False.
    with_goal = NativeThread(
        'owner', frozenset(), Checkout(old),
        goal=Goal("continue work", GoalId.mint(), 0),
        install_resume_due=True,
    )
    assert not with_goal.owes_install_resume

    following = comms.agents.begin_turn("owner", TurnId('bf5eb504b7221133c4f3cf2aa87008a3'))
    assert not comms.registry.require("owner").install_resume_due
    assert comms.agents.finish_turn(following.turn_lease)


async def test_owner_stop_distinguishes_install_maintenance_from_manual_stop(tmp_path):
    barrier = MaintenanceBarrier(tmp_path / "registry.json")
    manual = OwnerStop(barrier)
    manual.request()
    assert type(manual.terminal) is OperatorStopped
    assert manual.terminal.cancellation_phase() == CancellingPhase()

    held = barrier.begin(MaintenanceOperator.current())
    try:
        during_install = OwnerStop(barrier)
        during_install.request()
        assert type(during_install.terminal) is InstallStopped
        assert during_install.terminal.cancellation_phase() == ShutdownPhase()
        assert isinstance(ScheduledTurn.resuming_after_install(), ScheduledTurn)
    finally:
        barrier.clear(held)


async def test_message_after_project_change_starts_a_turn_in_the_new_project(tmp_path, native_runtime_package):
    """The live failure: a direct message's turn changed project, then no turn ever started."""
    async with native_backend_fixture(tmp_path, package=native_runtime_package) as native:
        await native.author_history()
        second = tmp_path / "second"
        second.mkdir()
        async with native.open_owner(auto_wake=True) as (agent, session):
            comms = agent._comms
            comms.threads.claim_thread("sender", tags=frozenset(), worktree=Checkout(native.project))
            sent = []

            def reply(message):
                return json.dumps(FieldCodec.encode((Message(
                    agent.sessions.require(session), message.exact_reply_target, f"Reply {len(sent)}",
                    InfoKind, timestamp=0,
                ),)))

            def change_project_and_send_again():
                changed = comms.threads.set_project(comms.registry.require(session).incarnation, str(second))
                sent.append(comms.messaging.send("sender", session, "Second message").message)
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

            sent.append(comms.messaging.send("sender", session, "First message").message)
            native.provider.text = reply(sent[0])
            gate = native.provider.response_gate = ChangeProjectDuringFirstTurn()
            try:
                await agent.inputs.drain_inbox(session)
                async with asyncio.timeout(60):
                    while native.provider.posts < 2 or agent.turns.turn_state(session).active.phase.busy:
                        await asyncio.sleep(0.01)
            finally:
                native.provider.response_gate = None
            assert gate.changed.changed
            thread = comms.registry.require(session)
            assert thread.worktree == Checkout(second) and not thread.executing
            # The turn that changed project recorded its result: this owner's
            # coordination pointer is free, so later messages can start turns.
            with Coordination(str(comms.root / "coordination.sqlite3")) as store:
                participant = store.participants.get(thread.incarnation.lookup)
                assert participant.pointer.execution_id == NoPointedExecution()
            # The second message's turn started in the new project.
            assert str(second) in json.dumps(native.provider.requests[-1])
            assert not thread.project_resume_due
