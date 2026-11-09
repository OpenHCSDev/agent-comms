"""Selected capture borrows a real hold and drains real store transactions."""
import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from threading import Event

import pytest

from agent_comms.cli import build_parser
from agent_comms.comms import Comms, wire
from agent_comms.context_store import ContextStore
from agent_comms.coordination.database import CoordinationStore
from agent_comms.errors import RelationViolationError
from agent_comms.execution.input_key import TurnInputKey
from agent_comms.execution.launch_runtime import LaunchRuntime
from agent_comms.input_disposition import InputDispositions
from agent_comms.installer.launch import InstallRun
from agent_comms.installer.practice import PracticeCopy
from agent_comms.installer.probes.inputs import SelectedInputsProbe
from agent_comms.installer.refusals import UpgradeRefused
from agent_comms.installer.request import InstallRequest
from agent_comms.messaging.bus import Bus
from agent_comms.nominal.field_codec import FieldCodec, TurnId
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.threads import NativeThread
from agent_comms.storage.maintenance_barrier import (
    MaintenanceBarrier,
    MaintenanceNotHeldError,
    MaintenanceOperator,
    NoMaintenanceHolder,
)
from agent_comms.storage.typed_table import WalStore
from bus_record_fixture import prepare_native_comms

type StoreOpening = Callable[[Path], AbstractContextManager[WalStore | CoordinationStore]]


@pytest.fixture
def capture_source(tmp_path: Path) -> Comms:
    comms = prepare_native_comms(wire(tmp_path / 'source'))
    comms.registry.declare(NativeThread('capture-owner', frozenset(), Checkout(tmp_path), created_at=17001.0))
    with comms.bus.bus.reading(), InputDispositions(comms.root).writing(), ContextStore(comms.root).writing():
        pass
    with CoordinationStore(CoordinationStore.path_in(comms.root)):
        pass
    return comms


def test_selected_capture_request_survives_cli_and_launcher_wire(tmp_path: Path) -> None:
    probe = SelectedInputsProbe(tmp_path / 'source', tmp_path / 'selected', 3 * 1024 ** 3)
    arguments = build_parser().parse_args((
        'install', '--checkout', str(tmp_path),
        '--selected-inputs', json.dumps(FieldCodec.encode((probe,))),
    ))
    command = arguments.command_declaration.from_arguments(arguments)
    assert command.selected_inputs == (probe,)
    run = InstallRun(command, probe.root, tmp_path / 'installer', 'a' * 40)
    decoded = FieldCodec.decode(InstallRun, FieldCodec.encode(run))
    assert decoded.command == FieldCodec.decode(InstallRequest, FieldCodec.encode(command))
    assert decoded.command.selected_inputs == (probe,)
    assert not probe.destination.exists()


def test_selected_capture_reads_original_inputs_before_releasing_its_cut(
    capture_source: Comms, tmp_path: Path,
) -> None:
    source = capture_source
    entry = source.registry.snapshot().entry('capture-owner')
    lookup = entry.thread.incarnation.require_recorded().lookup
    key = TurnInputKey(TurnId('b' * 32))
    InputDispositions(source.root).record(
        key, seq=None, owner=entry.thread.name, claim_scope=1,
        target=entry.thread.name, text='original unresolved input',
    )
    gate = MaintenanceBarrier(source.registry.store.path)
    held = gate.retarget(gate.begin(MaintenanceOperator.current()), LaunchRuntime.running())
    destination = tmp_path / 'read-back'
    try:
        with held.holding():
            measured = SelectedInputsProbe(source.root, destination, 3 * 1024**3, (lookup,)).run()
            assert measured.bytes > 0
            assert InputDispositions(destination).unresolved((entry,)) == InputDispositions(
                source.root,
            ).unresolved((entry,))
    finally:
        gate.clear(held)


