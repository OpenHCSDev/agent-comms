"""One-shot routing carry in the existing retained index/batch operation."""
from dataclasses import field, dataclass
from pathlib import Path
import os
import json
from typing import ClassVar

from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.registry_provenance import RegistryProvenance
from retained_index_cutover import RetainedIndexCutover


@dataclass(frozen=True)
class RetainedRoutingCutover(RetainedIndexCutover):
    acknowledgement: Path = field(metadata={"wire_name": 'receipt'})
    writer_script: ClassVar[str] = 'retained_routing_writer.py'
    installer_script: ClassVar[str] = 'install_retained_routing.py'

    @property
    def operation_arguments(self) -> tuple[str, ...]:
        return (str(self.acknowledgement), json.dumps(FieldCodec.record_schema(RegistryProvenance)))

    def require_source(self) -> None:
        super().require_source()
        if not self.acknowledgement.is_absolute() or self.acknowledgement.exists() or self.acknowledgement.is_symlink():
            raise RelationViolationError('Routing carry requires a fresh persistent acknowledgement path.')
        if not self.acknowledgement.parent.is_dir() or self.acknowledgement.parent.is_symlink():
            raise RelationViolationError('Routing acknowledgement parent must already be owned storage.')
        parent = self.acknowledgement.parent.stat()
        if parent.st_uid != os.geteuid() or parent.st_mode & 0o022:
            raise RelationViolationError('Routing acknowledgement storage is not owner-controlled.')
        originals = self.acknowledgement.with_name(self.acknowledgement.name + '.originals')
        if originals.exists() or originals.is_symlink():
            raise RelationViolationError('Retained recovery originals prohibit another carry attempt.')
