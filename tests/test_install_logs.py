"""Install diagnostics are kept as long as the data backups are, and no longer."""

import os

from agent_comms.installer.backup import StoreBackup
from agent_comms.installer.launch import InstallLogs


def test_a_new_install_keeps_only_as_many_log_directories_as_backups(tmp_path):
    root = tmp_path / "bus"
    logs = InstallLogs(root)
    older = [tmp_path / f"{logs.prefix}{index}" for index in range(5)]
    for age, path in enumerate(older):
        path.mkdir()
        os.utime(path, (age, age))
    unrelated = tmp_path / ".bus.retired-1"
    unrelated.mkdir()

    minted = logs.minted().logs

    kept = sorted(path for path in tmp_path.iterdir() if path.name.startswith(logs.prefix))
    assert minted in kept
    assert len(kept) == StoreBackup.kept
    assert older[-1] in kept
    assert unrelated.is_dir()
