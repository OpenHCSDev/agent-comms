"""The Pi session's disk store reads a context's journal entries from its file once."""
import json
import subprocess
from pathlib import Path


def test_a_context_entry_is_read_once_and_a_new_one_is_read_when_it_arrives(pinned_native, tmp_path):
    result = subprocess.run(
        ["node", str(Path(__file__).with_name("native_context_entries_contract.mjs")),
         str(pinned_native), str(tmp_path / "session")],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {
        "first": ["a", "b", "c"], "first_reads": 3,
        "again": ["a", "b", "c"], "again_reads": 3,
        "after_append": ["a", "b", "c", "appended"], "after_append_reads": 4,
        "same_as_the_journal": True,
    }
