"""Provider-free disposable checks of the default-off maintenance claim seam."""

import asyncio
import json
import multiprocessing as mp
import os
from pathlib import Path

import pytest
from agent_comms.lifecycle_capability import Executing

from agent_comms.send_outcome import GrantedSend, HeldSend
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.maintenance_barrier import (
    DrainingPhase,
    InstallingPhase,
    MaintenanceBarrier,
    MaintenanceOperator,
    MaintenancePhase,
    PausedPhase,
    ReadyPhase,
)
from agent_comms.registration import Registration
from agent_comms.store_files import _async_store_lock, _store_lock


@pytest.mark.parametrize('text', [None, '', 'x' * 129, 42])
def test_maintenance_operator_rejects_invalid_external_text(text: object) -> None:
    with pytest.raises((ValueError, TypeError)):
        FieldCodec.decode(MaintenanceOperator, text)


def test_draining_lends_existing_turn_but_holds_new_prompt(tmp_path: Path) -> None:
    gate = MaintenanceBarrier(tmp_path / 'wire' / 'registry.json')
    hold = gate.begin(MaintenanceOperator.current())
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
    hold = gate.begin(MaintenanceOperator.current())
    assert gate.read() == hold
    ready = gate.clear(hold)
    assert ready.phase is ReadyPhase and gate.read().phase is ReadyPhase
    with pytest.raises(RelationViolationError):
        gate.clear(hold)
    assert gate.marker_path.is_file() and gate.state_path.is_file()
    assert gate.read().operator == MaintenanceOperator.current()


def test_only_hold_bound_cutover_may_retire_during_draining(tmp_path: Path) -> None:
    from agent_comms.owner_cutover import MaintainedOwnerCutover, PreserveOwnerRuntime

    class ScratchCutover(MaintainedOwnerCutover):
        def require_selection(self, snapshot, owners):
            pass

        def complete(self, stopped):
            raise AssertionError('No process should be stopped by a phase check')

    comms = Comms(tmp_path / 'wire')
    hold = comms.owners.maintenance.begin(MaintenanceOperator.current())
    cutover = ScratchCutover()
    cutover.maintenance_hold = hold
    with comms.registry.store.locked():
        cutover.require_restart_phase(comms.owners)
        from agent_comms.restart_refusals import OwnerMaintenancePendingRefusal

        with pytest.raises(OwnerMaintenancePendingRefusal) as refused:
            PreserveOwnerRuntime().require_restart_phase(comms.owners)
        from agent_comms.lifecycle_capability import Pending

        assert refused.value.queue_state().items_with(Pending, refused.value)  # Idle reload remains queued, not UNKNOWN.
    comms.owners.maintenance.clear(hold)
    with comms.registry.store.locked():
        with pytest.raises(RelationViolationError):
            cutover.require_restart_phase(comms.owners)


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
    first = control.begin(MaintenanceOperator.current())
    assert first.phase == MaintenancePhase.member_named("draining") and first.generation == 1
    assert MaintenanceBarrier(comms.registry.store.path).read() == first
    with pytest.raises(RelationViolationError):
        comms.agents.begin_turn("owner", "after")
    with pytest.raises(RelationViolationError):
        comms.registry.lease_live_turn_with_claim(
            comms.registry.require("owner"),
            "direct",
            expected_generation=comms.registry.snapshot().claim_generation("owner"),
        )
    with pytest.raises(RelationViolationError):
        comms.owners.start("owner")
    second = control.advance(first, MaintenancePhase.member_named("paused"))
    assert second.generation == 2
    with pytest.raises(RelationViolationError):
        control.advance(first, MaintenancePhase.member_named("paused"))
    with pytest.raises(ValueError):
        control.advance(second, MaintenancePhase.member_named("ready"))
    with pytest.raises(RelationViolationError):
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
    gate.begin(MaintenanceOperator.current())
    state = json.loads(gate.state_path.read_text())
    state["phase"] = "ready"  # Disposable READY detects a false-open parser result.
    state["generation"] = generation
    gate.state_path.write_text(json.dumps(state))
    if type(generation) is int and generation == 1:
        assert gate.read().phase == MaintenancePhase.member_named("ready")
        with gate.allow_input() as hold:
            assert hold.phase == MaintenancePhase.member_named("ready")
    else:
        with pytest.raises(RelationViolationError):
            gate.read()
        with (
            pytest.raises(RelationViolationError),
            gate.allow_input(),
        ):
            pytest.fail("Malformed generation must never admit ingress")


