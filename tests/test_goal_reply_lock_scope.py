"""Reply reads leave the registry free, and publication keeps its observed state."""

import os
import subprocess
import sys
from contextlib import contextmanager

import pytest

from agent_comms.bus import Bus
from agent_comms.bus_publication import Delivered
from agent_comms.bus_tables import ReceivedSource
from agent_comms.child_process import ProcessIdentity
from agent_comms.goal_actions import (
    EditGoalAction,
    GoalActionContext,
    GoalPrecondition,
    RuntimeInvocable,
    SetGoalAction,
    StandbyGoalAction,
)
from agent_comms.goal_waits import GoalWaitReview, RecordedGoalWait
from agent_comms.input_disposition import InputDispositions
from agent_comms.native_input_owner import RegistryOwner
from agent_comms.store_files import store_lock_path
from goal_owner_fixture import canonical_goal_wire


class RegistryProbeBus(Bus):
    """A real SQLite read asks another process to take the real registry lock."""

    def __init__(self, path, registry_path):
        super().__init__(path)
        self.registry_lock = store_lock_path(registry_path)
        self.checks = []

    def probe(self, statement):
        if self.checks or not any(table in statement for table in (
            ReceivedSource.declared_name, Delivered.table.declared_name,
        )):
            return
        checked = subprocess.run(
            [sys.executable, "-c", (
                "import fcntl, sys\n"
                "with open(sys.argv[1], 'rb') as lock:\n"
                " try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
                " except BlockingIOError: sys.exit(11)\n"
            ), str(self.registry_lock)],
            capture_output=True, text=True, timeout=5,
        )
        self.checks.append(checked.returncode)

    @contextmanager
    def observing(self):
        with super().observing() as db:
            db.set_trace_callback(self.probe)
            try:
                yield db
            finally:
                db.set_trace_callback(None)


@pytest.fixture
def reviewing(tmp_path):
    comms = canonical_goal_wire(tmp_path / "wire")
    for name in ("owner", "child"):
        comms.registry.declare(comms.registry.new_thread(
            name, frozenset(), str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid()),
        ))
    comms.agents.begin_turn("child", "child-work")
    comms.goals.update_goal("owner", SetGoalAction(text="Review the child's work"))
    return comms


@pytest.fixture
def probed(reviewing):
    reviewing.bus.bus = RegistryProbeBus(reviewing.bus.bus.path, reviewing.registry.store.path)
    return reviewing


def standby(comms, reviewed_inputs=()):
    return StandbyGoalAction(
        expect=GoalPrecondition(goal_id=comms.registry.require("owner").goal.require().id),
        wait_for=("child",), reviewed_inputs=reviewed_inputs,
    )


def test_standby_reads_replies_without_holding_registry(probed):
    probed.goals.update_goal("owner", standby(probed))
    assert probed.bus.bus.checks == [0]


def test_terminal_release_reads_replies_without_holding_registry(probed):
    probed.goals.update_goal("owner", standby(probed))
    probed.bus.bus.checks.clear()
    fence = probed.agents.finish_turn(probed.registry.require("child").require_turn_lease())
    assert probed.goals.release_waits_after_terminal_turn(fence) == ("owner",)
    assert probed.bus.bus.checks == [0]


def test_closed_wait_recovery_reads_replies_without_holding_registry(probed):
    probed.goals.update_goal("owner", standby(probed))
    probed.agents.finish_turn(probed.registry.require("child").require_turn_lease())
    probed.registry.unregister("child")
    probed.bus.bus.checks.clear()
    assert probed.goals.recover_closed_goal_wait("owner") == ("owner",)
    assert probed.bus.bus.checks == [0]


def test_completed_reply_is_decoded_without_holding_registry(probed):
    probed.goals.update_goal("owner", standby(probed))
    reply = probed.messaging.send("child", "owner", "Finished").message
    probed.bus.bus.checks.clear()
    owner = probed.registry.require("owner")
    witness = RegistryOwner(
        thread=owner, claimed_generation=probed.registry.snapshot().claim_generation(owner.name),
    )
    assert probed.goals.consume_reply_wait(witness, reply.reference)
    assert probed.bus.bus.checks == [0]


def test_reply_appended_between_review_and_publication_is_still_eligible(reviewing):
    cut = reviewing.bus.bus.revision()
    prepared = standby(reviewing).prepare(reviewing.goals, "owner", RuntimeInvocable)
    reply = reviewing.messaging.send("child", "owner", "Arrived during review").message
    with reviewing.registry.store.locked():
        prepared.apply(GoalActionContext(
            reviewing.goals, reviewing.registry.require("owner"), RuntimeInvocable,
        ))
    wait = reviewing.goals.goal_wait("owner")
    assert wait.after_seq == cut < reply.seq
    assert wait.has_reply(reviewing.bus.bus, reviewing.registry.require("owner").incarnation)
    review = reviewing.goals.goal_input_review("owner", wait.goal_id, ("child",))
    assert review.reviewed_inputs == (f"bus:{reply.seq}",)


@pytest.mark.parametrize("change", ("goal", "wait", "turn", "dependency"))
def test_stale_review_never_writes_inputs_or_replaces_newer_wait(reviewing, change):
    reply = reviewing.messaging.send("child", "owner", "Review this reply").message
    prepared = standby(reviewing, (f"bus:{reply.seq}",)).prepare(
        reviewing.goals, "owner", RuntimeInvocable,
    )
    owner = reviewing.registry.require("owner")
    goal = owner.goal.require()
    if change == "goal":
        reviewing.goals.update_goal("owner", EditGoalAction(text="A different objective"))
    elif change == "wait":
        target = reviewing.registry.require("child")
        reviewing.goals.waits.record(RecordedGoalWait(
            goal.id, "newer-wait", goal.revision, reply.seq, prepared.review.targets,
            owner=owner.incarnation.require_recorded(),
            target_turn_generations=(target.require_turn_lease().identity.generation,),
            report_turn_id=None, report_turn_generation=None,
        ))
    elif change == "turn":
        reviewing.agents.begin_turn("owner", "new-owner-turn")
    else:
        reviewing.registry.unregister("child")
        reviewing.registry.remove("child")
        reviewing.registry.declare(reviewing.registry.new_thread(
            "child", frozenset(), owner.worktree,
            process_identity=ProcessIdentity.capture(os.getpid()),
        ))
    inputs = InputDispositions(reviewing.root)
    before = inputs.path.read_bytes() if inputs.path.exists() else None
    wait_before = reviewing.goals.goal_wait("owner")
    with reviewing.registry.store.locked(), pytest.raises(ValueError):
        prepared.apply(GoalActionContext(
            reviewing.goals, reviewing.registry.require("owner"), RuntimeInvocable,
        ))
    assert (inputs.path.read_bytes() if inputs.path.exists() else None) == before
    assert reviewing.goals.goal_wait("owner") == wait_before


def test_late_reply_invalidates_captured_silence(reviewing):
    reviewing.goals.update_goal("owner", standby(reviewing))
    reviewing.agents.finish_turn(reviewing.registry.require("child").require_turn_lease())
    captured = GoalWaitReview.capture(reviewing.goals, "owner")
    cut, = captured.unanswered(reviewing.bus.bus)
    reviewing.messaging.send("child", "owner", "A reply after the silence check")
    with reviewing.registry.store.locked():
        assert captured.current_for(reviewing.goals)
        assert not reviewing.goals._release_reviewed_wait(captured, cut, "stale silence")
    assert reviewing.goals.goal_wait("owner") == captured.wait
    assert reviewing.registry.require("owner").goal == captured.owner.goal