def test_capture_refuses_a_destination_inside_its_source(
    capture_source: Comms,
) -> None:
    copy = PracticeCopy(capture_source.root, capture_source.root / 'nested-copy')
    with pytest.raises(UpgradeRefused, match='separate destination'), copy.capturing_inputs():
        pytest.fail('capture accepted a destination inside its donor')
    assert not copy.path.exists()


def test_selected_probe_refuses_oversized_complete_readers_before_copying(
    capture_source: Comms, tmp_path: Path,
) -> None:
    source = capture_source
    gate = MaintenanceBarrier(source.registry.store.path)
    held = gate.retarget(gate.begin(MaintenanceOperator.current()), LaunchRuntime.running())
    destination = tmp_path / 'too-small'
    try:
        with held.holding(), pytest.raises(UpgradeRefused, match='Complete input readers'):
            SelectedInputsProbe(source.root, destination, 1).run()
        assert not Bus.path_in(destination).exists()
        assert not InputDispositions.path_in(destination).exists()
    finally:
        gate.clear(held)


stores = pytest.mark.parametrize('opening', (
    pytest.param(lambda root: nullcontext(Bus(Bus.path_in(root))), id='bus'),
    pytest.param(lambda root: nullcontext(InputDispositions(root)), id='ledger'),
    pytest.param(lambda root: nullcontext(ContextStore(root)), id='contexts'),
    pytest.param(lambda root: CoordinationStore(CoordinationStore.path_in(root)), id='coordinator'),
))


def test_selected_capture_refuses_an_open_or_only_draining_root(
    capture_source: Comms, tmp_path: Path,
) -> None:
    source = capture_source
    copy = PracticeCopy(source.root, tmp_path / 'selected')
    gate = MaintenanceBarrier(source.registry.store.path)
    with pytest.raises(MaintenanceNotHeldError), copy.capturing_inputs():
        pytest.fail('open root supplied a capture cut')
    held = gate.begin(MaintenanceOperator.current())
    try:
        with held.holding(), pytest.raises(MaintenanceNotHeldError), copy.capturing_inputs():
            pytest.fail('unretargeted drain supplied a capture cut')
    finally:
        gate.clear(held)
    assert not copy.path.exists()


def test_selected_capture_requires_holder_and_actual_target_runtime(
    capture_source: Comms, tmp_path: Path,
) -> None:
    source = capture_source
    copy = PracticeCopy(source.root, tmp_path / 'selected')
    gate = MaintenanceBarrier(source.registry.store.path)
    held = gate.retarget(gate.begin(MaintenanceOperator.current()),
                         LaunchRuntime(tmp_path / 'other-runtime/bin/python'))
    try:
        with held.holding(), pytest.raises(MaintenanceNotHeldError), copy.capturing_inputs():
            pytest.fail('another runtime supplied a capture cut')
        held = gate.retarget(held, LaunchRuntime.running())
        with NoMaintenanceHolder().selected(), pytest.raises(MaintenanceNotHeldError), copy.capturing_inputs():
            pytest.fail('non-holder supplied a capture cut')
    finally:
        gate.clear(held)
    assert not copy.path.exists()


@stores
def test_capture_waits_for_each_already_admitted_transaction(
    capture_source: Comms, tmp_path: Path, opening: StoreOpening,
) -> None:
    source = capture_source
    admitted, release, started, captured = Event(), Event(), Event(), Event()
    copy = PracticeCopy(source.root, tmp_path / 'selected')
    gate = MaintenanceBarrier(source.registry.store.path)

    def writer() -> None:
        with NoMaintenanceHolder().selected(), opening(source.root) as store, store.writing() as db:
            db.execute('SELECT 1').fetchone()
            admitted.set()
            assert release.wait(5), 'controller did not release the admitted writer'

    with ThreadPoolExecutor(max_workers=2) as workers:
        pending = workers.submit(writer)
        try:
            assert admitted.wait(5), 'writer did not enter its ordinary transaction'
            held = gate.retarget(gate.begin(MaintenanceOperator.current()), LaunchRuntime.running())

            def capture() -> None:
                with held.holding():
                    started.set()
                    with copy.capturing_inputs():
                        captured.set()

            taking = workers.submit(capture)
            assert started.wait(5)
            assert not captured.wait(0.05), 'capture passed an admitted SQL writer'
            release.set()
            pending.result(timeout=5)
            taking.result(timeout=5)
            assert captured.is_set()
            gate.clear(held)
        finally:
            release.set()


