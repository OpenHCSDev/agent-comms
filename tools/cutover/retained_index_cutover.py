"""One-shot retained writer operation; remove after the reviewed index cutover.

This tool is outside production readers. OwnerLifecycle owns the only batch
stop/start path; this member owns the old-format writer through installation.
"""
from dataclasses import dataclass
from pathlib import Path
import os
import subprocess
import sys
from typing import ClassVar

from agent_comms.errors import RelationViolationError
from agent_comms.owner_cutover import StoppedOwnerInstallation
from agent_comms.owner_restart import OwnerRestartRequest
from agent_comms.native_package import verify_native_package
from agent_comms.wire_metadata import WireRootIdText
from checkpoint_schema import declared_schema_digest


@dataclass(frozen=True)
class RetainedIndexCutover(StoppedOwnerInstallation):
    original_python: Path
    wire_root_id: str
    native_package: Path
    writer_script: ClassVar[str] = 'retained_index_writer.py'
    installer_script: ClassVar[str] = 'install_retained_index.py'

    def failed(self, failure):
        # Reset/carry may have committed a new index. Routing inherits this
        # same disposition; neither one-shot may reinterpret it as old format.
        self.leave_stopped(failure)

    @property
    def operation_arguments(self) -> tuple[str, ...]:
        return ()

    def require_selection(self, snapshot, owners) -> None:
        audience = {thread.name for thread in OwnerRestartRequest().threads(snapshot)}
        if {thread.name for thread in owners} != audience:
            raise RelationViolationError('Index cutover requires every managed restart owner.')
        if not self.original_python.is_absolute() or not self.original_python.is_file():
            raise RelationViolationError('Original installed writer interpreter is required.')
        WireRootIdText.from_text(self.wire_root_id)
        verify_native_package(self.native_package)
        environment = dict(os.environ)
        environment.pop('PYTHONPATH', None)
        original_schema = subprocess.run([
            str(self.original_python), str(Path(__file__).with_name('checkpoint_schema.py')),
        ], env=environment, check=True, capture_output=True, text=True).stdout.strip()
        if original_schema == declared_schema_digest():
            raise RelationViolationError('Installed schemas already match; retained reset is unnecessary.')

    def after_stopped(self, lifecycle) -> None:
        # Existing central batch holds wire, and has verified every exact
        # original process exited. The old child acquires bus; its new child
        # inherits the ORIGINAL opened lock, never reentering old schema.
        environment = dict(os.environ)
        environment.pop('PYTHONPATH', None)
        subprocess.run([
            str(self.original_python), str(Path(__file__).with_name(self.writer_script)),
            str(lifecycle.root), sys.executable,
            str(Path(__file__).with_name(self.installer_script)), self.wire_root_id,
            *self.operation_arguments,
        ], env=environment, check=True)

    def bind_target_launch(self, lifecycle) -> None:
        lifecycle.pin_private_nk_launch(lifecycle.root, self.wire_root_id, self.native_package)
