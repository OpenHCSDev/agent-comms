"""One-shot routing carry in the existing retained index/batch operation."""
from dataclasses import dataclass
from pathlib import Path
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
        if not self.receipt.is_absolute() or self.receipt.exists():
            raise RelationViolationError('Routing carry requires a fresh persistent receipt path.')
        if not self.receipt.parent.is_dir() or self.receipt.parent.is_symlink():
            raise RelationViolationError('Routing receipt parent must already be owned storage.')
