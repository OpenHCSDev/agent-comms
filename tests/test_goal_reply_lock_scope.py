"""Reply reads leave the registry free, and publication keeps its observed state."""
from agent_comms.field_codec import TurnId

import fcntl
import os
from contextlib import contextmanager
from dataclasses import replace
from functools import partial

import pytest

from agent_comms.bus import Bus
from agent_comms.bus_publication import Delivered
from agent_comms.bus_tables import ReceivedSource
from agent_comms.child_process import ProcessIdentity
from agent_comms.coordination_errors import StaleFence
from agent_comms.goal_actions import (
    EditGoalAction,
    GoalActionContext,
    GoalPrecondition,
    RuntimeInvocable,
    SetGoalAction,
    StandbyGoalAction,
)
from agent_comms.goal_waits import GoalWaitReview, NoGoalWait, RecordedGoalWait
from agent_comms.input_disposition import InputDispositions
from agent_comms.messages import ConversationRole, NoticeRole
from agent_comms.store_files import store_lock_path
from agent_comms.thread_text import GivenThreadText
from agent_comms.thread_status import StoppedThreadStatus
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tools import CommsGoalTool
from goal_owner_fixture import canonical_goal_wire
from agent_comms.threads import Thread


class RegistryProbeBus(Bus):
    """A second open file description tries to take the real registry lock."""

    def __init__(self, path, registry_path):
        super().__init__(path)
        self.registry_lock = store_lock_path(registry_path)
        self.checks = []

    def probe(self, statement):
        tables = (*ReceivedSource.members_with(ReceivedSource), Delivered.table)
        if self.checks or not any(table.declared_name in statement for table in tables):
            return
        with self.registry_lock.open("rb") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.checks.append(11)
            else:
                self.checks.append(0)
                fcntl.flock(lock, fcntl.LOCK_UN)

    @contextmanager
    def observing(self, *args, **kwargs):
        # Bus.observing owns its parameters; the probe only traces the snapshot.
        with super().observing(*args, **kwargs) as db:
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
        comms.registry.declare(Thread(
            name, frozenset(), str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid()),
        ))
    comms.agents.begin_turn("child", TurnId('83dcc89110e0b7e93d50dfc4d8f66d0f'))
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
    witness = probed.agents.begin_turn("owner", TurnId('8443600f04e8b6b0a977082a8a88321d'))
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


def change_goal(comms, prepared):
    comms.goals.update_goal("owner", EditGoalAction(text="A different objective"))


def replace_wait(comms, prepared):
    owner = comms.registry.require("owner")
    goal = owner.goal.require()
    target = comms.registry.require("child")
    comms.goals.waits.record(RecordedGoalWait(
        goal.id, "newer-wait", goal.revision, prepared.review.through_seq,
        prepared.review.targets, owner=owner.incarnation.require_recorded(),
        target_turn_generations=(target.require_turn_lease().identity.generation,),
        report_turn_id=None, report_turn_generation=None,
    ))


def begin_owner_turn(comms, prepared):
    comms.agents.begin_turn("owner", TurnId('85a98504553ba87ec3fe28eb889fab23'))


def begin_dependency_turn(comms, prepared):
    comms.agents.finish_turn(comms.registry.require("child").require_turn_lease())
    comms.agents.begin_turn("child", TurnId('25b63a30f24cf9d67463f931d800c73a'))


def review_inputs(comms, prepared):
    goal = comms.registry.require("owner").goal.require()
    InputDispositions(comms.root).review_for_goal(
        prepared.reviewed_inputs, observed=prepared.review.unknown,
        owners=prepared.review.owners, goal_id=goal.id,
        goal_revision=goal.revision + 1, turn_id="",
    )


def replace_registration(comms, prepared, *, name):
    original = comms.registry.require(name)
    comms.registry.unregister(name)
    comms.registry.remove(name)
    comms.registry.declare(Thread(
        name, frozenset(), original.worktree,
        process_identity=ProcessIdentity.capture(os.getpid()),
    ))


