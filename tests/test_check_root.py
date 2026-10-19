"""Selected check history is small, non-executable and scoped to the command."""
from dataclasses import replace
import json
from pathlib import Path
import shutil
import sys

import pytest

from agent_comms.check_root import CheckRoot
from agent_comms.cli import main
from agent_comms.comms import Comms
from agent_comms.coordination_errors import RecoveryBlocked
from agent_comms.fresh_private_session import create_fresh_private_session
from agent_comms.input_disposition import InputDispositions
from agent_comms.native_entries import NativeEntry
from agent_comms.thread_status import RunningThreadStatus, StoppedThreadStatus


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    c = Comms(root)
    c.messaging.initialize_private_initial_protocol()
    for name in ("A", "B", "other"):
        c.registry.declare(c.registry.new_thread(name, frozenset({"comms"}), str(tmp_path)),
                           StoppedThreadStatus())
    c.messaging.send("A", "B", "old history is not a new input")
    c.registry.register(c.registry.require("other"), RunningThreadStatus())
    c.registry.rename("other", "unselected")
    return c


def test_only_selected_registry_and_history_without_old_pending_input(source, tmp_path):
    root = tmp_path / "selected"
    with CheckRoot(source, ("A", "B"), root).open() as c:
        assert set(c.registry.snapshot().threads) == {"A", "B"}
        assert "other" not in c.registry.snapshot().aliases
        assert not c.bus.inbox("B")
        assert all(not t.process_alive and not t.executing for t in c.registry.snapshot().threads.values())
        with c.bus.log.opened_wire_snapshot() as history:
            assert [m.body for m in history.newest_messages()] == ["old history is not a new input"]
        assert sum(p.stat().st_size for p in root.rglob("*") if p.is_file()) < 2 * 1024 * 1024
    assert not root.exists()


def test_wrapped_nonzero_exit_removes_root_and_is_the_cli_exit(source, tmp_path, capsys):
    root = tmp_path / "command"
    status = main(["--root", str(source.root), "check-root", "--threads", "B", "--into", str(root),
                   "--", sys.executable, "-c",
                   "import pathlib,sys; assert pathlib.Path(sys.argv[1]).is_dir(); sys.exit(7)", "{root}"])
    assert status == 7
    assert json.loads(capsys.readouterr().out)["return_code"] == 7
    assert not root.exists()


def test_python_failure_removes_root(source, tmp_path):
    root = tmp_path / "failed"
    with pytest.raises(RuntimeError, match="failed check"):
        with CheckRoot(source, ("B",), root).open():
            raise RuntimeError("failed check")
    assert not root.exists()


def test_existing_destination_and_source_directory_are_never_removed(source, tmp_path):
    with pytest.raises(FileExistsError):
        with CheckRoot(source, ("B",), tmp_path).open():
            pytest.fail("existing directory was entered")
    assert source.root.is_dir()
    with pytest.raises(ValueError, match="inside its source"):
        with CheckRoot(source, ("B",), source.root / "nested").open():
            pytest.fail("source directory was entered")
    assert source.registry.require("B").name == "B"


def test_new_uncertain_input_is_kept_instead_of_erased(source, tmp_path):
    root = tmp_path / "uncertain"
    with pytest.raises(RecoveryBlocked, match="uncertain work kept"):
        with CheckRoot(source, ("B",), root).open() as c:
            InputDispositions(c.root).record("synthetic-unknown", seq=None, owner="B",
                claim_scope=1, target="B", text="construction-only uncertainty fixture")
    assert not root.exists()
    archive, = tmp_path.glob("uncertain.unknown-*")
    assert InputDispositions(archive).lookup("synthetic-unknown").unresolved
    assert list((archive / "native-sessions").rglob("*.jsonl"))
    # No provider/tool was run for this synthetic row.
    shutil.rmtree(archive)


def test_native_window_uses_existing_import_and_route_readers(source, tmp_path, monkeypatch):
    session = create_fresh_private_session(tmp_path / "original-session", worktree=tmp_path)
    stamp = "2026-10-19T14:00:00Z"
    with session.path.open("a") as output:
        for i in range(20):
            record = {"type": "message", "id": f"entry-{i}", "parentId": None if i == 0 else f"entry-{i-1}",
                      "timestamp": stamp, "message": {"role": "user", "content": f"history {i}", "timestamp": 1}}
            output.write(json.dumps(record) + "\n")
    source.registry.register(replace(source.registry.require("B"), session_file=session.session_file), StoppedThreadStatus())
    monkeypatch.setattr(CheckRoot, "max_records", 3)
    original = session.path.read_bytes()
    root = tmp_path / "native-window"
    with CheckRoot(source, ("B",), root).open() as c:
        copied = Path(c.registry.require("B").session_file)
        rows = [NativeEntry.read(line) for line in copied.read_bytes().splitlines()]
        assert len(rows) == 4
        assert rows[0].id != session.session_id
        assert [entry.message.text for entry in rows[1:]] == ["history 17", "history 18", "history 19"]
        assert rows[1].parent_id is None
        assert rows[2].parent_id == rows[1].id
        assert session.path.read_bytes() == original
    assert not root.exists()
