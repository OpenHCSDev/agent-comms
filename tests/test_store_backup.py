"""SQLite snapshots preserve committed WAL rows and restore absent resources."""
import multiprocessing
import os
import sqlite3
from contextlib import closing

import pytest

from agent_comms.installer.backup import BackupName, StoreBackup
from agent_comms.coordination_database import (
    CoordinationStore, InvalidStore, MissingStore, ReadableStore,
)
from agent_comms.coordination_errors import CoordinationReadUnavailable
from agent_comms.coordination_tables.refusals import SendRefusal
from agent_comms.external_transcript import ExternalTranscript
from agent_comms.locked_store import SQLiteFile, StoreBusy


def _try_external_writer(path, result):
    """A fresh process checks the OS lock, outside SQLite's process-local bookkeeping."""
    try:
        with CoordinationReadUnavailable.checking():
            with closing(SQLiteFile.writer(path, StoreBusy(), isolation_level=None)) as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("ROLLBACK")
    except CoordinationReadUnavailable:
        result.send(False)
    else:
        result.send(True)
    finally:
        result.close()


def _external_writer_acquires(path):
    context = multiprocessing.get_context("spawn")
    read, write = context.Pipe(duplex=False)
    process = context.Process(target=_try_external_writer, args=(path, write))
    process.start()
    write.close()
    try:
        assert read.poll(10), "The external lock witness did not answer"
        acquired = read.recv()
    finally:
        read.close()
        process.join(10)
        if process.is_alive():
            process.kill()
            process.join()
    assert process.exitcode == 0
    return acquired


@pytest.mark.parametrize("inspect_file", (CoordinationStore.observed, CoordinationStore.digest),
                         ids=("coordinator", "backup"))
def test_inspection_preserves_another_connections_process_locks(tmp_path, inspect_file):
    path = tmp_path / "locked.sqlite3"
    with closing(sqlite3.connect(path, isolation_level=None)) as writer:
        writer.execute("CREATE TABLE records (body TEXT)")
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO records VALUES ('not yet committed')")
        inspect_file(path)
        assert not _external_writer_acquires(path)
        writer.execute("ROLLBACK")
    assert _external_writer_acquires(path)


def test_coordinator_observation_accepts_wal_without_changing_committed_rows(tmp_path):
    path = CoordinationStore.path_in(tmp_path)
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        SendRefusal.create(writer)
        SendRefusal.insert_values(
            writer, sender="sender", target="recipient", reason="committed", refused_at_ms=1,
        )
        writer.commit()
        SendRefusal.insert_values(
            writer, sender="sender", target="recipient", reason="uncommitted", refused_at_ms=2,
        )
        observed = CoordinationStore.observed(path)
        assert observed == ReadableStore(path)
        assert [row.reason for row in SendRefusal.recent(tmp_path)] == ["committed"]
        with closing(SQLiteFile.read_snapshot(path)) as reader:
            with pytest.raises(sqlite3.OperationalError):
                reader.execute(f"DELETE FROM {SendRefusal.declared_name}")
        assert len(SendRefusal.select(writer)) == 2
        writer.rollback()


def test_observation_classifies_missing_foreign_and_busy_files_without_inventing_absence(tmp_path):
    path = tmp_path / "source"
    assert CoordinationStore.observed(path) == MissingStore()
    assert not path.exists()
    path.write_text("A foreign document")
    assert isinstance(CoordinationStore.observed(path), InvalidStore)
    assert path.read_text() == "A foreign document"
    path.unlink()
    with closing(sqlite3.connect(path, isolation_level=None)) as writer:
        writer.execute("CREATE TABLE records (body TEXT)")
        writer.execute("BEGIN EXCLUSIVE")
        with pytest.raises(CoordinationReadUnavailable):
            CoordinationStore.observed(path)
        writer.execute("ROLLBACK")


def test_tree_copy_keeps_committed_wal_rows(tmp_path):
    root = tmp_path / 'source'
    root.mkdir()
    store = ExternalTranscript.path_in(root)
    with sqlite3.connect(store) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('CREATE TABLE events (body TEXT)')
        writer.execute("INSERT INTO events VALUES ('retained')")
        writer.commit()
        writer.execute("INSERT INTO events VALUES ('uncommitted')")
        store.chmod(0o640)
        os.utime(store, ns=(1_600_000_000_000_000_000, 1_600_000_001_000_000_000))
        original = store.stat()
        target = tmp_path / 'copy'
        StoreBackup.copy_tree(root, target)
        assert {path.name for path in target.iterdir()} == {store.name}
        copied = (target / store.name).stat()
        assert copied.st_mode == original.st_mode
        assert copied.st_mtime_ns == original.st_mtime_ns
        with sqlite3.connect(target / store.name) as reader:
            assert reader.execute('SELECT body FROM events').fetchall() == [('retained',)]