@pytest.mark.parametrize(
    "fault",
    ["missing-state", "missing-marker", "corrupt-state", "stale-generation", "symlink-state"],
)
def test_enabled_witness_damage_never_restores_default_off(tmp_path: Path, fault: str) -> None:
    gate = MaintenanceBarrier(tmp_path / "wire" / "registry.json")
    gate.begin(MaintenanceOperator.current())
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
    with pytest.raises(RelationViolationError):
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
    hold = comms.owners.maintenance.begin(MaintenanceOperator.current())
    ready.set()
    proc.join(10)
    assert proc.exitcode == 0
    assert q.get(timeout=2)[0] == "denied"
    async with MaintenanceBarrier(root / "registry.json").ingress_async() as status:
        with status.phase.send_new(None, None, "native", "original") as outcome:
            assert isinstance(outcome, HeldSend)
    assert comms.owners.maintenance.read() == hold
    assert not bool(comms.registry.require('owner').turn_state.active.items_with(Executing, comms.registry.require('owner').turn_state.active))


def test_rename_and_stopped_same_pid_cannot_reactivate_under_gate(tmp_path: Path) -> None:
    comms = Comms(tmp_path / "wire")
    owner = comms.registry.new_thread(name='owner', tags=frozenset(), worktree=str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
    comms.registry.declare(owner)
    comms.registry.rename("owner", "renamed")
    comms.registry.unregister("renamed")
    comms.owners.maintenance.begin(MaintenanceOperator.current())
    with pytest.raises(RelationViolationError):
        comms.owners.start("owner")
    with pytest.raises(RelationViolationError):
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
    hold = comms.owners.maintenance.begin(MaintenanceOperator.current())
    child.join(10)
    assert child.exitcode == 0
    result, _ = q.get(timeout=2)
    assert result in {"claimed", "denied"}
    current = comms.registry.require("owner")
    # A claim linearized before the pause is visible for explicit drain; a
    # claim after pause is denied. Neither is silently retried or erased.
    assert bool(current.turn_state.active.items_with(Executing, current.turn_state.active)) == (result == "claimed")
    assert comms.owners.maintenance.read() == hold
    with pytest.raises(RelationViolationError):
        comms.registry.lease_local_turn("owner", "never-after-pause")


async def test_native_ingress_waits_without_blocking_exclusive_owner(tmp_path: Path) -> None:
    root = tmp_path / "wire"
    entered = asyncio.Event()

    async def native_write():
        async with MaintenanceBarrier(root / "registry.json").ingress_async() as status:
            with status.phase.send_running(None, None, "native", "original"):
                entered.set()

    async with _async_store_lock(root / "registry.json"):
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
        control.begin(MaintenanceOperator.current())
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
    with _store_lock(root / "registry.json"), pytest.raises(RelationViolationError):
        gate.assert_open_unlocked()


def test_activity_write_is_refused_while_draining(tmp_path: Path, capsys) -> None:
    """Activity is not a publication request and remains refused during a hold."""
    from agent_comms.activity import ActivityStore
    from agent_comms.bus import Bus
    from agent_comms.cli import main

    def cli(*argv: str) -> tuple[int, str]:
        code = main(['--root', str(tmp_path), *argv])
        return code, capsys.readouterr().out

    bus = Bus.install(Bus.path_in(tmp_path))
    for name in ('a', 'b'):
        assert cli('register', '--name', name, '--worktree', '/wt')[0] == 0
    def written() -> tuple[int, bytes]:
        activity = ActivityStore.path_in(tmp_path)
        return bus.revision(), activity.read_bytes() if activity.exists() else b''

    before = written()
    gate = MaintenanceBarrier(tmp_path / 'registry.json')
    hold = gate.begin(MaintenanceOperator.current())
    code, out = cli('activity', '--name', 'a', '--state', 'working')
    assert code != 0 and 'under maintenance' in out
    assert written() == before
    gate.clear(hold)
    assert cli('send', '--from', 'a', '--to', 'b', '--body', 'hi')[0] == 0


async def test_bus_waits_before_mutating_a_transcript_batch(tmp_path: Path) -> None:
    from functools import partial
    from agent_comms.runtime_updates import AppendDraftItem, StartedItem, TextDraft, ThreadOutput
    from agent_comms.turn_phase import ModelWaitPhase
    from agent_comms.recorded_time import RecordedEventTime
    from agent_comms.transcript_events import AssistantTranscript

    comms = Comms(tmp_path)
    comms.registry.declare(comms.registry.new_thread('cli', frozenset(), str(tmp_path)))
    output = ThreadOutput(comms.transcripts.external('cli'), comms.agents)
    updates = [StartedItem('turn', 'item', ModelWaitPhase('Replying'),
                          TextDraft.from_parts(AssistantTranscript, ('prefix',), RecordedEventTime(1.), 'item')),
               AppendDraftItem('turn', 'item', ' once')]
    barrier = MaintenanceBarrier(comms.registry.store.path)
    hold = barrier.begin(MaintenanceOperator.current())
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


def test_a_registry_write_is_refused_to_all_but_the_maintenance_holder(tmp_path: Path, monkeypatch) -> None:
    """The installer stops only its owners; the connector, bridges and forks keep running, so the
    registry itself refuses their writes during a hold, and the holder's own writes still land."""
    from agent_comms.maintenance_barrier import HOLDER_NONCE

    comms = Comms(tmp_path)
    comms.registry.declare(comms.registry.new_thread(name='a', tags=frozenset(), worktree='/wt'))
    before = comms.registry.store.path.read_bytes()
    gate = MaintenanceBarrier(comms.registry.store.path)
    hold = gate.begin(MaintenanceOperator.current())
    monkeypatch.delenv(HOLDER_NONCE, raising=False)
    with pytest.raises(RelationViolationError):
        comms.registry.declare(comms.registry.new_thread(name='b', tags=frozenset(), worktree='/wt'))
    assert comms.registry.store.path.read_bytes() == before
    monkeypatch.setenv(HOLDER_NONCE, hold.nonce)
    comms.registry.declare(comms.registry.new_thread(name='b', tags=frozenset(), worktree='/wt'))
    assert 'b' in comms.registry
    gate.clear(hold)


def test_a_carried_store_write_is_refused_to_all_but_the_maintenance_holder(tmp_path: Path, monkeypatch) -> None:
    """Every file an installation carries gates its writers through its kind, CarriedWriter:
    a bridge's transcript or a fork's todo write during a hold is refused instead of
    changing a file the install's carry check compares, and the holder still writes."""
    from agent_comms.external_transcript import ExternalTranscript
    from agent_comms.maintenance_barrier import HOLDER_NONCE
    from agent_comms.typed_table import CarriedWriter, DurableStore, DerivedStore, RuntimeWriter

    assert issubclass(DurableStore, CarriedWriter) and issubclass(ExternalTranscript, CarriedWriter)
    assert issubclass(DerivedStore, RuntimeWriter)
    comms = Comms(tmp_path)
    todos = comms.goals.todos
    todos.allocate_problem()
    before = todos.path.read_bytes()
    gate = MaintenanceBarrier(comms.registry.store.path)
    hold = gate.begin(MaintenanceOperator.current())
    monkeypatch.delenv(HOLDER_NONCE, raising=False)
    with pytest.raises(RelationViolationError):
        todos.allocate_problem()
    assert todos.path.read_bytes() == before
    monkeypatch.setenv(HOLDER_NONCE, hold.nonce)
    todos.allocate_problem()
    gate.clear(hold)


def test_every_carried_json_document_writes_through_the_maintenance_gate() -> None:
    """A durable JSON document gets the gate from its kind, as the registry does, and the
    runtime projection that running owners rewrite during maintenance does not."""
    from agent_comms.bus_projection import AppendProjectionStore
    from agent_comms.catalog_store import ChannelCatalog
    from agent_comms.connector_store import ConnectorStore
    from agent_comms.maintenance_barrier import CarriedDocument
    from agent_comms.read_ledger import ReadLedger
    from agent_comms.registry_store import RegistryStore
    from agent_comms.relationships import RelationshipStore
    from agent_comms.runtime_children import RuntimeChildrenStore
    from agent_comms.runtime_info import RuntimeInfoStore
    from agent_comms.shared_ledger import SharedLedger

    for store in (ChannelCatalog, ConnectorStore, ReadLedger, RegistryStore, RelationshipStore,
                  RuntimeChildrenStore, RuntimeInfoStore, SharedLedger):
        assert issubclass(store, CarriedDocument), store
    assert not issubclass(AppendProjectionStore, CarriedDocument)