@pytest.mark.parametrize("change", (
    change_goal, replace_wait, begin_owner_turn, begin_dependency_turn, review_inputs,
    partial(replace_registration, name="child"),
    partial(replace_registration, name="owner"),
))
def test_stale_review_never_writes_inputs_or_replaces_newer_wait(reviewing, change):
    reply = reviewing.messaging.send("child", "owner", "Review this reply").message
    prepared = standby(reviewing, (f"bus:{reply.seq}",)).prepare(
        reviewing.goals, "owner", RuntimeInvocable,
    )
    change(reviewing, prepared)
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
    reviewing.registry.declare(Thread("other", frozenset(), str(reviewing.root)))
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


def completed_reply(comms):
    comms.goals.update_goal("owner", standby(comms))
    reply = comms.messaging.send("child", "owner", "The actual completed reply").message
    witness = comms.agents.begin_turn("owner", TurnId('14ef4c75692efd61f1b3f105c8af23d2'))
    return witness, reply, comms.goals.goal_wait("owner")


def test_completed_reply_rechecks_its_exact_wait(reviewing):
    witness, reply, wait = completed_reply(reviewing)
    newer = replace(wait, wait_id="newer-wait")
    reviewing.bus.bus = AfterReadBus(
        reviewing.bus.bus.path, lambda: reviewing.goals.waits.record(newer),
    )
    assert not reviewing.goals.consume_reply_wait(witness, reply.reference)
    assert reviewing.goals.goal_wait("owner") == newer


def test_completed_reply_rechecks_its_original_turn(reviewing):
    witness, reply, wait = completed_reply(reviewing)

    def advance_turn():
        reviewing.agents.finish_turn(witness.thread.require_turn_lease())
        reviewing.agents.begin_turn("owner", TurnId('13e021d6bd76f1eca99b0268508e0542'))

    reviewing.bus.bus = AfterReadBus(reviewing.bus.bus.path, advance_turn)
    with pytest.raises(StaleFence):
        reviewing.goals.consume_reply_wait(witness, reply.reference)
    assert reviewing.goals.goal_wait("owner") == wait


def test_capture_keeps_incarnation_across_rename(reviewing):
    original = reviewing.registry.require("owner")
    reviewing.registry.rename("owner", "renamed-owner")
    captured = GoalWaitReview.capture(reviewing.goals, original.incarnation.require_recorded())
    assert captured.owner.name == "renamed-owner"
    assert captured.owner.incarnation == original.incarnation


def test_probe_observes_actual_exclusive_lock(probed):
    with probed.registry.store.locked():
        probed.bus.bus.probe(ReceivedSource.declared_name)
    assert probed.bus.bus.checks == [11]


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
    captured = GoalWaitReview.capture(
        reviewing.goals, reviewing.registry.require("owner").incarnation.require_recorded(),
    )
    title = GivenThreadText("Keep this concurrent title")
    reviewing.registry.register(replace(reviewing.registry.require("owner"), title=title))
    if rename:
        reviewing.registry.rename("owner", "renamed-owner")
    assert reviewing.goals._release_finished_wait(captured, fence) == (
        "renamed-owner" if rename else "owner",
    )
    assert reviewing.registry.require("owner").title == title
    assert reviewing.goals.goal_wait("owner") != captured.wait


def edit_waiting_goal(comms):
    comms.goals.update_goal("owner", EditGoalAction(text="Keep this newer objective"))


def edit_waiting_progress(comms):
    owner = comms.registry.require("owner")
    goal = owner.goal.require()
    comms.registry.register(replace(
        owner, goal=replace(goal, progress="Keep this newer progress", revision=goal.revision + 1),
    ))


def edit_waiting_title(comms):
    owner = comms.registry.require("owner")
    comms.registry.register(replace(owner, title=GivenThreadText("Keep this newer title")))


def stop_unrelated_thread(comms):
    comms.registry.register(comms.registry.require("other"), StoppedThreadStatus())


def release_terminal_wait(comms):
    fence = comms.agents.finish_turn(comms.registry.require("child").require_turn_lease())
    return partial(comms.goals.release_waits_after_terminal_turn, fence)


