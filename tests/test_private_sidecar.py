"""Private snapshot boundary negatives, durability faults and process races."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys

import pytest

from agent_comms import private_sidecar as sidecar
from agent_comms.coordination_store import IdentityConflict
from agent_comms.native_prompt_binding import _DDL, _DDL_DIGEST


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
    sidecar.create_sidecar_file(path, _DDL, _DDL_DIGEST)


def _connection(path):
    return sidecar.sidecar_connection(path, _DDL, _DDL_DIGEST)


def _insert(db, key="1"):
    return db.execute(
        "INSERT INTO prompt_bindings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            key * 32,
            1,
            "triage",
            "claim",
            None,
            None,
            "2" * 32,
            "owner",
            1,
            "3" * 32,
            1,
            "message",
            "a" * 64,
            1,
        ),
    )


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
        "CREATE TRIGGER unrelated_insert_suppressor BEFORE INSERT ON prompt_bindings "
        "BEGIN SELECT RAISE(IGNORE); END",
        "CREATE INDEX unrelated_index ON prompt_bindings(claim_id)",
        "CREATE VIEW unrelated_view AS SELECT * FROM prompt_bindings",
        "CREATE TABLE unrelated_table (x)",
        "DROP TRIGGER prompt_binding_delete_guard",
    ],
)
def test_all_schema_objects_checked_independent_of_name(path, sql):
    _create(path)
    with sqlite3.connect(path) as db:
        db.execute(sql)
    before = path.read_bytes()
    with pytest.raises(IdentityConflict, match="schema objects"):
        sidecar.verify_sidecar(path, _DDL, _DDL_DIGEST)
    with pytest.raises(IdentityConflict, match="schema objects"), _connection(path):
        pytest.fail("tampered schema reached caller")
    assert path.read_bytes() == before


def test_sqlite_only_opens_memory_and_exact_connection_is_verified(path, monkeypatch):
    connect = sidecar.sqlite3.connect
    names = []
    handles = []

    def record(name, *args, **kwargs):
        names.append(name)
        db = connect(name, *args, **kwargs)
        handles.append(db)
        return db

    monkeypatch.setattr(sidecar.sqlite3, "connect", record)
    _create(path)
    with _connection(path) as db:
        assert db is handles[-1]
        assert _insert(db).rowcount == 1
    with _connection(path) as db:
        assert db is handles[-1]
        assert db.execute("SELECT COUNT(*) FROM prompt_bindings").fetchone()[0] == 1
    assert names == [":memory:"] * 3
    with pytest.raises(sqlite3.ProgrammingError):
        db.execute("SELECT 1")


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
        pytest.raises(IdentityConflict, match="outside the snapshot protocol"),
        _connection(path) as db,
    ):
        _insert(db)
        with sqlite3.connect(path) as other:
            _insert(other, "4")
    with sqlite3.connect(path) as db:
        rows = db.execute("SELECT input_id FROM prompt_bindings").fetchall()
    assert rows == [("4" * 32,)]


def test_schema_drift_during_scope_cannot_publish(path):
    _create(path)
    before = path.read_bytes()
    with pytest.raises(IdentityConflict, match="schema objects"), _connection(path) as db:
        db.execute(
            "CREATE TRIGGER suppress BEFORE INSERT ON prompt_bindings "
            "BEGIN SELECT RAISE(IGNORE); END"
        )
        assert _insert(db).rowcount == 0
    assert path.read_bytes() == before


@pytest.mark.parametrize("position", [1, 2, 3, 4, 5])
def test_each_commit_fsync_fault_is_unknown_never_success(path, monkeypatch, position):
    _create(path)
    sync = os.fsync
    calls = []

    def fail(fd):
        calls.append(fd)
        if len(calls) == position:
            raise OSError("injected durability fault")
        sync(fd)

    monkeypatch.setattr(sidecar.os, "fsync", fail)
    with pytest.raises(sidecar.SidecarCommitUnknown), _connection(path) as db:
        _insert(db)
    assert len(calls) == position
    monkeypatch.setattr(sidecar.os, "fsync", sync)
    if position < 5:
        with pytest.raises(sidecar.SidecarCommitUnknown):
            _create(path)
        with pytest.raises(sidecar.SidecarCommitUnknown), _connection(path):
            pytest.fail("uncertain snapshot reached caller")
    else:
        # Only the intent-removal sync failed. Data+replacement were synced
        # first; this read is informational, never a permit to retry input.
        with _connection(path) as db:
            assert db.execute("SELECT COUNT(*) FROM prompt_bindings").fetchone()[0] == 1


def test_mode_drift_and_connection_attachment_are_not_admitted(path):
    _create(path)
    before = path.read_bytes()
    with pytest.raises(IdentityConflict), _connection(path) as db:
        _insert(db)
        path.chmod(0o644)
    path.chmod(0o600)
    with (
        pytest.raises(IdentityConflict, match="only its in-memory snapshot"),
        _connection(path) as db,
    ):
        db.execute("ATTACH ':memory:' AS other")
    assert path.read_bytes() == before


def test_owner_mismatch_refused(path, monkeypatch):
    _create(path)
    real_uid = os.geteuid()
    monkeypatch.setattr(sidecar.os, "geteuid", lambda: real_uid + 1)
    with pytest.raises(IdentityConflict):
        sidecar.verify_sidecar(path, _DDL, _DDL_DIGEST)


def test_wal_and_bounded_snapshot_refused(path, monkeypatch):
    _create(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    with pytest.raises(IdentityConflict, match="standalone rollback-mode"):
        sidecar.verify_sidecar(path, _DDL, _DDL_DIGEST)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
    monkeypatch.setattr(sidecar, "_MAX_SIDECAR_BYTES", 100)
    with pytest.raises(IdentityConflict, match="oversized"):
        sidecar.verify_sidecar(path, _DDL, _DDL_DIGEST)


def test_install_sync_failure_leaves_explicit_unknown_not_repaired(path, monkeypatch):
    sync = os.fsync

    def fail(fd):
        raise OSError("installation fault")

    monkeypatch.setattr(sidecar.os, "fsync", fail)
    with pytest.raises(sidecar.SidecarCommitUnknown):
        _create(path)
    assert not path.exists()
    monkeypatch.setattr(sidecar.os, "fsync", sync)
    with pytest.raises(sidecar.SidecarCommitUnknown):
        _create(path)


_WORKER = r"""
import os, signal, sys
from pathlib import Path
from agent_comms import private_sidecar as s
from agent_comms.native_prompt_binding import _DDL, _DDL_DIGEST
p = Path(sys.argv[1])
print('READY', flush=True)
assert sys.stdin.readline() == 'GO\n'
s.create_sidecar_file(p, _DDL, _DDL_DIGEST)
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
    with s.sidecar_connection(p, _DDL, _DDL_DIGEST) as db:
        db.execute('INSERT INTO prompt_bindings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ('1'*32,1,'triage','claim',None,None,'2'*32,'owner',1,'3'*32,1,'message','a'*64,1))
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
        sidecar.verify_sidecar(path, _DDL, _DDL_DIGEST)
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
