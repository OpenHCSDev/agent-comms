"""One-time cutover: rebuild the private bus checkpoint with the reply index.

The checkpoint owner's installer builds the whole index, ``replies`` included,
from a verified scan of the bus. Run it with the code that declares
``Replies`` while nothing else holds the bus (fleet stopped, every Toad
closed). The old sidecar and marker move into BACKUP_DIR first.

Usage: reply_index.py ROOT BACKUP_DIR
"""

import shutil
import sys
from dataclasses import replace
from pathlib import Path

from agent_comms.child_process import Platform
from agent_comms.private_bus_checkpoint import install_private_bus_checkpoint
from agent_comms.store_files import _store_lock_file
from agent_comms.wire_log import WireLog


def rebuild(root: Path, backup: Path) -> None:
    bus = WireLog(root / "bus.jsonl")
    sidecar = bus.path.with_name("private_bus_checkpoint.sqlite3")
    platform = Platform.current()
    # The ordinary bus lock verifies the old sidecar against the new schema
    # and refuses it; this takes the same lock without that verification.
    with _store_lock_file(bus.path) as lock_file:
        platform.acquire_store_lock(lock_file.fileno(), shared=False, blocking=False)
        try:
            backup.mkdir(parents=True, exist_ok=False, mode=0o700)
            shutil.copy2(bus.metadata_path, backup / bus.metadata_path.name)
            marker = bus.read_metadata_unlocked()
            sidecar.rename(backup / sidecar.name)
            bus.write_metadata_unlocked(replace(marker, checkpoint_version=None, checkpoint_seal=None))
            install_private_bus_checkpoint(bus, _bus_locked=True)
        finally:
            platform.release_store_lock(lock_file.fileno())


if __name__ == "__main__":
    root, backup = Path(sys.argv[1]), Path(sys.argv[2])
    rebuild(root, backup)
    with WireLog(root / "bus.jsonl").certified_read() as source:
        print("certified through", source.committed_sequence(), "; backup", backup)
