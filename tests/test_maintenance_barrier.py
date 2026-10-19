"""Provider-free disposable checks of the default-off maintenance claim seam."""

import asyncio
import json
import multiprocessing as mp
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.send_outcome import GrantedSend, HeldSend
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.maintenance_barrier import (
    DrainingPhase,
    InstallingPhase,
    MaintenanceBarrier,
    MaintenanceOperatorField,
    MaintenancePhase,
    PausedPhase,
    ReadyPhase,
)
from agent_comms.registration import Registration
from agent_comms.store_files import _async_store_lock, _store_lock
from agent_comms.threads import Thread


async def test_channel_post_queues_without_start_during_cutover(tmp_path: Path, monkeypatch) -> None:
    from agent_comms.coordination_cohort import next_sealed_assignment
    from agent_comms.coordinator import Coordination
    from agent_comms.routing import ScheduledTurn
    from agent_comms.schedule_rules import SelectedWakeCheck, WakeScheduleCheck
    from test_acp_private_nk_delivery import _session
    import agent_comms.native_pi as native_pi
    import agent_comms.private_nk_entrypoint as private_nk_entrypoint

    comms, agent, _root_id = _session(tmp_path)
    control = comms.owners.maintenance
    # A post pending when the drain begins; the drain refuses new publication.
    # Use the actual scratch bus, not a constructed message pretending to be a post.
    post = comms.messaging.send('sender', '#team', 'mid-drain channel post').message
    hold = control.begin(FieldCodec.decode(MaintenanceOperatorField, 'scratch-cutover'))
    queued = ScheduledTurn.incoming(post)
    starts = []

    async def start(*args, **kwargs):
        starts.append((args, kwargs))

    monkeypatch.setattr(agent.turns, 'run_agent_turn', start)
    check = WakeScheduleCheck(session_id='beta', inputs=agent.inputs)
    assert comms.owners.maintenance.read() == hold
    paths = agent.inputs._tick_paths()
    assert paths.maintenance == comms.owners.maintenance.witness_paths
    assert all(any(source.directory == path.parent and source.includes(os.fsencode(path.name))
                   for source in paths.watch_sources) for path in paths.maintenance)
    assert await agent.inputs.drain_inbox('beta') == 0
    assert await agent.inputs.drain_inbox('beta') == 0  # settled closed tick
    assert starts == [] and not any(state.wake_task for state in agent.inputs.drained.values())
    beta = comms.registry.require('beta')
    with Coordination(comms.root / 'coordination.sqlite3') as store:
        pending = next_sealed_assignment(store, RecordedThreadIncarnation("fixture", beta.created_at).lookup, beta.name)
        assert pending.wire_seq == post.seq
    assert not beta.turn_state.active.executing

    # The local queue follows the same typed gate; its origin is retained.
    agent.inputs.state('beta').pending_turns = [queued]
    assert await check.run() == hold
    assert agent.inputs.state('beta').pending_turns == [queued] and starts == []
    agent.inputs.state('beta').pending_turns = []
    for phase in (PausedPhase, InstallingPhase, ReadyPhase):
        hold = control.advance(hold, phase)
    assert comms.owners.maintenance.read().phase is ReadyPhase
    selected = []
    monkeypatch.setattr(SelectedWakeCheck, 'schedule', lambda check: selected.append(check))
    assert await agent.inputs.drain_inbox('beta') == 1
    assert len(selected) == 1
    with Coordination(comms.root / 'coordination.sqlite3') as store:
        assert next_sealed_assignment(store, RecordedThreadIncarnation("fixture", beta.created_at).lookup, beta.name).wire_seq == post.seq
    # Selection scheduled just before another drain must still refuse at the
    # effect boundary without consuming that durable assignment.
    another_drain = control.begin(FieldCodec.decode(MaintenanceOperatorField, 'second-cutover'))
    assert await selected[0].run() == another_drain
    with Coordination(comms.root / 'coordination.sqlite3') as store:
        assert next_sealed_assignment(store, RecordedThreadIncarnation("fixture", beta.created_at).lookup, beta.name).wire_seq == post.seq
    for phase in (PausedPhase, InstallingPhase, ReadyPhase):
        another_drain = control.advance(another_drain, phase)
    agent.inputs.state('beta').pending_turns = [queued]
    await check.run()
    assert len(starts) == 1 and starts[0][1]['origins'] == (post,)
    assert not agent.inputs.state('beta').pending_turns


