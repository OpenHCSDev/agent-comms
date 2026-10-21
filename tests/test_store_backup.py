"""SQLite snapshots preserve committed WAL rows and restore absent resources."""
import sqlite3
from pathlib import Path

import pytest

from agent_comms.installer.backup import BackupName, NoBackup, StoreBackup
from agent_comms.installer.carry import CarryChain, CarryLedger


def test_tree_copy_keeps_committed_wal_rows(tmp_path):
    root = tmp_path / 'source'
    root.mkdir()
    store = root / 'transcripts.sqlite3'
    with sqlite3.connect(store) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('CREATE TABLE events (body TEXT)')
        writer.execute("INSERT INTO events VALUES ('retained')")
        writer.commit()
        target = tmp_path / 'copy'
        StoreBackup.copy_tree(root, target)
        with sqlite3.connect(target / store.name) as reader:
            assert reader.execute('SELECT body FROM events').fetchall() == [('retained',)]


def test_store_backup_restores_wal_rows_and_ledger(tmp_path):
    root = tmp_path / 'wire'
    root.mkdir()
    store = root / 'transcripts.sqlite3'
    route = tmp_path / 'route.json'
    route.write_text('original route')
    ledger = root / CarryLedger.filename
    with sqlite3.connect(store) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('CREATE TABLE events (body TEXT)')
        writer.execute("INSERT INTO events VALUES ('retained')")
        writer.commit()
        spill_dir = tmp_path / 'spill'
        backup = StoreBackup.take(root, route, (store, ledger), spill_dir)
        assert isinstance(backup, StoreBackup)
        assert backup.path.parent == spill_dir
        assert backup.original()
        writer.execute("UPDATE events SET body='changed'")
        writer.commit()
        ledger.write_text('["new-conversion"]')
    route.write_text('changed route')
    assert not backup.original()
    backup.restore()
    assert backup.original()
    assert not ledger.exists()
    assert route.read_text() == 'original route'
    with sqlite3.connect(store) as reader:
        assert reader.execute('SELECT body FROM events').fetchall() == [('retained',)]
    archived = backup.retire()
    assert not backup.path.exists()
    assert archived.exists()


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


def test_runtime_only_backup_retains_without_a_spill_directory(tmp_path):
    root = tmp_path / 'wire'
    spill_dir = tmp_path / 'missing-spill'
    backup = StoreBackup.take(root, tmp_path / 'no-route.json', (), spill_dir)

    assert isinstance(backup, NoBackup)
    assert backup.retain(spill_dir) == ()
    assert backup.original()
    backup.restore()
    assert backup.report().backup == 'none'
    assert backup.report().bytes == 0
    assert not spill_dir.exists()
    assert not root.exists()


def test_ledger_does_not_repeat_a_recorded_conversion(tmp_path):
    checkout = Path(__file__).resolve().parents[1]
    chain = CarryChain.at(checkout)
    ledger = CarryLedger(tmp_path)
    for step in chain.steps:
        ledger.record(step.name())
    def repeated():
        raise AssertionError('a recorded conversion must not run again')
    assert ledger.apply(chain.steps[-1].name(), repeated).changed == 0
