"""Read-error scopes preserve real SQLite failures, cleanup and tool outcomes."""
import sqlite3

import pytest

from agent_comms.coordination_database import CoordinationStore, StoreBusy
from agent_comms.coordination_errors import CoordinationReadUnavailable
from agent_comms.coordination_session import CoordinationSession
from agent_comms.coordinator import Coordination
from agent_comms import selected_tool_broker as broker


def test_busy_read_rolls_back_and_the_same_session_reads_after_unlock(tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / 'coordination.sqlite3'
    with CoordinationSession(str(path), contention=StoreBusy()) as session:
        with sqlite3.connect(path, isolation_level=None) as blocker:
            blocker.execute('BEGIN EXCLUSIVE')
            with pytest.raises(CoordinationReadUnavailable):
                with session.read():
                    pytest.fail('a locked read was allowed')
            assert not session._connection.in_transaction
            blocker.execute('ROLLBACK')
        with session.read():
            assert session._connection.execute('SELECT 1').fetchone()[0] == 1


def test_observation_preserves_original_sqlite_error_and_path_note(tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / 'coordination.sqlite3'
    with CoordinationSession(str(path)):
        pass
    with pytest.raises(sqlite3.OperationalError) as failure:
        with CoordinationStore.observing(path, StoreBusy()) as db:
            db.execute('SELECT * FROM nonexistent_table')
    assert failure.value.sqlite_errorcode == sqlite3.SQLITE_ERROR
    assert any(str(path) in note and 'SQLITE_ERROR' in note for note in failure.value.__notes__)
    with CoordinationStore.observing(path, StoreBusy()) as db:
        assert db.execute('SELECT 1').fetchone()[0] == 1


def test_tool_boundary_converts_an_already_mapped_busy_read(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    path = str(tmp_path / 'coordination.sqlite3')
    with Coordination(path, contention=StoreBusy()) as original:
        scoped = Coordination(path, contention=StoreBusy())
        monkeypatch.setattr(broker, 'Coordination', lambda _path: scoped)
        with sqlite3.connect(path, isolation_level=None) as blocker:
            blocker.execute('BEGIN EXCLUSIVE')
            with pytest.raises(broker.CoordinationBusy) as failure:
                broker.verify_sent_full_input(original, None, 'unused', '0' * 32)
            assert isinstance(failure.value.__cause__, CoordinationReadUnavailable)
            blocker.execute('ROLLBACK')
