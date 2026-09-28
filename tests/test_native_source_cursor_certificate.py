"""Certified source pages still require sealed and native owner-epoch proof."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
from pathlib import Path

import pytest

from agent_comms import coordinated_runtime as runtime
from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.child_process import ProcessIdentity
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.comms import Comms
from agent_comms.coordinated_runtime_schema import install_native_runtime_schema
from agent_comms.coordination_cohort import accept_initial_cohort
from agent_comms.coordination_response import install_private_response_schema
from agent_comms.coordination_store import MutationStore
from agent_comms.errors import RelationViolationError
from agent_comms.native_prompt_binding import install_prompt_binding_schema
from agent_comms.native_source_cursor import read_current_native_cursor
from agent_comms.private_bus_checkpoint import install_private_bus_checkpoint
from agent_comms.threads import Thread
from test_native_prompt_binding import _fake_model


@pytest.fixture
def tmp_path():
    if os.name != "posix" or not Path("/var/tmp").is_dir() or Path("/var").is_symlink():
        pytest.skip("private sealed runtime needs a disposable /var/tmp")
    with tempfile.TemporaryDirectory(prefix="ac-certified-cursor-", dir="/var/tmp") as path:
        yield Path(path)


def _fresh(tmp_path: Path, count: int = 2):
    root = tmp_path / "wire"
    root.mkdir(mode=0o700)
    comms = Comms(root, private_initial_writes=True)
    comms.threads.register(
        Thread(
            "sender",
            frozenset(),
            str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid()),
        )
    )
    for n in range(count):
        name = "alpha" if n == 0 else f"other{n:03}"
        comms.threads.register(
            Thread(
                name,
                frozenset({"team"}),
                str(tmp_path),
                process_identity=ProcessIdentity.capture(os.getpid()),
                model="fake/fake",
            )
        )
    root_id = comms.messaging.initialize_private_initial_protocol()
    comms.messaging.initialize_private_claim_protocol()
    install_private_bus_checkpoint(comms.bus.log)  # Strictly fresh-root opt-in.
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        install_private_cohort_schema(store)
        install_private_response_schema(store)
        install_native_runtime_schema(store)
        install_prompt_binding_schema(store)
        for n in range(count):
            name = "alpha" if n == 0 else f"other{n:03}"
            owner = comms.registry.require(name)
            store.register_participant(
                stable_thread_lookup(owner.created_at), name, name, committed=True
            )
    return root, root_id, comms


def _seal(comms: Comms, root: Path, root_id: str, target: str, body: str):
    message = comms.messaging.send_initial_cohort("sender", target, body)
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        assert accept_initial_cohort(comms.bus, root_id, message.seq, store).value.member_count
    return message


async def test_fresh_open_after_1001_unrelated_and_over_8mib(tmp_path, monkeypatch):
    root, root_id, comms = _fresh(tmp_path, 10)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="IGNORE")
    monkeypatch.setattr(runtime, "run_native_pi_turn", fake)
    first = _seal(comms, root, root_id, "alpha", "first selected")
    one = await runtime.SelectedExecution(
        root=root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
    ).run()
    assert one is not None and one.cursor_status == "proven"
    # The certificate filters 1,001 committed initials for another owner.
    # Do not accept, inject, acknowledge, or replay those unrelated sources.
    for n in range(1001):
        comms.messaging.send_initial_cohort("sender", "other001", f"unrelated-{n}:" + "x" * 8400)
    assert comms.bus.log.path.stat().st_size > 8 * 1024 * 1024
    selected = _seal(comms, root, root_id, "#team", "@alpha selected after churn")
    second = await runtime.SelectedExecution(
        root=root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
    ).run()
    assert second is not None and second.cursor_status == "proven"
    assert len(calls) == 2 and second.input_id != one.input_id
    with MutationStore(str(root / "coordination.sqlite3")) as reopened:
        cursor = read_current_native_cursor(
            comms.bus, reopened, wire_root_id=root_id, owner_name="alpha"
        )
        assert cursor is not None and cursor.input_id == second.input_id
        assert cursor.injected_seq == selected.seq and cursor.covered_seq == selected.seq
        assert first.seq < selected.seq


async def test_addressed_no_wake_page_boundary_does_not_become_injection(tmp_path, monkeypatch):
    root, root_id, comms = _fresh(tmp_path)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="IGNORE")
    monkeypatch.setattr(runtime, "run_native_pi_turn", fake)
    first = _seal(comms, root, root_id, "alpha", "first selected")
    one = await runtime.SelectedExecution(
        root=root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
    ).run()
    assert one is not None and one.cursor_status == "proven"
    for n in range(105):
        # Both owners are frozen recipients, but alpha receives only a sealed
        # no-wake delivery. Other's selected work is never processed here.
        _seal(comms, root, root_id, "#team", f"@other001 unrelated {n}")
    selected = _seal(comms, root, root_id, "alpha", "last exact selected")
    second = await runtime.SelectedExecution(
        root=root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
    ).run()
    assert second is not None and second.cursor_status == "proven"
    assert len(calls) == 2 and second.input_id != one.input_id
    with MutationStore(str(root / "coordination.sqlite3")) as reopened:
        cursor = read_current_native_cursor(
            comms.bus, reopened, wire_root_id=root_id, owner_name="alpha"
        )
        assert cursor is not None and cursor.input_id == second.input_id
        assert cursor.covered_seq == selected.seq and cursor.injected_seq == selected.seq
        assert cursor.injected_seq != first.seq


@pytest.mark.parametrize("recipients", [10, 150])
async def test_frozen_n_selected_cursor_provider_free(tmp_path, monkeypatch, recipients: int):
    root, root_id, comms = _fresh(tmp_path, recipients)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="IGNORE")
    monkeypatch.setattr(runtime, "run_native_pi_turn", fake)
    source = _seal(comms, root, root_id, "#team", "@alpha selected")
    frozen = comms.bus.log.read_initial_cohort(root_id, source.seq)
    assert len(frozen.audience.recipients) == recipients
    start = time.perf_counter()
    turn = await runtime.SelectedExecution(
        root=root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
    ).run()
    delivery_ms = (time.perf_counter() - start) * 1000
    assert turn is not None and turn.cursor_status == "proven" and len(calls) == 1
    start = time.perf_counter()
    with MutationStore(str(root / "coordination.sqlite3")) as reopened:
        current = read_current_native_cursor(
            comms.bus, reopened, wire_root_id=root_id, owner_name="alpha"
        )
    reconnect_ms = (time.perf_counter() - start) * 1000
    assert current is not None and current.input_id == turn.input_id
    assert current.injected_seq == current.covered_seq == source.seq
    print(
        f"certified frozen N={recipients}: fake full runner {delivery_ms:.3f} ms, "
        f"reconnect {reconnect_ms:.3f} ms"
    )


async def test_certified_cursor_rejects_changed_sidecar_without_replay(tmp_path, monkeypatch):
    root, root_id, comms = _fresh(tmp_path)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="IGNORE")
    monkeypatch.setattr(runtime, "run_native_pi_turn", fake)
    _seal(comms, root, root_id, "alpha", "exact selected")
    turn = await runtime.SelectedExecution(
        root=root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
    ).run()
    assert turn is not None and turn.cursor_status == "proven" and len(calls) == 1
    path = root / "private_bus_checkpoint.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM addressed")  # Disposable corrupt sidecar only.
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        with pytest.raises(RelationViolationError, match="seal changed"):
            read_current_native_cursor(comms.bus, store, wire_root_id=root_id, owner_name="alpha")
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM current_native_cursor"
            ).fetchone()[0]
            == 1
        )
    assert len(calls) == 1