def test_store_backup_restores_wal_rows_and_removes_new_files(tmp_path):
    root = tmp_path / 'wire'
    root.mkdir()
    store = ExternalTranscript.path_in(root)
    route = tmp_path / 'route.json'
    route.touch(mode=0o600)  # The route's owner writes it private.
    route.write_text('original route')
    created = root / 'created.json'
    with sqlite3.connect(store) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('CREATE TABLE events (body TEXT)')
        writer.execute("INSERT INTO events VALUES ('retained')")
        writer.commit()
        spill_dir = tmp_path / 'spill'
        backup = StoreBackup.take(root, route, (store, created), spill_dir)
        assert isinstance(backup, StoreBackup)
        assert backup.path.parent == spill_dir
        assert backup.spill_directory(spill_dir) == backup.path
        assert backup.original()
        writer.execute("UPDATE events SET body='changed'")
        writer.commit()
        created.write_text('["new-conversion"]')
    route.write_text('changed route')
    assert not backup.original()
    backup.restore()
    assert backup.stores_original()
    assert not backup.restoring.exists()
    assert not created.exists()
    assert route.read_text() == 'changed route'
    with sqlite3.connect(store) as reader:
        assert reader.execute('SELECT body FROM events').fetchall() == [('retained',)]
    archived = backup.retire()
    assert not backup.path.exists()
    assert archived.exists()


def test_store_backup_restores_a_file_whose_directory_a_carry_deleted(tmp_path):
    """Install 24 died restoring a ledger file whose directory install 22's carry had
    removed; restoring the file restores its directory."""
    root = tmp_path / 'wire'
    saved = root / 'imported_sessions' / 'retained.jsonl'
    saved.parent.mkdir(parents=True)
    saved.write_text('{"retained": true}\n')
    route = tmp_path / 'route.json'
    backup = StoreBackup.take(root, route, (saved,), tmp_path / 'spill')
    saved.unlink()
    saved.parent.rmdir()
    backup.restore()
    assert saved.read_text() == '{"retained": true}\n'
    assert backup.stores_original()
    assert not backup.restoring.exists()


@pytest.mark.parametrize('root_name', ('ROOT', 'wire%[x]+.{}'))
def test_backup_name_recognizes_only_its_own_complete_names(tmp_path, root_name):
    root = tmp_path / root_name
    name = BackupName.now(root)
    foreign = (BackupName.now(tmp_path / 'other').value,
               name.value + '.restored-123', name.value + '.retired-123',
               name.value + '0', name.value[:-1])
    assert tuple(BackupName.matching(root, (name.value, *foreign))) == (name,)
    assert tuple(BackupName.matching(root, foreign)) == ()


def test_backup_retention_keeps_two_newest_and_other_roots(tmp_path):
    root = tmp_path / 'ROOT'
    spill_dir = tmp_path / 'spill'
    spill_dir.mkdir()
    backups = tuple(spill_dir / f'ROOT.upgrade-20260101T00000{n}' for n in range(4))
    for backup in backups:
        backup.mkdir()
        (backup / 'saved').write_text('retained store')
    unrelated = spill_dir / 'unrelated'
    other_root = spill_dir / 'OTHER.upgrade-20260101T000000'
    retired_run = spill_dir / 'ROOT.retired-20260101T000000'
    wrong_width = spill_dir / 'ROOT.upgrade-2026011T000000'
    foreign_directories = (unrelated, other_root, retired_run, wrong_width)
    for directory in foreign_directories:
        directory.mkdir()
        (directory / 'saved').write_text('untouched')
    ordinary_file = spill_dir / 'ROOT.upgrade-20260101T000009'
    ordinary_file.write_text('untouched')

    backup = StoreBackup(root, tmp_path / 'route.json', backups[-1])
    assert backup.retain(spill_dir) == backups[:2]
    assert not any(path.exists() for path in backups[:2])
    assert all((path / 'saved').read_text() == 'retained store' for path in backups[2:])
    assert all((path / 'saved').read_text() == 'untouched'
               for path in foreign_directories)
    assert ordinary_file.read_text() == 'untouched'
    assert backup.retain(spill_dir) == ()


def test_backup_retention_preserves_restored_archives(tmp_path):
    root = tmp_path / 'ROOT'
    spill_dir = tmp_path / 'spill'
    spill_dir.mkdir()
    backups = tuple(spill_dir / f'ROOT.upgrade-20260101T00000{n}' for n in range(3))
    for backup in backups:
        backup.mkdir()
    archive = spill_dir / f'{backups[0].name}.restored-123456789'
    archive.mkdir()
    (archive / 'saved').write_text('restored evidence')

    backup = StoreBackup(root, tmp_path / 'route.json', backups[-1])
    assert backup.retain(spill_dir) == backups[:1]
    assert not backups[0].exists()
    assert all(path.is_dir() for path in backups[1:])
    assert (archive / 'saved').read_text() == 'restored evidence'


@pytest.mark.parametrize('had_route', (False, True))
def test_route_only_backup_preserves_a_concurrent_change(tmp_path, had_route):
    root = tmp_path / 'wire'
    root.mkdir()
    route = tmp_path / 'route.json'
    if had_route:
        route.touch(mode=0o600)  # The route's owner writes it private.
        route.write_text('original route')
    spill_dir = tmp_path / 'spill'
    backup = StoreBackup.take(root, route, (), spill_dir)
    assert backup.original()

    route.write_text('changed route')
    assert not backup.original()
    backup.restore()
    assert backup.stores_original()
    assert not backup.original()
    assert route.read_text() == 'changed route'

