"""One-shot routing carry in the existing retained index/batch operation."""
from dataclasses import dataclass
from pathlib import Path
import os
from typing import ClassVar

from agent_comms.errors import RelationViolationError
from retained_index_cutover import RetainedIndexCutover


@dataclass(frozen=True)
class RetainedRoutingCutover(RetainedIndexCutover):
    receipt: Path
    writer_script: ClassVar[str] = 'retained_routing_writer.py'
    installer_script: ClassVar[str] = 'install_retained_routing.py'

    @property
    def operation_arguments(self) -> tuple[str, ...]:
        return (str(self.receipt),)

    def require_selection(self, snapshot, owners) -> None:
        super().require_selection(snapshot, owners)
        if not self.receipt.is_absolute() or self.receipt.exists() or self.receipt.is_symlink():
            raise RelationViolationError('Routing carry requires a fresh persistent receipt path.')
        if not self.receipt.parent.is_dir() or self.receipt.parent.is_symlink():
            raise RelationViolationError('Routing receipt parent must already be owned storage.')
        parent = self.receipt.parent.stat()
        if parent.st_uid != os.geteuid() or parent.st_mode & 0o022:
            raise RelationViolationError('Routing receipt storage is not owner-controlled.')
        originals = self.receipt.with_name(self.receipt.name + '.originals')
        if originals.exists() or originals.is_symlink():
            raise RelationViolationError('Retained recovery originals prohibit another carry attempt.')
