"""Process absence is typed; registry JSON and release SQL keep their old shapes."""

import json
import signal
import sqlite3

import pytest

from agent_comms.child_process import NoProcess, ProcessIdentity, ProcessState
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.owner_lifecycle import OwnerReleaseEvidence


@pytest.mark.parametrize("process, wire", [(NoProcess(), None), (ProcessIdentity(123, 456), {"pid": 123, "start_time": 456})])
def test_process_field_keeps_null_or_exact_record(process, wire):
    assert FieldCodec.encode(process, ProcessState) == wire
    assert FieldCodec.decode(ProcessState, wire) == process
    assert json.dumps(FieldCodec.encode(process, ProcessState)) == json.dumps(wire)


@pytest.mark.parametrize("process", [NoProcess(), ProcessIdentity(123, 456)])
def test_thread_creation_result_carries_the_process_state(process):
    from agent_comms.cli_commands import ThreadCreatedResult

    result = ThreadCreatedResult("worker", process)
    assert FieldCodec.decode(ThreadCreatedResult, FieldCodec.encode(result)) == result


@pytest.mark.parametrize("wire", [False, 0, {}, {"pid": 0, "start_time": 1}, {"pid": True, "start_time": 1}])
def test_process_field_rejects_non_identity_shapes(wire):
    with pytest.raises((ValueError, TypeError)):
        FieldCodec.decode(ProcessState, wire)


def test_unstarted_process_does_not_signal_or_supply_a_pid():
    process = NoProcess()
    assert not process.alive()
    assert not process.offline()
    assert process.allows_owner_start()
    assert not process.runs_here()
    process.signal(signal.SIGTERM)
    assert process.pid_fields() == {}
    process.require_release_by(ProcessIdentity(123, 456))
    with pytest.raises(RelationViolationError):
        process.require("never started")


@pytest.mark.parametrize("process, expected", [(NoProcess(), (None, "null")),
    (ProcessIdentity(123, 456), ('{"pid":123,"start_time":456}', "text"))])
def test_release_rows_preserve_sql_null_and_record(process, expected):
    row = OwnerReleaseEvidence("owner", 1.0, process, 1, 2)
    with sqlite3.connect(":memory:") as db:
        OwnerReleaseEvidence.create(db)
        row.insert(db)
        encoded, kind = db.execute(
            f'SELECT process_identity, typeof(process_identity) FROM "{OwnerReleaseEvidence.declared_name}"'
        ).fetchone()
        assert (encoded, kind) == expected
        assert OwnerReleaseEvidence.one(db, name="owner") == row
