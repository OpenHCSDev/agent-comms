"""Explicit existing installer under the fresh private root's file lock."""
import fcntl
from pathlib import Path
from agent_comms.private_bus_checkpoint import install_private_bus_checkpoint
from agent_comms.store_files import _store_lock_file
from agent_comms.wire_log import WireLog
p=Path('/var/tmp/agent-scratch-p156/bus.jsonl')
with _store_lock_file(p) as lock:
    fcntl.flock(lock,fcntl.LOCK_EX)
    witness=install_private_bus_checkpoint(WireLog(p),_bus_locked=True)
    print(witness.offset,witness.through_seq,flush=True)
