"""Run the pre-S5 reader/writer in a separate process against new output.

AGENT_COMMS_OLD_SOURCE selects an archived source tree, using the existing venv.
No provider, installation, live root or duplicate native package is involved.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_comms.declarations import Thread
from agent_comms.operations import Comms


@pytest.fixture
def old_source():
    path = os.environ.get("AGENT_COMMS_OLD_SOURCE")
    if not path:
        pytest.skip("Select archived pre-S5 source for mixed-version acceptance")
    return str(Path(path).resolve())


def old_process(old_source, root, action):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import json, sys
from dataclasses import replace
from pathlib import Path
from agent_comms.declarations import ThreadRegistry
registry = ThreadRegistry(Path(sys.argv[1]) / 'registry.json')
before = registry.snapshot()
owner = registry.require('owner')
if sys.argv[2] == 'update':
    registry.register(replace(owner, title='old UI title'))
elif sys.argv[2] == 'stop':
    registry.unregister('owner')
print(json.dumps({'before': dict(before.owner_epochs),
                  'after': dict(registry.snapshot().owner_epochs),
                  'turn': owner.active_turn.to_wire() if owner.active_turn else None}))
""",
            str(root),
            action,
        ],
        env=dict(os.environ, PYTHONPATH=old_source),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("private", [False, True])
def test_old_ui_update_preserves_active_turn_and_new_settlement(tmp_path, old_source, private):
    root = tmp_path / "wire"
    comms = Comms(root, private_initial_writes=private)
    comms.register(Thread("owner", frozenset(), str(tmp_path), pid=os.getpid()))
    # A high-water owner stamp demonstrates that the old reader did not reset it.
    for _ in range(3):
        comms.registry.register(comms.registry.require("owner"), new_owner=True)
    if private:
        comms.initialize_private_initial_protocol()
    fence = comms.begin_turn("owner", "active")
    before = comms.registry.snapshot()
    result = old_process(old_source, root, "update")
    after = comms.registry.snapshot()
    assert result["before"]["owner"] == before.owner_generations["owner"] > 1
    # A legacy writer still performs its own historical extra metadata bump.
    assert result["after"]["owner"] > result["before"]["owner"]
    assert after.owner_generations["owner"] == result["after"]["owner"]
    assert after.admission_generations == before.admission_generations
    assert after.threads["owner"].active_turn == before.threads["owner"].active_turn
    assert after.threads["owner"].turn_identity == fence.identity
    assert after.threads["owner"].title == "old UI title"
    assert comms.registry.live_owner_with_admission("owner")[1] == fence.admission_generation
    assert comms.registry.live_owner_with_generation("owner")[1] == after.owner_generations["owner"]
    assert comms.finish_turn("owner", "active", expected=fence) is not None
    persisted = json.loads((root / "registry.json").read_text())
    assert "owner_generations" not in persisted
    assert persisted["turn_epochs"] == {}
    assert old_process(old_source, root, "read")["after"] == result["after"]


def test_old_writer_stop_revokes_new_exact_turn(tmp_path, old_source):
    comms = Comms(tmp_path)
    comms.register(Thread("owner", frozenset(), str(tmp_path), pid=os.getpid()))
    fence = comms.begin_turn("owner", "active")
    old_process(old_source, tmp_path, "stop")
    assert comms.finish_turn("owner", "active", expected=fence) is None
    comms.registry.register(comms.registry.require("owner"))
    newer = comms.begin_turn("owner", "active")
    assert newer.identity.generation > fence.identity.generation
    assert comms.finish_turn("owner", "active", expected=fence) is None
    assert comms.finish_turn("owner", "active", expected=newer) is not None


def test_old_ui_can_read_and_extend_new_read_ledger(tmp_path, old_source):
    comms = Comms(tmp_path)
    comms.register(Thread("owner", frozenset(), str(tmp_path)))
    viewer = comms.user_identity(str(tmp_path)).name
    first = comms.send_message("owner", viewer, "new core paint")
    page = comms.dm_display_page("owner", worktree=str(tmp_path))
    comms.mark_dm_view_read(
        "owner",
        worktree=str(tmp_path),
        through=first.seq,
        expected_display_basis=page.display_basis,
    )
    second = comms.send_message("owner", viewer, "old UI paint")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from agent_comms.operations import Comms
comms = Comms(sys.argv[1])
viewer = comms.user_identity(sys.argv[1]).name
assert int(sys.argv[2]) in comms.reads.seen_sequences(viewer, comms.registry.snapshot())
page = comms.dm_display_page('owner', worktree=sys.argv[1])
comms.mark_dm_view_read('owner',worktree=sys.argv[1],through=page.newest_seq,
                        expected_display_basis=page.display_basis)
""",
            str(tmp_path),
            str(first.seq),
        ],
        env=dict(os.environ, PYTHONPATH=old_source),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert {first.seq, second.seq} <= comms.reads.seen_sequences(viewer, comms.registry.snapshot())
