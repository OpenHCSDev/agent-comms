"""Producer/cursor integration beyond the legacy bus byte and page caps.

Provider-free native fake only: a source certificate is not an ACK or input permit.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from agent_comms import coordinated_runtime as runtime
from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.comms import Comms
from agent_comms.coordinated_runtime_schema import install_native_runtime_schema
from agent_comms.coordination_cohort import accept_initial_cohort
from agent_comms.coordination_response import install_private_response_schema
from agent_comms.coordination_store import IdentityConflict, MutationStore
from agent_comms.declarations import RelationViolationError, Thread
from agent_comms.native_prompt_binding import install_prompt_binding_schema
from agent_comms.native_source_cursor import read_current_native_cursor
from agent_comms.private_bus_checkpoint import install_private_bus_checkpoint
from test_native_prompt_binding import _fake_model


@pytest.fixture
def tmp_path():
    if os.name != "posix" or not Path("/var/tmp").is_dir() or Path("/var").is_symlink():
        pytest.skip("sealed runtime requires a disposable /var/tmp")
    with tempfile.TemporaryDirectory(prefix="ac-checkpoint-cursor-", dir="/var/tmp") as name:
        yield Path(name)


def _root(tmp_path: Path):
    root = tmp_path / "wire"
    root.mkdir(mode=0o700)
    comms = Comms(root, private_initial_writes=True)
    people = [
        Thread("sender", frozenset(), str(tmp_path), pid=os.getpid()),
        Thread(
            "alpha",
            frozenset({"team"}),
            str(tmp_path),
            pid=os.getpid(),
            model="openai-codex/gpt-6-sol",
        ),
        Thread("other", frozenset(), str(tmp_path), pid=os.getpid()),
    ]
    for person in people:
        comms.threads.register(person)
    root_id = comms.messaging.initialize_private_initial_protocol()
    comms.messaging.initialize_private_claim_protocol()
    install_private_bus_checkpoint(comms.bus)  # fresh private root only
    first = comms.messaging.send_initial_cohort("sender", "#team", "selected one")
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        install_private_cohort_schema(store)
        install_private_response_schema(store)
        install_native_runtime_schema(store)
        install_prompt_binding_schema(store)
        lookup = stable_thread_lookup(people[1].created_at)
        store.register_participant(lookup, "alpha", "alpha", committed=True)
        accept_initial_cohort(comms.bus, root_id, first.seq, store)
    return root, root_id, comms, first, lookup


@pytest.mark.parametrize("migrate_existing", [False, True])
async def test_fresh_open_1002_initials_over_eight_mib_remain_exact(
    tmp_path, monkeypatch, migrate_existing
):
    root, root_id, comms, first, lookup = _root(tmp_path)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="IGNORE")
    monkeypatch.setattr(runtime, "run_native_pi_turn", fake)
    first_turn = await runtime.run_one_sealed_claim(
        root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
    )
    assert first_turn is not None and first_turn.cursor_status == "proven"
    for number in range(1000):
        comms.messaging.send_initial_cohort("sender", "other", f"unrelated-{number:04}-" + "x" * 8700)
    second = comms.messaging.send_initial_cohort("sender", "#team", "selected after 1000 other rows")
    assert second.seq == first.seq + 1001 and comms.bus._path.stat().st_size > 8 * 1024 * 1024
    if migrate_existing:
        # Build the large fixture through the real certified publisher, then
        # remove only its certificate to represent the same pre-migration bus.
        # This avoids O(n**2) fixture setup through the old unindexed writer.
        marker_path = root / "bus_meta.json"
        marker = json.loads(marker_path.read_text())
        del marker["checkpoint_version"]
        del marker["checkpoint_seal"]
        marker_path.write_text(json.dumps(marker))
        (root / "private_bus_checkpoint.sqlite3").unlink()
        before = comms.bus._path.read_bytes()
        witness = install_private_bus_checkpoint(Comms(root).bus)
        assert witness.through_seq == second.seq
        assert comms.bus._path.read_bytes() == before
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        accept_initial_cohort(comms.bus, root_id, second.seq, store)
    second_turn = await runtime.run_one_sealed_claim(
        root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
    )
    assert second_turn is not None and second_turn.cursor_status == "proven"
    assert len(calls) == 2 and second_turn.input_id != first_turn.input_id
    with MutationStore(str(root / "coordination.sqlite3")) as reopened:
        current = read_current_native_cursor(
            Comms(root).bus, reopened, wire_root_id=root_id, owner_name="alpha"
        )
        assert current is not None
        assert current.covered_seq == current.injected_seq == second.seq
        assert current.input_id == second_turn.input_id
        assert current.recipient_lookup == lookup


async def test_certified_unproven_first_source_cannot_be_skipped(tmp_path, monkeypatch):
    root, root_id, comms, _first, lookup = _root(tmp_path)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    bad, bad_calls = _fake_model(decision="IGNORE", digest_override="b" * 64)
    monkeypatch.setattr(runtime, "run_native_pi_turn", bad)
    with pytest.raises(IdentityConflict, match="exact bound source prompt equality"):
        await runtime.run_one_sealed_claim(
            root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
        )
    assert len(bad_calls) == 1  # failed input is UNKNOWN; never retry it
    for number in range(101):
        comms.messaging.send_initial_cohort("sender", "other", f"unrelated-{number}")
    later = comms.messaging.send_initial_cohort("sender", "alpha", "later selected")
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        accept_initial_cohort(comms.bus, root_id, later.seq, store)
    good, good_calls = _fake_model(decision="IGNORE")
    monkeypatch.setattr(runtime, "run_native_pi_turn", good)
    result = await runtime.run_one_sealed_claim(
        root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
    )
    assert result is not None and result.cursor_status == "blocked_gap" and len(good_calls) == 1
    with MutationStore(str(root / "coordination.sqlite3")) as reopened:
        assert (
            reopened._connection.execute(
                "SELECT COUNT(*) FROM native_runtime_source_cursors WHERE recipient_lookup=?",
                (lookup,),
            ).fetchone()[0]
            == 0
        )
    assert not (root / "read_markers.json").exists()


async def test_pending_unknown_append_cold_rebuild_does_not_replay(tmp_path, monkeypatch):
    import agent_comms.private_bus_checkpoint as checkpoint

    root, root_id, comms, first, _lookup = _root(tmp_path)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="IGNORE")
    monkeypatch.setattr(runtime, "run_native_pi_turn", fake)
    turn = await runtime.run_one_sealed_claim(
        root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path
    )
    assert turn is not None and turn.cursor_status == "proven" and len(calls) == 1
    original = checkpoint.append_private_bus_checkpoint_unlocked

    def uncertain(*_args, **_kwargs):
        raise OSError("crash after durable original bus append")

    monkeypatch.setattr(checkpoint, "append_private_bus_checkpoint_unlocked", uncertain)
    with pytest.raises(RelationViolationError, match="outcome UNKNOWN"):
        comms.messaging.send_initial_cohort("sender", "other", "uncertain other original")
    assert len(comms.bus._path.read_bytes().splitlines()) == first.seq + 1
    monkeypatch.setattr(checkpoint, "append_private_bus_checkpoint_unlocked", original)
    with MutationStore(str(root / "coordination.sqlite3")) as reopened:
        retained = read_current_native_cursor(
            Comms(root).bus, reopened, wire_root_id=root_id, owner_name="alpha"
        )
        assert retained is not None and retained.input_id == turn.input_id
        assert retained.injected_seq == first.seq
    assert len(calls) == 1  # recovery certifies a source, not native acceptance


def test_checkpoint_index_rollback_denies_cursor_without_sql_mutation(tmp_path):
    root, root_id, comms, first, lookup = _root(tmp_path)
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        assert (
            read_current_native_cursor(comms.bus, store, wire_root_id=root_id, owner_name="alpha")
            is None
        )
    index = root / "private_bus_checkpoint.sqlite3"
    with __import__("sqlite3").connect(index) as db:
        db.execute("DELETE FROM addressed WHERE lookup=? AND seq=?", (lookup, first.seq))
    with MutationStore(str(root / "coordination.sqlite3")) as reopened:
        with pytest.raises((RelationViolationError, IdentityConflict)):
            read_current_native_cursor(
                Comms(root).bus, reopened, wire_root_id=root_id, owner_name="alpha"
            )
        assert (
            reopened._connection.execute(
                "SELECT COUNT(*) FROM native_runtime_source_cursors"
            ).fetchone()[0]
            == 0
        )
