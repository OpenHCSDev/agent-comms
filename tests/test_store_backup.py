"""SQLite snapshots preserve committed WAL rows and restore absent resources."""
import os
import sqlite3
from pathlib import Path


from agent_comms.locked_store import LockedStore
from agent_comms.store_files import StoreOwner
from agent_comms.installer.backup import StoreBackup
from agent_comms.installer.carry import CarryChain, CarryLedger


def test_tree_copy_omits_owned_staging_before_stat(tmp_path, monkeypatch):
    root = tmp_path / 'source'
    root.mkdir()
    committed = root / 'bus_meta.json'
    committed.write_text('committed')
    monkeypatch.setattr(StoreOwner, 'staging_name', '.owned-{name}-{token}')
    descriptor, stage = StoreOwner.stage_file(committed)
    os.close(descriptor)
    os.link(committed, StoreOwner.rollback_file(stage))
    original = Path.lstat

    def committed_stat(path, *args, **kwargs):
        if LockedStore.is_staging(path):
            raise AssertionError('copy inspected an unpublished staging file')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'lstat', committed_stat)
    target = tmp_path / 'copy'
    StoreBackup.copy_tree(root, target)
    assert (target / committed.name).read_text() == 'committed'
    assert sorted(path.name for path in target.iterdir()) == [committed.name]


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
    ledger = root / CarryLedger.name
    with sqlite3.connect(store) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('CREATE TABLE events (body TEXT)')
        writer.execute("INSERT INTO events VALUES ('retained')")
        writer.commit()
        backup = StoreBackup.take(root, route, (store, ledger), tmp_path / 'backup')
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


def test_ledger_does_not_repeat_a_recorded_conversion(tmp_path):
    checkout = Path(__file__).resolve().parents[1]
    chain = CarryChain.at(checkout, 'HEAD')
    ledger = CarryLedger(tmp_path)
    for step in chain.steps:
        ledger.record(step.name)
    def repeated():
        raise AssertionError('a recorded conversion must not run again')
    assert ledger.apply(chain, chain.steps[-1].name, repeated).changed == 0