@pytest.mark.parametrize('abort', [False, True])
async def test_cutover_clear_delivers_pending_channel_post(tmp_path: Path, monkeypatch, abort: bool) -> None:
    import agent_comms.native_pi as native_pi
    import agent_comms.private_nk_entrypoint as private_nk_entrypoint
    from agent_comms.tracked_turn import TrackedTurnSession
    from test_coordinated_runtime import _fake_model
    from agent_comms.coordination_cohort import next_sealed_assignment
    from agent_comms.coordinator import Coordination
    from test_acp_private_nk_delivery import _session

    comms, agent, _root_id = _session(tmp_path)
    gate = comms.owners.maintenance
    post = comms.messaging.send('sender', '#team', 'while draining').message
    hold = gate.begin(FieldCodec.decode(MaintenanceOperatorField, 'scratch-cutover'))
    assert await agent.inputs.drain_inbox('beta') == 0
    assert await agent.inputs.drain_inbox('beta') == 0
    beta = comms.registry.require('beta')
    with Coordination(comms.root / 'coordination.sqlite3') as store:
        assert next_sealed_assignment(store, RecordedThreadIncarnation("fixture", beta.created_at).lookup, beta.name).wire_seq == post.seq
    assert not beta.turn_state.active.executing
    if abort:
        assert gate.clear(hold).phase is ReadyPhase
    else:
        for phase in (PausedPhase, InstallingPhase, ReadyPhase):
            hold = gate.advance(hold, phase)
    fake, calls = _fake_model(decision='FULL')
    monkeypatch.setattr(TrackedTurnSession, 'execute', fake)
    assert await agent.inputs.drain_inbox('beta') == 1
    task = agent.inputs.state('beta').wake_task
    assert task is not None
    await task
    assert calls and 'while draining' in calls[0][1]
    assert await agent.inputs.drain_inbox('beta') == 0  # same post is not replayed


@pytest.mark.parametrize('text', [None, '', 'x' * 129, 42])
def test_maintenance_operator_rejects_invalid_external_text(text: object) -> None:
    with pytest.raises((ValueError, TypeError)):
        FieldCodec.decode(MaintenanceOperatorField, text)


def test_draining_lends_existing_turn_but_holds_new_prompt(tmp_path: Path) -> None:
    gate = MaintenanceBarrier(tmp_path / 'wire' / 'registry.json')
    hold = gate.begin(FieldCodec.decode(MaintenanceOperatorField, 'operator'))
    assert hold.phase is DrainingPhase
    with gate.read().phase.send_new(None) as new:
        assert isinstance(new, HeldSend)
    with gate.read().phase.send_running(None) as running:
        assert isinstance(running, GrantedSend)
    paused = gate.advance(hold, PausedPhase)
    with paused.phase.send_running(None) as running:
        assert isinstance(running, HeldSend)
    gate.advance(paused, InstallingPhase)


def test_phase_owner_is_default_off_and_clear_is_exact(tmp_path: Path) -> None:
    gate = Comms(tmp_path / 'fresh').owners.maintenance
    assert gate.read().phase is ReadyPhase
    hold = gate.begin(FieldCodec.decode(MaintenanceOperatorField, 'operator'))
    assert gate.read() == hold
    ready = gate.clear(hold)
    assert ready.phase is ReadyPhase and gate.read().phase is ReadyPhase
    with pytest.raises(RelationViolationError, match='generation/operator'):
        gate.clear(hold)
    assert gate.marker_path.is_file() and gate.state_path.is_file()
    assert gate.read().operator == FieldCodec.decode(MaintenanceOperatorField, 'operator')


