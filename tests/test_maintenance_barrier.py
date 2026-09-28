"""Provider-free disposable checks of the default-off maintenance claim seam."""

import json
import multiprocessing as mp
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_comms.backend import _maintenance_send_boundary, stream_agent_events
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.errors import RelationViolationError
from agent_comms.maintenance_barrier import MaintenanceBarrier
from agent_comms.registration import Registration
from agent_comms.store_files import _store_lock
from agent_comms.threads import Thread
from maintenance_control_fixture import FixtureMaintenanceControl


def test_production_has_no_same_uid_phase_mutator(tmp_path: Path) -> None:
    import agent_comms.maintenance_barrier as production
    from agent_comms.acp import CommsAgent

    gate = Comms(tmp_path / "fresh").owners.maintenance
    assert gate.read() is None
    for name in ("begin", "advance", "_write_unlocked", "release", "reopen"):
        assert not hasattr(gate, name)
        assert not hasattr(production, name)
        with pytest.raises(AttributeError):
            getattr(gate, name)
    for name in ("maintenance_begin", "maintenance_advance", "maintenance_reopen"):
        assert not hasattr(Comms, name)
        assert not hasattr(CommsAgent, name)
    assert not hasattr(production, "_atomic_write_text")
    assert not gate.marker_path.exists() and not gate.state_path.exists()


def test_production_only_import_cannot_reach_disposable_control(tmp_path: Path) -> None:
    src = Path(__file__).resolve().parents[1] / "src"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import importlib.util\n"
            "from agent_comms.maintenance_barrier import MaintenanceBarrier\n"
            "assert not hasattr(MaintenanceBarrier, 'begin'); "
            "assert not hasattr(MaintenanceBarrier, 'advance'); "
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
    assert gate.read() is None
    comms.threads.register(
        Thread(
            name="owner",
            tags=frozenset(),
            worktree=str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid()),
        )
    )
    assert comms.agents.begin_turn("owner", "before")
    comms.agents.finish_turn(comms.registry.require("owner").turn_lease)
    control = FixtureMaintenanceControl(gate)
    first = control.begin("operator-one")
    assert first.phase == "draining" and first.generation == 1
    assert MaintenanceBarrier(comms.registry.store.path).read() == first
    with pytest.raises(RelationViolationError, match="Maintenance"):
        comms.agents.begin_turn("owner", "after")
    with pytest.raises(RelationViolationError, match="Maintenance"):
        comms.registry.lease_live_turn_with_admission(
            comms.registry.require("owner"),
            "direct",
            expected_generation=comms.registry.snapshot().admission_generations["owner"],
        )
    with pytest.raises(RelationViolationError, match="Maintenance"):
        comms.owners.start("owner")
    second = control.advance(first, "paused")
    assert second.generation == 2
    with pytest.raises(RelationViolationError, match="epoch/operator"):
        control.advance(first, "paused")
    with pytest.raises(ValueError, match="closed expected"):
        control.advance(second, "ready")
    with pytest.raises(RelationViolationError, match="Maintenance"):
        Registration(comms.registry.store.path).lease_local_turn("owner", "cold")
    assert control.advance(second, "installing").phase == "installing"


@pytest.mark.parametrize("generation", [True, 1.0, "1", -1, 0, 1, 2])
def test_legacy_state_generation_requires_exact_matching_positive_int(
    tmp_path: Path, generation: object
) -> None:
    gate = MaintenanceBarrier(tmp_path / "wire" / "registry.json")
    assert gate.read() is None  # Fresh roots remain default OFF.
    FixtureMaintenanceControl(gate).begin("disposable-fixture")
    state = json.loads(gate.state_path.read_text())
    state["phase"] = "ready"  # Disposable READY detects a false-open parser result.
    state["generation"] = generation
    gate.state_path.write_text(json.dumps(state))
    if type(generation) is int and generation == 1:
        assert gate.read().phase == "ready"
        with gate.admit_ingress() as receipt:
            assert receipt is not None and receipt.phase == "ready"
    else:
        with pytest.raises(RelationViolationError, match="Maintenance witness inconsistent"):
            gate.read()
        with (
            pytest.raises(RelationViolationError, match="Maintenance witness inconsistent"),
            gate.admit_ingress(),
        ):
            pytest.fail("Malformed generation must never admit ingress")


@pytest.mark.parametrize(
    "fault",
    ["missing-state", "missing-marker", "corrupt-state", "stale-generation", "symlink-state"],
)
def test_enabled_witness_damage_never_restores_default_off(tmp_path: Path, fault: str) -> None:
    gate = MaintenanceBarrier(tmp_path / "wire" / "registry.json")
    FixtureMaintenanceControl(gate).begin("operator")
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


def test_direct_claim_and_bind_denied_after_phase_ack_in_other_process(tmp_path: Path) -> None:
    root = tmp_path / "wire"
    comms = Comms(root)
    q: mp.Queue = mp.Queue()
    ready = mp.Event()
    proc = mp.Process(target=_claim_other_process, args=(str(comms.registry.store.path), ready, q))
    proc.start()
    assert proc.pid is not None
    comms.threads.register(
        Thread(
            name="owner",
            tags=frozenset(),
            worktree=str(tmp_path),
            process_identity=ProcessIdentity.capture(proc.pid),
        )
    )
    receipt = FixtureMaintenanceControl(comms.owners.maintenance).begin("operator")
    ready.set()
    proc.join(10)
    assert proc.exitcode == 0
    assert q.get(timeout=2)[0] == "denied"
    with (
        pytest.raises(RelationViolationError, match="Maintenance"),
        _maintenance_send_boundary(root, None, None, "native", "original"),
    ):
        pytest.fail("Native stdin would be written")
    assert comms.owners.maintenance.read() == receipt
    assert comms.registry.require("owner").active_turn is None


