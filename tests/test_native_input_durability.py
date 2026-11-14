"""The Pi session journal knows which entries a sync covered."""
import json
import subprocess
from pathlib import Path


def test_one_sync_covers_what_the_journal_held_and_nothing_appended_after(pinned_native, tmp_path):
    result = subprocess.run(
        ["node", str(Path(__file__).with_name("native_entry_durability_contract.mjs")),
         str(pinned_native), str(tmp_path / "journal")],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {
        "memory": False, "opened": False, "same_store": True, "synced": True,
        "appended_after": False, "still": True, "synced_again": True,
    }