def test_only_hold_bound_cutover_may_retire_during_draining(tmp_path: Path) -> None:
    from agent_comms.owner_cutover import MaintainedOwnerCutover, PreserveOwnerRuntime
    from agent_comms.store_files import _store_lock

    class ScratchCutover(MaintainedOwnerCutover):
        def require_selection(self, snapshot, owners):
            pass

        def complete(self, stopped):
            raise AssertionError('No process should be stopped by a phase check')

    comms = Comms(tmp_path / 'wire')
    hold = comms.owners.maintenance.begin(FieldCodec.decode(MaintenanceOperatorField, 'scratch-cutover'))
    cutover = ScratchCutover()
    cutover.maintenance_hold = hold
    with _store_lock(comms.root / 'wire'):
        cutover.require_restart_phase(comms.owners)
        from agent_comms.restart_refusals import OwnerMaintenancePendingRefusal

        with pytest.raises(OwnerMaintenancePendingRefusal, match='Maintenance is holding') as refused:
            PreserveOwnerRuntime().require_restart_phase(comms.owners)
        assert refused.value.queue_state().pending  # Idle reload remains queued, not UNKNOWN.
    comms.owners.maintenance.clear(hold)
    with _store_lock(comms.root / 'wire'):
        with pytest.raises(RelationViolationError, match='phase changed'):
            cutover.require_restart_phase(comms.owners)


def test_clear_stopped_uses_acquired_wire_descriptor(tmp_path: Path) -> None:
    gate = MaintenanceBarrier(tmp_path / 'wire' / 'registry.json')
    hold = gate.begin(FieldCodec.decode(MaintenanceOperatorField, 'stopped-cutover'))
    with _store_lock(gate.wire_path) as lock:
        assert gate.clear_stopped(hold, lock.descriptor).phase is ReadyPhase
    assert gate.read().phase is ReadyPhase
    with _store_lock(tmp_path / 'unrelated') as other:
        with pytest.raises(RelationViolationError, match='wire lock changed'):
            next_hold = gate.begin(FieldCodec.decode(MaintenanceOperatorField, 'new-cutover'))
            gate.clear_stopped(next_hold, other.descriptor)


def test_production_only_import_cannot_reach_disposable_control(tmp_path: Path) -> None:
    src = Path(__file__).resolve().parents[1] / "src"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import importlib.util\n"
            "from agent_comms.maintenance_barrier import MaintenanceBarrier\n"
            "assert callable(MaintenanceBarrier.begin); "
            "assert callable(MaintenanceBarrier.advance); "
            "assert importlib.util.find_spec('agent_comms.maintenance_control_fixture') is None",
        ],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(src)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def _claim_other_process(registry_path: str, ready: mp.Event, result: mp.Queue) -> None:
    if not ready.wait(10):
        result.put(("timeout", ""))
        return
    try:
        Registration(Path(registry_path)).lease_local_turn("owner", "other-process")
    except RelationViolationError as error:
        result.put(("denied", str(error)))
    else:
        result.put(("claimed", ""))