def recover_closed_wait(comms):
    comms.agents.finish_turn(comms.registry.require("child").require_turn_lease())
    comms.registry.unregister("child")
    return partial(comms.goals.recover_closed_goal_wait, "owner")


@pytest.mark.parametrize("release", (release_terminal_wait, recover_closed_wait))
@pytest.mark.parametrize("change", (
    edit_waiting_goal, edit_waiting_progress, edit_waiting_title, stop_unrelated_thread,
))
def test_retained_wait_releases_in_same_call_after_concurrent_edit(reviewing, release, change):
    reviewing.registry.declare(Thread("other", frozenset(), str(reviewing.root)))
    reviewing.goals.update_goal("owner", standby(reviewing))
    wait = reviewing.goals.goal_wait("owner")
    current_goal = reviewing.registry.require("owner").goal.require()
    release_once = release(reviewing)

    def change_after_snapshot():
        nonlocal current_goal
        change(reviewing)
        assert reviewing.goals.goal_wait("owner") == wait
        current_goal = reviewing.registry.require("owner").goal.require()

    reviewing.bus.bus = AfterReadBus(reviewing.bus.bus.path, change_after_snapshot)
    assert release_once() == ("owner",)
    assert reviewing.bus.bus.changed
    assert reviewing.goals.goal_wait("owner") == NoGoalWait()
    published = reviewing.registry.require("owner").goal.require()
    assert published.revision == current_goal.revision + 1
    assert published.progress.startswith(current_goal.progress)
    assert "Standby was released" in published.progress
    assert replace(published, progress=current_goal.progress, revision=current_goal.revision) == current_goal


@pytest.mark.parametrize("change", (edit_waiting_goal, edit_waiting_progress))
def test_completed_reply_consumes_retained_wait_after_concurrent_goal_edit(reviewing, change):
    witness, reply, wait = completed_reply(reviewing)
    reviewing.bus.bus = AfterReadBus(reviewing.bus.bus.path, partial(change, reviewing))
    assert reviewing.goals.consume_reply_wait(witness, reply.reference)
    assert reviewing.bus.bus.changed
    assert reviewing.goals.goal_wait("owner") == NoGoalWait()
    assert reviewing.registry.require("owner").goal.require().revision == wait.revision + 1


def test_new_dependency_turn_during_silence_read_keeps_wait(reviewing):
    reviewing.goals.update_goal("owner", standby(reviewing))
    wait = reviewing.goals.goal_wait("owner")
    fence = reviewing.agents.finish_turn(reviewing.registry.require("child").require_turn_lease())
    reviewing.bus.bus = AfterReadBus(
        reviewing.bus.bus.path, partial(reviewing.agents.begin_turn, "child", "new-child-turn"),
    )
    assert reviewing.goals.release_waits_after_terminal_turn(fence) == ()
    assert reviewing.bus.bus.changed
    assert reviewing.goals.goal_wait("owner") == wait


def test_terminal_callback_reobserves_same_wait_after_waiter_turn_changes(reviewing):
    reviewing.goals.update_goal("owner", standby(reviewing))
    wait = reviewing.goals.goal_wait("owner")
    owner = reviewing.registry.require("owner")
    fence = reviewing.agents.finish_turn(reviewing.registry.require("child").require_turn_lease())

    def finish_waiter_turn():
        witness = reviewing.agents.begin_turn("owner", TurnId('0ab2ad7eead38307a0a663568aed1d8b'))
        reviewing.agents.finish_turn(witness.thread.require_turn_lease())
        assert reviewing.goals.goal_wait("owner") == wait

    reviewing.bus.bus = AfterReadBus(reviewing.bus.bus.path, finish_waiter_turn)
    assert reviewing.goals.release_waits_after_terminal_turn(fence) == ("owner",)
    assert reviewing.registry.require("owner").turn_generation > owner.turn_generation
    assert reviewing.goals.goal_wait("owner") == NoGoalWait()
