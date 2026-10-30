"""Version the registry's repository-scoped head-review authority without granting it.

Version one has no reviewer selection. The new declaration's empty default preserves
that absence; this carry changes only the format version, never identities or grants.
Retire this one-shot step after the receiving installation is accepted.
"""

from typing import ClassVar

from pydantic import JsonValue

from agent_comms.field_codec import FieldCodec
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.locked_store import RootFile
from agent_comms.registry_document import RegistryDocument
from agent_comms.registry_store import RegistryStore
from agent_comms.store_files import AtomicFile


class RegistryHeadReviewers(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (RegistryStore,)

    def carry(self, *, apply: bool) -> Carried:
        path = RegistryStore.path_in(self.root)
        self.require_confined(path)
        if not path.is_file():
            return Carried(0)
        document = FieldCodec.decode(dict[str, JsonValue], FieldCodec.parse(path.read_text()))
        version = FieldCodec.record_key(RegistryDocument, 'version')
        if FieldCodec.decode(int, document[version]) != 1:
            FieldCodec.decode_document(RegistryDocument, document)
            return Carried(0)
        carried = document | {version: RegistryDocument.version}
        FieldCodec.decode_document(RegistryDocument, carried)
        if apply:
            AtomicFile(path).write_text(FieldCodec.compact(carried, allow_nan=False))
        return Carried(1, rewritten=(path.relative_to(self.root),) if apply else ())
