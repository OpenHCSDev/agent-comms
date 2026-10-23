"""Retire with the one-shot carry after the retained buses have been installed."""

import os
import shutil
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from agent_comms.coordination_database import CoordinationStore
from agent_comms.coordination_tables.attempts import AttemptRecord
from agent_comms.coordination_tables.participants import OwnerGenerations
from agent_comms.locked_store import RegistryFile, SQLiteFile
from agent_comms.maintenance_barrier import MaintenanceBarrier, MaintenanceNotHeldError, MaintenanceOperator
from agent_comms.native_prompt_binding import PromptBinding
from agent_comms.native_runtime_input import NativeRuntimeInput
from test_carry_bus_history import outgoing_source

PREDECESSOR = "f2f80357091809ec925b306b46a4b9ed6db812a5"


def generate(root):
    """Run under the predecessor's declarations, including its transition guards."""
    from agent_comms.native_input_record import FullNativeExecution
    from test_coordination_response import _ready

    root.mkdir()
    case = _ready(root, direct=True)
    try:
        row = NativeRuntimeInput(
            input_id="1" * 32, stage=FullNativeExecution,
            execution_id="exec", attempt_ordinal=1,
            owner_lookup=case.owner_lookup, owner_thread="owner", owner_generation=1,
            owner_token_digest=case.fence.authority.token_digest,
        )
        row.insert(case.store.session._connection)
        with closing(SQLiteFile.writer(PromptBinding.path_in(case.comms.root))) as db:
            PromptBinding.create(db)
            PromptBinding(
                input_id=row.input_id, stage=FullNativeExecution,
                execution_id="exec", attempt_ordinal=1,
                owner_lookup=case.owner_lookup, owner_thread="owner", owner_generation=1,
                wire_root_id=case.root_id, expected_prompt_digest="a" * 64, bound_at_ms=1,
            ).insert(db)
    finally:
        case.close()


def test_carry_keeps_existing_attempts_inputs_and_bindings(tmp_path):
    from cutover.thread_name_owners import ThreadNameOwners

    source = outgoing_source(tmp_path / "predecessor", PREDECESSOR)
    generated = tmp_path / "generated"
    runner = source / Path(__file__).name
    shutil.copyfile(__file__, runner)
    result = subprocess.run(
        [sys.executable, str(runner), str(generated)], cwd=source,
        env={**os.environ, "PYTHONPATH": os.pathsep.join((str(source / "src"), str(source / "tests")))},
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    root = generated / "wire"
    step = ThreadNameOwners(root, root, tmp_path / "spill")
    before = {path: path.read_bytes() for path in step.backup_files()}
    pending = step.carry(apply=False)
    assert pending.changed > 0
    assert pending.detail["attempts_before"] == pending.detail["attempts_after"] == 1
    assert pending.detail["native_runtime_inputs_before"] == pending.detail["native_runtime_inputs_after"] == 1
    assert pending.detail["prompt_binding_before"] == pending.detail["prompt_binding_after"] == 1
    assert all(path.read_bytes() == original for path, original in before.items())
    with pytest.raises(MaintenanceNotHeldError):
        step.carry(apply=True)
    assert all(path.read_bytes() == original for path, original in before.items())
    hold = MaintenanceBarrier(RegistryFile.path_in(root)).begin(MaintenanceOperator.current())
    with hold.holding():
        carried = step.carry(apply=True)
    assert carried.detail == pending.detail
    assert step.carry(apply=False).changed == 0
    with closing(SQLiteFile.read_snapshot(CoordinationStore.path_in(root))) as db:
        attempt = AttemptRecord.one(db, execution_id="exec", attempt_ordinal=1)
        native = NativeRuntimeInput.one(db, input_id="1" * 32)
        assert native.owner_identity == attempt.owner_identity
        assert OwnerGenerations.one(db, owner_lookup=attempt.owner_lookup) == attempt.owner_identity
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='participant_aliases'").fetchall()
    with closing(SQLiteFile.read_snapshot(PromptBinding.path_in(root))) as db:
        assert PromptBinding.one(db, input_id="1" * 32).owner_identity == native.owner_identity


if __name__ == "__main__":
    generate(Path(sys.argv[1]))
