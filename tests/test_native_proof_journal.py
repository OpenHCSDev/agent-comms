"""The Pi process's proof journal, written by the pinned package and read by Core."""
import json
import subprocess
from pathlib import Path

from agent_comms.execution.native_input_proof import NativeContextJournal


def test_a_request_records_an_input_once_and_always_the_newest(pinned_native, tmp_path):
    result = subprocess.run(
        ["node", str(Path(__file__).with_name("native_proof_journal_contract.mjs")),
         str(pinned_native), str(tmp_path / "journal")],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    written = json.loads(result.stdout)
    # Five requests over a context that grows to three inputs; the fourth is a new process.
    assert written["requests"] == [["a"], ["b"], ["b"], ["c"], ["c"]]
    with NativeContextJournal.open_evidence(Path(written["file"])) as db:
        rows = NativeContextJournal.select(db, order_by=("request_generation", "input_id"))
        assert [(row.input_id[0], row.request_generation) for row in rows] == [
            ("a", 1), ("b", 2), ("b", 3), ("c", 4), ("c", 5)]
