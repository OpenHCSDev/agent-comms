"""Private snapshot boundary negatives, durability faults and process races."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys

import pytest

import agent_comms.private_sidecar as sidecar
from agent_comms.native_input_record import TriageNativeExecution, UnrecordedNativeColumn
from agent_comms.coordination_errors import IdentityConflict
from agent_comms.native_prompt_binding import PromptBinding


@pytest.fixture
def path(tmp_path):
    if os.name != "posix":
        pytest.skip("snapshot boundary requires POSIX descriptors")
    # Platforms may expose their temp root through /var -> /private/var.
    # Positive fixtures use a real lexical root; redirect negatives add aliases.
    tmp_path = tmp_path.resolve()
    tmp_path.chmod(0o700)
    return tmp_path / "binding.sqlite3"


def _create(path):
    sidecar.create_sidecar_file(path, PromptBinding)


def _connection(path):
    return sidecar.sidecar_connection(path, PromptBinding)


def _insert(db, key="1"):
    return PromptBinding(
        input_id=key * 32,
        stage=TriageNativeExecution,
        execution_id=UnrecordedNativeColumn(),
        attempt_ordinal=UnrecordedNativeColumn(),
        owner_lookup="2" * 32,
        owner_generation=1,
        wire_root_id="3" * 32,
        expected_prompt_digest="a" * 64,
        bound_at_ms=1,
    ).insert(db)


@pytest.mark.parametrize(
    "kind", ["symlink", "dangling", "hardlink", "fifo", "directory", "public", "empty"]
)
def test_invalid_leaf_never_mutates_external_target(path, kind):
    external = path.with_name("external.sqlite3")
    external.touch(mode=0o600)
    if kind == "symlink":
        path.symlink_to(external)
    elif kind == "dangling":
        path.symlink_to(path.with_name("missing"))
    elif kind == "hardlink":
        os.link(external, path)
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    elif kind == "directory":
        path.mkdir(mode=0o700)
    elif kind == "public":
        path.touch(mode=0o644)
    else:
        path.touch(mode=0o600)
    with pytest.raises((IdentityConflict, OSError)):
        _create(path)
    assert external.read_bytes() == b""
    assert not path.with_name("missing").exists()


@pytest.mark.parametrize("kind", ["file", "dangling", "directory", "fifo"])
def test_pending_intent_is_present_without_following_or_opening_it(path, kind):
    _create(path)
    before = path.read_bytes()
    intent = sidecar.StoreOwner.snapshot_intent(path)
    if kind == "file":
        intent.write_text("unresolved commit")
    elif kind == "dangling":
        intent.symlink_to(path.with_name("missing-intent-target"))
    elif kind == "directory":
        intent.mkdir()
    else:
        os.mkfifo(intent, 0o600)
    identity = intent.lstat()
    with pytest.raises(sidecar.SidecarCommitUnknown):
        with _connection(path):
            pytest.fail("a pending commit reached the database caller")
    assert path.read_bytes() == before
    assert intent.lstat() == identity
    assert not path.with_name("missing-intent-target").exists()


def test_symlink_ancestor_refused(path):
    directory = path.parent / "real"
    directory.mkdir(mode=0o700)
    link = path.parent / "alias"
    link.symlink_to(directory, target_is_directory=True)
    with pytest.raises((IdentityConflict, OSError)):
        _create(link / path.name)
    assert not (directory / path.name).exists()


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TRIGGER unrelated_insert_suppressor BEFORE INSERT ON prompt_binding "
        "BEGIN SELECT RAISE(IGNORE); END",
        "CREATE INDEX unrelated_index ON prompt_binding(owner_lookup)",
        "CREATE VIEW unrelated_view AS SELECT * FROM prompt_binding",
        "CREATE TABLE unrelated_table (x)",
        *(f'DROP TRIGGER "{name}"' for name in PromptBinding.triggers()),
    ],
)
def test_all_schema_objects_checked_independent_of_name(path, sql):
    _create(path)
    with sqlite3.connect(path) as db:
        db.execute(sql)
    before = path.read_bytes()
    with pytest.raises(IdentityConflict), _connection(path):
        pass
    with pytest.raises(IdentityConflict), _connection(path):
        pytest.fail("tampered schema reached caller")
    assert path.read_bytes() == before


@pytest.mark.parametrize("replacement", ["file", "symlink", "hardlink", "lock"])
def test_mid_scope_replacement_refuses_before_publish(path, replacement):
    _create(path)
    before = path.read_bytes()
    external = path.with_name("external.sqlite3")
    external.write_bytes(before)
    external.chmod(0o600)
    with pytest.raises(IdentityConflict), _connection(path) as db:
        _insert(db)
        if replacement == "lock":
            lock = path.with_name(f".{path.name}.snapshot-lock")
            lock.rename(lock.with_suffix(".old"))
            lock.touch(mode=0o600)
        else:
            path.rename(path.with_suffix(".old"))
            if replacement == "file":
                path.write_bytes(before)
                path.chmod(0o600)
            elif replacement == "symlink":
                path.symlink_to(external)
            else:
                os.link(external, path)
    assert external.read_bytes() == before
    assert not path.with_name(f".{path.name}.pending").exists()


def test_mutation_of_loaded_inode_refuses_before_publish(path):
    _create(path)
    with (
        pytest.raises(IdentityConflict),
        _connection(path) as db,
    ):
        _insert(db)
        with sqlite3.connect(path) as other:
            _insert(other, "4")
    with sqlite3.connect(path) as db:
        rows = db.execute("SELECT input_id FROM prompt_binding").fetchall()
    assert rows == [("4" * 32,)]


def test_schema_drift_during_scope_cannot_publish(path):
    _create(path)
    before = path.read_bytes()
    with pytest.raises(IdentityConflict), _connection(path) as db:
        db.execute(
            "CREATE TRIGGER suppress BEFORE INSERT ON prompt_binding "
            "BEGIN SELECT RAISE(IGNORE); END"
        )
        assert _insert(db).rowcount == 0
    assert path.read_bytes() == before


def test_mode_drift_and_connection_attachment_are_not_admitted(path):
    _create(path)
    before = path.read_bytes()
    with pytest.raises(IdentityConflict), _connection(path) as db:
        _insert(db)
        path.chmod(0o644)
    path.chmod(0o600)
    with (
        pytest.raises(IdentityConflict),
        _connection(path) as db,
    ):
        db.execute("ATTACH ':memory:' AS other")
    assert path.read_bytes() == before


_WORKER = r"""
import os, signal, sys
from pathlib import Path
import agent_comms.private_sidecar as s
from agent_comms.native_input_record import TriageNativeExecution, UnrecordedNativeColumn
from agent_comms.native_prompt_binding import PromptBinding
p = Path(sys.argv[1])
print('READY', flush=True)
assert sys.stdin.readline() == 'GO\n'
s.create_sidecar_file(p, PromptBinding)
if len(sys.argv) > 2:
    sync = os.fsync
    count = 0
    def crash(fd):
        global count
        sync(fd)
        count += 1
        if count == int(sys.argv[2]):
            os.kill(os.getpid(), signal.SIGKILL)
    s.os.fsync = crash
    with s.sidecar_connection(p, PromptBinding) as db:
        PromptBinding(input_id='1'*32,stage=TriageNativeExecution,
            execution_id=UnrecordedNativeColumn(),attempt_ordinal=UnrecordedNativeColumn(),owner_lookup='2'*32,
            owner_generation=1,wire_root_id='3'*32,
            expected_prompt_digest='a'*64,
            bound_at_ms=1).insert(db)
print('OK', flush=True)
"""


def _worker(path, *extra):
    return subprocess.Popen(
        [sys.executable, "-u", "-c", _WORKER, str(path), *extra],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_two_process_installers_serialize_one_exact_schema(path):
    children = [_worker(path), _worker(path)]
    try:
        for child in children:
            assert child.stdout.readline() == "READY\n"
        for child in children:
            child.stdin.write("GO\n")
            child.stdin.flush()
        for child in children:
            stdout, stderr = child.communicate(timeout=10)
            assert child.returncode == 0, stderr
            assert stdout == "OK\n"
        with _connection(path):
            pass
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)


@pytest.mark.parametrize("cut", [3, 4])
def test_process_crash_before_or_after_replacement_leaves_unreplayable_intent(path, cut):
    _create(path)
    child = _worker(path, str(cut))
    try:
        assert child.stdout.readline() == "READY\n"
        stdout, stderr = child.communicate("GO\n", timeout=10)
        assert child.returncode < 0, stderr
        assert "OK" not in stdout
        with pytest.raises(sidecar.SidecarCommitUnknown):
            _create(path)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)
