"""The read index closes with its resources, before Python clears its modules."""
import gc
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import weakref

import pytest

from agent_comms.view_unread import TranscriptReadState, transcript_read_state


def test_collected_reader_cancels_and_joins_its_worker_then_closes_sqlite(tmp_path):
    source = tmp_path / "session.jsonl"
    source.write_text(json.dumps({"type": "message", "message": {
        "role": "assistant", "content": [{"type": "text", "text": "one reply"}],
    }}) + "\n")
    state = TranscriptReadState(tmp_path / "read_ledger.json")
    assert state.counts("viewer", {"worker": str(source)}).pending == {"worker"}
    state.preparation.result(timeout=10)
    workers = tuple(state._pool._processes.values())
    database, cancelled = state._connection, state._cancelled
    reference = weakref.ref(state)
    del state
    gc.collect()
    assert reference() is None
    assert cancelled.is_set()
    assert workers and all(not worker.is_alive() for worker in workers)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        database.execute("SELECT 1")


def test_database_without_worker_is_closed_when_reader_dies(tmp_path):
    state = TranscriptReadState(tmp_path / "read_ledger.json")
    database = state._database()
    assert state._pool is None
    reference = weakref.ref(state)
    del state
    gc.collect()
    assert reference() is None
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        database.execute("SELECT 1")


def test_explicit_close_is_idempotent_and_factory_replaces_closed_unused_reader(tmp_path):
    path = tmp_path / "read_ledger.json"
    state = transcript_read_state(path)
    state.close()
    state.close()
    with pytest.raises(RuntimeError, match="closed"):
        state.counts("viewer", {})
    fresh = transcript_read_state(path)
    assert fresh is not state
    fresh.close()


def test_comms_exits_before_multiprocessing_module_teardown(tmp_path):
    script = tmp_path / "exit_comms.py"
    script.write_text('''import atexit
import multiprocessing.synchronize as synchronize
from pathlib import Path
import sys

# This is the module-clearing stage that made the old destructor fail.
@atexit.register
def clear_module():
    synchronize.sys = None

from agent_comms.comms import Comms
comms = Comms(Path(sys.argv[1]))
print("Comms created; interpreter exits normally")
''')
    environment = os.environ.copy()
    environment.pop("AGENT_COMMS_NATIVE_CONFIG_DIR", None)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    result = subprocess.run([sys.executable, str(script), str(tmp_path / "wire")],
                            env=environment, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "Comms created" in result.stdout
    assert result.stderr == ""