def test_default_off_then_close_reopen_and_no_stale_transition(tmp_path: Path) -> None:
    comms = Comms(tmp_path / "wire")
    gate = comms.owners.maintenance
    assert gate.read().phase is ReadyPhase
    comms.registry.declare(
        comms.registry.new_thread(name='owner', tags=frozenset(), worktree=str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
    )
    assert comms.agents.begin_turn("owner", "before")
    comms.agents.finish_turn(comms.registry.require("owner").turn_lease)
    control = gate
    first = control.begin(FieldCodec.decode(MaintenanceOperatorField, "operator-one"))
    assert first.phase == MaintenancePhase.member_named("draining") and first.generation == 1
    assert MaintenanceBarrier(comms.registry.store.path).read() == first
    with pytest.raises(RelationViolationError, match="Maintenance"):
        comms.agents.begin_turn("owner", "after")
    with pytest.raises(RelationViolationError, match="Maintenance"):
        comms.registry.lease_live_turn_with_claim(
            comms.registry.require("owner"),
            "direct",
            expected_generation=comms.registry.snapshot().claimed_generations["owner"],
        )
    with pytest.raises(RelationViolationError, match="Maintenance"):
        comms.owners.start("owner")
    second = control.advance(first, MaintenancePhase.member_named("paused"))
    assert second.generation == 2
    with pytest.raises(RelationViolationError, match="generation/operator"):
        control.advance(first, MaintenancePhase.member_named("paused"))
    with pytest.raises(ValueError, match="valid expected successor"):
        control.advance(second, MaintenancePhase.member_named("ready"))
    with pytest.raises(RelationViolationError, match="Maintenance"):
        Registration(comms.registry.store.path).lease_local_turn("owner", "cold")
    assert control.advance(
        second, MaintenancePhase.member_named("installing")
    ).phase == MaintenancePhase.member_named("installing")


@pytest.mark.parametrize("generation", [True, 1.0, "1", -1, 0, 1, 2])
def test_state_generation_requires_exact_matching_positive_int(
    tmp_path: Path, generation: object
) -> None:
    gate = MaintenanceBarrier(tmp_path / "wire" / "registry.json")
    assert gate.read().phase is ReadyPhase  # Fresh roots remain default OFF.
    gate.begin(FieldCodec.decode(MaintenanceOperatorField, "disposable-fixture"))
    state = json.loads(gate.state_path.read_text())
    state["phase"] = "ready"  # Disposable READY detects a false-open parser result.
    state["generation"] = generation
    gate.state_path.write_text(json.dumps(state))
    if type(generation) is int and generation == 1:
        assert gate.read().phase == MaintenancePhase.member_named("ready")
        with gate.allow_input() as hold:
            assert hold.phase == MaintenancePhase.member_named("ready")
    else:
        with pytest.raises(RelationViolationError, match="Maintenance witness"):
            gate.read()
        with (
            pytest.raises(RelationViolationError, match="Maintenance witness"),
            gate.allow_input(),
        ):
            pytest.fail("Malformed generation must never admit ingress")


@pytest.mark.parametrize(
    "fault",
    ["missing-state", "missing-marker", "corrupt-state", "stale-generation", "symlink-state"],
)
def test_enabled_witness_damage_never_restores_default_off(tmp_path: Path, fault: str) -> None:
    gate = MaintenanceBarrier(tmp_path / "wire" / "registry.json")
    gate.begin(FieldCodec.decode(MaintenanceOperatorField, "operator"))
    if fault == "missing-state":
        gate.state_path.unlink()
    elif fault == "missing-marker":
        gate.marker_path.unlink()
    elif fault == "corrupt-state":
        gate.state_path.write_text("{")
    elif fault == "stale-generation":
        state = json.loads(gate.state_path.read_text())
        state["generation"] += 1
        gate.state_path.write_text(json.dumps(state))
    else:
        gate.state_path.unlink()
        gate.state_path.symlink_to(gate.marker_path)
    with pytest.raises(RelationViolationError, match="Maintenance"):
        MaintenanceBarrier(gate.registry_path).assert_open_unlocked()


async def test_direct_claim_and_bind_denied_after_phase_ack_in_other_process(tmp_path: Path) -> None:
    root = tmp_path / "wire"
    comms = Comms(root)
    q: mp.Queue = mp.Queue()
    ready = mp.Event()
    proc = mp.Process(target=_claim_other_process, args=(str(comms.registry.store.path), ready, q))
    proc.start()
    assert proc.pid is not None
    comms.registry.declare(
        comms.registry.new_thread(name='owner', tags=frozenset(), worktree=str(tmp_path), process_identity=ProcessIdentity.capture(proc.pid))
    )
    hold = comms.owners.maintenance.begin(FieldCodec.decode(MaintenanceOperatorField, "operator"))
    ready.set()
    proc.join(10)
    assert proc.exitcode == 0
    assert q.get(timeout=2)[0] == "denied"
    async with MaintenanceBarrier(root / "registry.json").ingress_async() as status:
        with status.phase.send_new(None, None, "native", "original") as outcome:
            assert isinstance(outcome, HeldSend)
    assert comms.owners.maintenance.read() == hold
    assert not comms.registry.require("owner").turn_state.active.executing


@pytest.mark.parametrize("fault_at", ["before-state", "after-state"])
def test_unknown_parent_fsync_does_not_reopen_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault_at: str
) -> None:
    import agent_comms.maintenance_barrier as barrier_module

    gate = MaintenanceBarrier(tmp_path / "wire" / "registry.json")
    real_write = barrier_module._atomic_write_text

    def uncertain_write(path: Path, text: str, *, fsync_parent: bool = False) -> None:
        if path == gate.state_path and fault_at == "before-state":
            raise OSError("injected before replacement")
        real_write(path, text, fsync_parent=fsync_parent)
        if path == gate.state_path and fault_at == "after-state":
            raise OSError("injected after replacement before acknowledged transition")

    monkeypatch.setattr(barrier_module, "_atomic_write_text", uncertain_write)
    with pytest.raises(OSError, match="injected"):
        gate.begin(FieldCodec.decode(MaintenanceOperatorField, "operator"))
    with pytest.raises(RelationViolationError, match="Maintenance"):
        gate.assert_open_unlocked()
    if fault_at == "before-state":
        with pytest.raises(RelationViolationError, match="Maintenance"):
            gate.read()
    else:
        assert gate.read().phase == MaintenancePhase.member_named(
            "draining"
        )  # Visible state is not an ACK.