@stores
def test_capture_refuses_later_non_holder_store_writes(
    capture_source: Comms, tmp_path: Path, opening: StoreOpening,
) -> None:
    source = capture_source
    gate = MaintenanceBarrier(source.registry.store.path)
    held = gate.retarget(gate.begin(MaintenanceOperator.current()), LaunchRuntime.running())
    copy = PracticeCopy(source.root, tmp_path / 'selected')
    try:
        with held.holding(), copy.capturing_inputs():
            with NoMaintenanceHolder().selected(), pytest.raises(RelationViolationError):
                with opening(source.root) as store, store.writing():
                    pytest.fail('later non-holder writer entered the captured store')
    finally:
        gate.clear(held)


def test_capture_keeps_original_registry_association_through_first_copy(
    capture_source: Comms, tmp_path: Path,
) -> None:
    source = capture_source
    expected = source.registry.snapshot().entry('capture-owner')
    before = source.bus.bus.digest(source.bus.bus.path)
    gate = MaintenanceBarrier(source.registry.store.path)
    held = gate.retarget(gate.begin(MaintenanceOperator.current()), LaunchRuntime.running())
    copy = PracticeCopy(source.root, tmp_path / 'selected')
    try:
        with held.holding(), copy.capturing_inputs() as original:
            assert original.snapshot.entry('capture-owner') == expected
            assert original.matches_current()
            copy.include((Bus.path_in(source.root), InputDispositions.path_in(source.root)))
        assert Bus.digest(Bus.path_in(copy.path)) == before
        assert source.registry.snapshot().entry('capture-owner') == expected
        assert source.bus.bus.digest(source.bus.bus.path) == before
        with held.holding(), pytest.raises(UpgradeRefused), copy.capturing_inputs():
            pytest.fail('capture started after its first source copy')
    finally:
        gate.clear(held)


def test_capture_rejects_a_holder_side_effect_instead_of_accepting_mixed_sources(
    capture_source: Comms, tmp_path: Path,
) -> None:
    source = capture_source
    gate = MaintenanceBarrier(source.registry.store.path)
    held = gate.retarget(gate.begin(MaintenanceOperator.current()), LaunchRuntime.running())
    ledger = InputDispositions(source.root)
    key = TurnInputKey(TurnId('a' * 32))
    copy = PracticeCopy(source.root, tmp_path / 'selected')
    try:
        with held.holding(), pytest.raises(RelationViolationError):
            with copy.capturing_inputs():
                copy.include((ledger.path,))
                ledger.record(key, seq=None, owner='capture-owner', claim_scope=1,
                              target='capture-owner', text='holder wrote after copying')
        assert key not in ledger.read((key,)).rows
        assert key not in InputDispositions(copy.path).read((key,)).rows
    finally:
        gate.clear(held)


def test_capture_borrows_one_hold_generation_and_releases_resources_on_failure(
    capture_source: Comms, tmp_path: Path,
) -> None:
    source = capture_source
    gate = MaintenanceBarrier(source.registry.store.path)
    runtime = LaunchRuntime.running()
    held = gate.retarget(gate.begin(MaintenanceOperator.current()), runtime)
    copy = PracticeCopy(source.root, tmp_path / 'selected')
    try:
        with held.holding(), pytest.raises(MaintenanceNotHeldError):
            with gate.capturing(runtime):
                held = gate.retarget(held, runtime)
        with held.holding(), pytest.raises(RuntimeError, match='stop materialization'):
            with copy.capturing_inputs():
                raise RuntimeError('stop materialization')
    finally:
        gate.clear(held)
    with InputDispositions(source.root).writing():
        pass
    assert not copy.path.exists()
