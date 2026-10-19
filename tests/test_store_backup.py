"""SQLite snapshots preserve committed WAL rows and restore absent resources."""
import sqlite3
from pathlib import Path

from cutover.backup import StoreBackup
from cutover.carry import CarryChain, CarryLedger, NoConversion, PendingConversion, PreparedCarries


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


def test_current_conversion_plan_only_selects_declared_transcript_store(tmp_path):
    checkout = Path(__file__).resolve().parents[1]
    chain = CarryChain.at(checkout, 'HEAD')
    applied = tuple(step.name for step in chain.steps if step.name != 'carry_external_positions')
    ledger = CarryLedger(tmp_path)
    for name in applied:
        ledger.record(name)
    destination = StoreBackup(tmp_path, tmp_path / 'route.json', tmp_path / 'backup')
    plan = PreparedCarries.at(checkout, tmp_path, destination, False)
    assert isinstance(plan, PendingConversion)
    assert [step.name for step, _ in plan.conversions] == ['carry_external_positions']
    from agent_comms.external_transcript import ExternalTranscript
    assert plan.files == (tmp_path / CarryLedger.name, ExternalTranscript.path_in(tmp_path))


def test_no_conversion_has_no_snapshot_or_recovery_work(tmp_path):
    checkout = Path(__file__).resolve().parents[1]
    chain = CarryChain.at(checkout, 'HEAD')
    ledger = CarryLedger(tmp_path)
    for step in chain.steps:
        ledger.record(step.name)
    destination = StoreBackup(tmp_path, tmp_path / 'route.json', tmp_path / 'backup')
    plan = PreparedCarries.at(checkout, tmp_path, destination, False)
    assert isinstance(plan, NoConversion)
    assert plan.data_root == tmp_path
    assert plan.capture() is plan
    def forbidden():
        raise AssertionError('NoConversion must not restore or open a route context')
    plan.restore_if_unpublished(forbidden, forbidden)
    assert plan.cleanup(False)
    assert not destination.path.exists()


def test_ledger_does_not_repeat_a_recorded_conversion(tmp_path):
    checkout = Path(__file__).resolve().parents[1]
    chain = CarryChain.at(checkout, 'HEAD')
    ledger = CarryLedger(tmp_path)
    for step in chain.steps:
        ledger.record(step.name)
    def repeated():
        raise AssertionError('a recorded conversion must not run again')
    assert ledger.apply(chain, chain.steps[-1].name, repeated).changed == 0