def test_rename_and_stopped_same_pid_cannot_reactivate_under_gate(tmp_path: Path) -> None:
    comms = Comms(tmp_path / "wire")
    owner = comms.registry.new_thread(name='owner', tags=frozenset(), worktree=str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
    comms.registry.declare(owner)
    comms.registry.rename("owner", "renamed")
    comms.registry.unregister("renamed")
    comms.owners.maintenance.begin(FieldCodec.decode(MaintenanceOperatorField, "operator"))
    with pytest.raises(RelationViolationError, match="Maintenance"):
        comms.owners.start("owner")
    with pytest.raises(RelationViolationError, match="Maintenance"):
        comms.registry.register(comms.registry.require("renamed"), new_owner=True)
    assert comms.registry.require("renamed").same_local_process(owner.require_process())


def test_cross_process_claim_races_pause_at_registry_lock(tmp_path: Path) -> None:
    root = tmp_path / "wire"
    comms = Comms(root)
    q: mp.Queue = mp.Queue()
    ready = mp.Event()
    child = mp.Process(target=_claim_other_process, args=(str(comms.registry.store.path), ready, q))
    child.start()
    assert child.pid is not None
    comms.registry.declare(
        comms.registry.new_thread(name='owner', tags=frozenset(), worktree=str(tmp_path), process_identity=ProcessIdentity.capture(child.pid))
    )
    ready.set()
    hold = comms.owners.maintenance.begin(FieldCodec.decode(MaintenanceOperatorField, "operator"))
    child.join(10)
    assert child.exitcode == 0
    result, _ = q.get(timeout=2)
    assert result in {"claimed", "denied"}
    current = comms.registry.require("owner")
    # A claim linearized before the pause is visible for explicit drain; a
    # claim after pause is denied. Neither is silently retried or erased.
    assert current.turn_state.active.executing == (result == "claimed")
    assert comms.owners.maintenance.read() == hold
    with pytest.raises(RelationViolationError):
        comms.registry.lease_local_turn("owner", "never-after-pause")


@pytest.mark.asyncio
async def test_actual_native_held_input_resumes_from_existing_observer(native_backend):
    from agent_comms.backend import TurnSession
    from native_backend_fixture import wait_for

    owner = native_backend
    async with owner.open_owner(runtime_enabled=True) as (agent, session_id):
        async with owner.original_input(agent, session_id, "Held original input") as turn:
            gate = agent._comms.owners.maintenance
            hold = gate.begin(FieldCodec.decode(MaintenanceOperatorField, "operator"))
            sending = asyncio.create_task(turn.run_native())
            try:
                await wait_for(lambda: sending in TurnSession.active and
                    not TurnSession.active[sending].inputs.queue.ready.is_set())
                running = TurnSession.active[sending]
                assert running.native.proc.alive() and not sending.done()
                assert not running.inputs.pending and owner.provider.posts == 0
                gate.clear(hold)
                await asyncio.wait_for(sending, 20)
                assert owner.provider.posts == 1
                assert len(owner.saved_inputs()) == 1
                assert running.inputs.queue.empty() and not running.inputs.pending
                assert turn.progress.result.ok
            finally:
                sending.cancel()
                await asyncio.gather(sending, return_exceptions=True)


async def test_native_ingress_waits_without_blocking_exclusive_owner(tmp_path: Path) -> None:
    root = tmp_path / "wire"
    entered = asyncio.Event()

    async def native_write():
        async with MaintenanceBarrier(root / "registry.json").ingress_async() as status:
            with status.phase.send_running(None, None, "native", "original"):
                entered.set()

    async with _async_store_lock(root / "wire"):
        send = asyncio.create_task(native_write())
        try:
            # The original exclusive owner must resume on the same loop while
            # its concurrent native write waits for shared ingress ownership.
            await asyncio.sleep(0.025)
            assert not entered.is_set()
        except BaseException:
            send.cancel()
            await asyncio.gather(send, return_exceptions=True)
            raise
    await asyncio.wait_for(send, 1)
    assert entered.is_set()


def test_phase_change_waits_for_final_native_write_lock(tmp_path: Path) -> None:
    root = tmp_path / "wire"
    gate = MaintenanceBarrier(root / "registry.json")
    control = gate
    # The same lock spans final state check, delegate and fake stdin write.
    import threading

    entered = threading.Event()
    release = threading.Event()
    changed = threading.Event()

    async def fake_send() -> None:
        async with MaintenanceBarrier(root / "registry.json").ingress_async() as status:
            with status.phase.send_running(None, None, "id", "text"):
                entered.set()
                assert release.wait(5)

    def pause() -> None:
        control.begin(FieldCodec.decode(MaintenanceOperatorField, "operator"))
        changed.set()

    sender = threading.Thread(target=lambda: asyncio.run(fake_send()))
    sender.start()
    assert entered.wait(5)
    closer = threading.Thread(target=pause)
    closer.start()
    assert not changed.wait(0.05)
    release.set()
    sender.join(5)
    closer.join(5)
    assert changed.is_set()
    with _store_lock(root / "wire"), pytest.raises(RelationViolationError, match="Maintenance"):
        gate.assert_open_unlocked()


def test_core_writes_outside_a_watch_are_refused_while_draining(tmp_path: Path, capsys) -> None:
    """A sender or tool-use hook is not paused by the drain; Core refuses its write instead."""
    from agent_comms.cli import main

    def cli(*argv: str) -> tuple[int, str]:
        code = main(['--root', str(tmp_path), *argv])
        return code, capsys.readouterr().out

    for name in ('a', 'b'):
        assert cli('register', '--name', name, '--worktree', '/wt')[0] == 0
    def written() -> tuple[bytes, ...]:
        return tuple(path.read_bytes() if path.exists() else b''
                     for path in (tmp_path / 'bus.jsonl', tmp_path / 'activity.jsonl'))

    before = written()
    gate = MaintenanceBarrier(tmp_path / 'registry.json')
    hold = gate.begin(FieldCodec.decode(MaintenanceOperatorField, 'upgrade-bus'))
    for argv in (('send', '--from', 'a', '--to', 'b', '--body', 'hi'),
                 ('activity', '--name', 'a', '--state', 'working')):
        code, out = cli(*argv)
        assert code != 0 and 'under maintenance' in out
    assert written() == before
    gate.clear(hold)
    assert cli('send', '--from', 'a', '--to', 'b', '--body', 'hi')[0] == 0


async def test_bus_waits_before_mutating_a_transcript_batch(tmp_path: Path) -> None:
    from functools import partial
    from agent_comms.activity import ActivityState
    from agent_comms.runtime_updates import AppendDraftItem, StartedItem, TextDraft, ThreadOutput
    from agent_comms.maintenance_barrier import MaintenanceOperator
    from agent_comms.recorded_time import RecordedEventTime
    from agent_comms.transcript_events import AssistantTranscript

    comms = Comms(tmp_path)
    comms.registry.declare(comms.registry.new_thread('cli', frozenset(), str(tmp_path)))
    output = ThreadOutput(comms.transcripts.external('cli'), comms.agents)
    updates = [StartedItem('turn', 'item', ActivityState.WORKING, 'Replying',
                          TextDraft.from_parts(AssistantTranscript, ('prefix',), RecordedEventTime(1.), 'item')),
               AppendDraftItem('turn', 'item', ' once')]
    barrier = MaintenanceBarrier(comms.registry.store.path)
    hold = barrier.begin(MaintenanceOperator('batch-check'))
    pending = asyncio.create_task(comms.bus.when_open(partial(output.publish_batch, updates)))
    try:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(.05):
                await asyncio.shield(pending)
        assert output.drafts == {}
        assert not output.transcript.path.exists()
    finally:
        barrier.clear(hold)
    await asyncio.wait_for(pending, 3)
    with output.transcript.reading() as source:
        assert source.current_draft(output.transcript.root, output.transcript.incarnation).external_row().events == (AssistantTranscript('prefix once', timestamp=1., position='item'),)