@pytest.mark.parametrize("fault_at", ["before-state", "after-state"])
def test_unknown_parent_fsync_does_not_reopen_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault_at: str
) -> None:
    import maintenance_control_fixture as fixture_module

    gate = MaintenanceBarrier(tmp_path / "wire" / "registry.json")
    real_write = fixture_module._atomic_write_text

    def uncertain_write(path: Path, text: str, *, fsync_parent: bool = False) -> None:
        if path == gate.state_path and fault_at == "before-state":
            raise OSError("injected before replacement")
        real_write(path, text, fsync_parent=fsync_parent)
        if path == gate.state_path and fault_at == "after-state":
            raise OSError("injected after replacement before acknowledged transition")

    monkeypatch.setattr(fixture_module, "_atomic_write_text", uncertain_write)
    with pytest.raises(OSError, match="injected"):
        FixtureMaintenanceControl(gate).begin("operator")
    with pytest.raises(RelationViolationError, match="Maintenance"):
        gate.assert_open_unlocked()
    if fault_at == "before-state":
        with pytest.raises(RelationViolationError, match="Maintenance"):
            gate.read()
    else:
        assert gate.read().phase == "draining"  # Visible state is not an ACK.


def test_rename_and_stopped_same_pid_cannot_reactivate_under_gate(tmp_path: Path) -> None:
    comms = Comms(tmp_path / "wire")
    owner = Thread(
        name="owner",
        tags=frozenset(),
        worktree=str(tmp_path),
        process_identity=ProcessIdentity.capture(os.getpid()),
    )
    comms.threads.register(owner)
    comms.registry.rename("owner", "renamed")
    comms.registry.unregister("renamed")
    FixtureMaintenanceControl(comms.owners.maintenance).begin("operator")
    with pytest.raises(RelationViolationError, match="Maintenance"):
        comms.owners.start("owner")
    with pytest.raises(RelationViolationError, match="Maintenance"):
        comms.registry.register(comms.registry.require("renamed"), new_owner=True)
    assert comms.registry.require("renamed").pid == os.getpid()


def test_cross_process_claim_races_pause_at_registry_lock(tmp_path: Path) -> None:
    root = tmp_path / "wire"
    comms = Comms(root)
    q: mp.Queue = mp.Queue()
    ready = mp.Event()
    child = mp.Process(target=_claim_other_process, args=(str(comms.registry.store.path), ready, q))
    child.start()
    assert child.pid is not None
    comms.threads.register(
        Thread(
            name="owner",
            tags=frozenset(),
            worktree=str(tmp_path),
            process_identity=ProcessIdentity.capture(child.pid),
        )
    )
    ready.set()
    receipt = FixtureMaintenanceControl(comms.owners.maintenance).begin("operator")
    child.join(10)
    assert child.exitcode == 0
    result, _ = q.get(timeout=2)
    assert result in {"claimed", "denied"}
    current = comms.registry.require("owner")
    # A claim linearized before the pause is visible for explicit drain; a
    # claim after pause is denied. Neither is silently retried or erased.
    assert (current.active_turn is not None) == (result == "claimed")
    assert comms.owners.maintenance.read() == receipt
    with pytest.raises(RelationViolationError):
        comms.registry.lease_local_turn("owner", "never-after-pause")


@pytest.mark.asyncio
async def test_real_backend_fake_rpc_never_writes_prompt_after_pause(tmp_path: Path) -> None:
    import sys

    root = tmp_path / "wire"
    FixtureMaintenanceControl(MaintenanceBarrier(root / "registry.json")).begin("operator")
    marker = tmp_path / "sent"
    ready = tmp_path / "ready"
    stub = tmp_path / "pi-fake"
    stub.write_text(
        f"#!{sys.executable}\n"
        "import json, select, sys\n"
        "request = json.loads(sys.stdin.readline())\n"
        "cap = {'nativeInputProofCapability':'pi-native-input-v1-live-only'}\n"
        "reply = {'type':'response', 'command':'get_state', 'id':request['id'], "
        "'success':True, 'data':cap}\n"
        "print(json.dumps(reply), flush=True)\n"
        f"open({str(ready)!r}, 'w').write('RPC_READY')\n"
        "if select.select([sys.stdin], [], [], 1)[0] and sys.stdin.readline():\n"
        f"    open({str(marker)!r}, 'w').write('PROMPT')\n"
    )
    stub.chmod(0o700)
    result = [
        row
        async for row in stream_agent_events(
            str(stub), [], "fake-only", str(tmp_path), env_extra={"AGENT_COMMS_ROOT": str(root)}
        )
    ]
    assert ready.read_text() == "RPC_READY"  # Real RPC capability preflight ran.
    assert result[-1].ok is False
    assert not marker.exists()


def test_phase_change_waits_for_final_native_write_lock(tmp_path: Path) -> None:
    root = tmp_path / "wire"
    gate = MaintenanceBarrier(root / "registry.json")
    control = FixtureMaintenanceControl(gate)
    # The same lock spans final state check, delegate and fake stdin write.
    import threading

    entered = threading.Event()
    release = threading.Event()
    changed = threading.Event()

    def fake_send() -> None:
        with _maintenance_send_boundary(root, None, None, "id", "text") as allowed:
            assert allowed
            entered.set()
            assert release.wait(5)

    def pause() -> None:
        control.begin("operator")
        changed.set()

    sender = threading.Thread(target=fake_send)
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
