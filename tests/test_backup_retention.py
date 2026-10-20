"""Install archives live on the configured disk, with a bounded completed tail."""
from pathlib import Path

from cutover.backup import Backup
from upgrade_bus import Stack


def test_seven_backups_keep_five_and_current_rollback(tmp_path, monkeypatch):
    stack = Stack(Path(__file__).resolve().parents[1], tmp_path / 'home')
    disk = stack.home / 'mount/hdd'
    disk.mkdir(parents=True)
    mounted = Path.is_mount
    monkeypatch.setattr(Path, 'is_mount', lambda path: path == disk or mounted(path))
    root = tmp_path / 'wire'
    root.mkdir()
    payload = root / 'retained.txt'
    payload.write_text('original')
    route = tmp_path / 'active-route.json'
    route.write_text('original route')
    incomplete = stack.backup_root / 'wire.upgrade-interrupted'
    incomplete.mkdir(parents=True)
    (incomplete / 'bus.tar').write_text('incomplete; preserve for diagnosis')

    backups = [Backup.take(root, route, (root,), stack) for _ in range(7)]
    completed = {path for path in stack.backup_root.iterdir() if (path / 'files.json').is_file()}
    assert len(completed) == 5
    assert completed == {backup.path for backup in backups[-5:]}
    assert all(backup.path.parent == stack.backup_root for backup in backups)
    assert incomplete.exists() and incomplete not in completed
    rollback = backups[-1]
    assert rollback.path in completed
    pruned = sum(not backup.path.exists() for backup in backups)
    assert pruned == 2

    payload.write_text('after install')
    rollback.restore()
    assert payload.read_text() == 'original'
    assert rollback.original()
    print(f'completed={len(completed)} pruned={pruned} rollback_retained=True incomplete_retained=True')
