"""One-use durable task carry member of the existing stopped-owner operation.

No runtime reader imports this tool. The enclosing publication owner controls
the only stop/start, route, package and runtime-reset operations.
"""
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys

from agent_comms.errors import RelationViolationError
from agent_comms.owner_cutover import StoppedOwnerInstallation


@dataclass(frozen=True)
class RetainedTaskSourceCarry(StoppedOwnerInstallation):
    original_python: Path
    wire_root: Path
    wire_root_id: str
    receipt: Path

    def failed(self, failure):
        # Standalone task carry must preserve any committed target records.
        # Enclosing publishers use their own complete-operation recovery proof.
        self.leave_stopped(failure)

    def require_selection(self, snapshot, owners):
        live = {thread.incarnation for thread in snapshot.threads.values()
                if thread.role.executable and thread.process_alive}
        if live != {thread.incarnation for thread in owners}:
            raise RelationViolationError('Task carry requires the whole live owner audience.')
        if not self.original_python.is_absolute() or not self.original_python.is_file():
            raise RelationViolationError('Original installed decoder is required.')
        if not self.receipt.is_absolute() or self.receipt.exists() or self.receipt.is_symlink():
            raise RelationViolationError('Task carry receipt must be fresh persistent storage.')
        parent = self.receipt.parent.stat()
        if self.receipt.parent.is_symlink() or parent.st_uid != os.geteuid() or parent.st_mode & 0o022:
            raise RelationViolationError('Task carry receipt parent is not owner-controlled.')
        originals = self.receipt.with_name(self.receipt.name + '.originals')
        if originals.exists() or originals.is_symlink():
            raise RelationViolationError('Existing preimages prohibit another carry attempt.')
        environment = dict(os.environ)
        environment.pop('PYTHONPATH', None)
        # Admission occurs BEFORE any owner fence/signal. The same original
        # writer reads again under retained custody after the whole batch stops.
        subprocess.run([
            self.original_python, Path(__file__).with_name('read_original_task_preimage.py'),
            self.wire_root, self.wire_root_id,
        ], env=environment, stdout=subprocess.DEVNULL, check=True)

    def after_stopped(self, lifecycle):
        if lifecycle.root.resolve(strict=True) != self.wire_root.resolve(strict=True):
            raise RelationViolationError('Stopped task carry belongs to another root.')
        environment = dict(os.environ)
        environment.pop('PYTHONPATH', None)
        subprocess.run([
            str(self.original_python), str(Path(__file__).with_name('retained_task_writer.py')),
            str(lifecycle.root), sys.executable,
            str(Path(__file__).with_name('install_retained_task_source.py')),
            self.wire_root_id, str(self.receipt),
        ], env=environment, check=True)
