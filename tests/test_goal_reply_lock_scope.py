"""Reply reads leave the registry free, and publication keeps its observed state."""

import os
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import replace

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
from agent_comms.coordination_errors import StaleFence
from agent_comms.messages import ConversationRole, NoticeRole
from agent_comms.store_files import store_lock_path
from agent_comms.thread_text import GivenThreadText
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tools import CommsGoalTool
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


class AfterReadBus(Bus):
    """Change real state once the real query snapshot closes, before publication."""

    def __init__(self, path, change):
        super().__init__(path)
        self.change = change
        self.changed = False

    @contextmanager
    def reading(self):
        with super().reading() as source:
            yield source
        if not self.changed:
            self.changed = True
            self.change()


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
    CommsGoalTool.invoke(probed, {
        "goal_id": probed.registry.require("owner").goal.require().id,
        "status": "standby", "progress": "Waiting for the child", "wait_for": ["child"],
    }, caller=ThreadCaller("owner"))
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
    witness = probed.agents.begin_turn("owner", "handling-reply")
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


@pytest.mark.parametrize("change", (
    "goal", "wait", "turn", "dependency_turn", "dependency", "owner", "input",
))
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
    elif change == "dependency_turn":
        reviewing.agents.finish_turn(reviewing.registry.require("child").require_turn_lease())
        reviewing.agents.begin_turn("child", "new-child-turn")
    elif change == "input":
        InputDispositions(reviewing.root).review_for_goal(
            prepared.reviewed_inputs, observed=prepared.review.unknown,
            owners=prepared.review.owners, goal_id=goal.id,
            goal_revision=goal.revision + 1, turn_id="",
        )
    else:
        name = "owner" if change == "owner" else "child"
        reviewing.registry.unregister(name)
        reviewing.registry.remove(name)
        reviewing.registry.declare(reviewing.registry.new_thread(
            name, frozenset(), owner.worktree,
            process_identity=ProcessIdentity.capture(os.getpid()),
        ))
    inputs = InputDispositions(reviewing.root)
    before = inputs.read(prepared.reviewed_inputs)
    wait_before = reviewing.goals.goal_wait("owner")
    goal_before = reviewing.registry.require("owner").goal
    with reviewing.registry.store.locked(), pytest.raises(ValueError):
        prepared.apply(GoalActionContext(
            reviewing.goals, reviewing.registry.require("owner"), RuntimeInvocable,
        ))
    assert inputs.read(prepared.reviewed_inputs) == before
    assert reviewing.goals.goal_wait("owner") == wait_before
    assert reviewing.registry.require("owner").goal == goal_before


@pytest.mark.parametrize("role,released", ((ConversationRole(), ()), (NoticeRole(), ("owner",))))
def test_new_received_candidate_is_read_before_releasing_silence(reviewing, role, released):
    reviewing.goals.update_goal("owner", standby(reviewing))
    fence = reviewing.agents.finish_turn(reviewing.registry.require("child").require_turn_lease())
    wait = reviewing.goals.goal_wait("owner")
    reviewing.bus.bus = AfterReadBus(reviewing.bus.bus.path, lambda: reviewing.messaging.send(
        "child", "owner", "A message after the silence check", role=role,
    ))
    assert reviewing.goals.release_waits_after_terminal_turn(fence) == released
    assert (reviewing.goals.goal_wait("owner") != wait) == bool(released)


def test_other_recipient_traffic_does_not_strand_terminal_wait(reviewing):
    reviewing.registry.declare(reviewing.registry.new_thread("other", frozenset(), str(reviewing.root)))
    reviewing.goals.update_goal("owner", standby(reviewing))
    fence = reviewing.agents.finish_turn(reviewing.registry.require("child").require_turn_lease())
    reviewing.bus.bus = AfterReadBus(reviewing.bus.bus.path, lambda: reviewing.messaging.send(
        "child", "other", "This has nothing to do with the waiting owner",
    ))
    assert reviewing.goals.release_waits_after_terminal_turn(fence) == ("owner",)


def test_terminal_reply_read_cannot_clear_a_newer_wait(reviewing):
    reviewing.goals.update_goal("owner", standby(reviewing))
    fence = reviewing.agents.finish_turn(reviewing.registry.require("child").require_turn_lease())
    goal = reviewing.registry.require("owner").goal
    newer = replace(reviewing.goals.goal_wait("owner"), wait_id="newer-wait")
    reviewing.bus.bus = AfterReadBus(
        reviewing.bus.bus.path, lambda: reviewing.goals.waits.record(newer),
    )
    assert reviewing.goals.release_waits_after_terminal_turn(fence) == ()
    assert reviewing.goals.goal_wait("owner") == newer
    assert reviewing.registry.require("owner").goal == goal


@pytest.mark.parametrize("change", ("wait", "turn"))
def test_completed_reply_rechecks_its_wait_and_original_turn(reviewing, change):
    reviewing.goals.update_goal("owner", standby(reviewing))
    reply = reviewing.messaging.send("child", "owner", "The actual completed reply").message
    witness = reviewing.agents.begin_turn("owner", "original-handling-turn")
    wait = reviewing.goals.goal_wait("owner")
    newer = replace(wait, wait_id="newer-wait")

    def replace_state():
        if change == "wait":
            reviewing.goals.waits.record(newer)
        else:
            reviewing.agents.finish_turn(witness.thread.require_turn_lease())
            reviewing.agents.begin_turn("owner", "a-later-turn")

    reviewing.bus.bus = AfterReadBus(reviewing.bus.bus.path, replace_state)
    if change == "wait":
        assert not reviewing.goals.consume_reply_wait(witness, reply.reference)
        assert reviewing.goals.goal_wait("owner") == newer
    else:
        with pytest.raises(StaleFence):
            reviewing.goals.consume_reply_wait(witness, reply.reference)
        assert reviewing.goals.goal_wait("owner") == wait


@pytest.mark.parametrize("rename", (False, True))
def test_display_edit_during_standby_review_is_preserved(reviewing, rename):
    prepared = standby(reviewing).prepare(reviewing.goals, "owner", RuntimeInvocable)
    title = GivenThreadText("A title changed while replies were read")
    reviewing.registry.register(replace(reviewing.registry.require("owner"), title=title))
    if rename:
        reviewing.registry.rename("owner", "renamed-owner")
    with reviewing.registry.store.locked():
        prepared.apply(GoalActionContext(
            reviewing.goals, reviewing.registry.require("owner"), RuntimeInvocable,
        ))
    assert reviewing.registry.require("owner").title == title
    assert reviewing.goals.goal_wait("owner").goal_id == prepared.review.goal_id


@pytest.mark.parametrize("rename", (False, True))
def test_display_edit_during_terminal_review_does_not_strand_wait(reviewing, rename):
    reviewing.goals.update_goal("owner", standby(reviewing))
    fence = reviewing.agents.finish_turn(reviewing.registry.require("child").require_turn_lease())
    captured = GoalWaitReview.capture(reviewing.goals, "owner")
    title = GivenThreadText("Keep this concurrent title")
    reviewing.registry.register(replace(reviewing.registry.require("owner"), title=title))
    if rename:
        reviewing.registry.rename("owner", "renamed-owner")
    assert reviewing.goals._release_finished_wait(captured, fence) == (
        "renamed-owner" if rename else "owner",
    )
    assert reviewing.registry.require("owner").title == title
    assert reviewing.goals.goal_wait("owner") != captured.wait
