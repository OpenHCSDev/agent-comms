"""Runtime reset preserves old bytes, never decodes or replays old attempts."""
from agent_comms.locked_store import RuntimeFile
from agent_comms.restart_queue import RestartQueue
from agent_comms.runtime_info import RuntimeInfoStore
from cutover.backup import StoreBackup


def test_reset_archives_uncertain_restart_and_backup_restores_it(tmp_path):
    root = tmp_path / 'bus'
    queue = RestartQueue.at(root)
    uncertain = queue.directory / 'settled' / 'owner' / 'uncertain.json'
    uncertain.parent.mkdir(parents=True)
    old = b'{"state":{"kind":"uncertain"},"old_runtime_field":"must not decode"}'
    uncertain.write_bytes(old)
    observation = RuntimeInfoStore.path_in(root)
    observation.write_bytes(b'old observation')
    original = (uncertain, observation)
    backup = StoreBackup.take(root, tmp_path / 'no-route', original, tmp_path / 'backup')
    moved = RuntimeFile.reset(root, backup.path / 'runtime-before')
    assert 'owner-restart-queue' in moved
    assert not queue.directory.exists()
    assert (backup.path / 'runtime-before' / uncertain.relative_to(root)).read_bytes() == old
    backup.restore()
    assert uncertain.read_bytes() == old
    assert observation.read_bytes() == b'old observation'
    assert backup.original()


def test_reset_never_touches_input_database_or_sessions(tmp_path):
    root = tmp_path / 'bus'
    root.mkdir()
    inputs = root / 'coordination.sqlite3'
    session = root / 'native-sessions' / 'session.jsonl'
    session.parent.mkdir()
    inputs.write_bytes(b'uncertain input bytes')
    session.write_bytes(b'original session bytes')
    assert RuntimeFile.reset(root, tmp_path / 'backup') == ()
    assert inputs.read_bytes() == b'uncertain input bytes'
    assert session.read_bytes() == b'original session bytes'
