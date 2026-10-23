from __future__ import annotations

import sys

import agent_comms
from agent_comms.field_codec import FieldCodec
from agent_comms.installer.interpreter_operation import RuntimeProbe


class PrepareNativeProbe(RuntimeProbe[None]):
    def run(self) -> None:
        from agent_comms.native_package import VerifiedNativePackage, pinned_native_package
        from agent_comms.unit_instructions import UnitInstructions

        VerifiedNativePackage.qualify(pinned_native_package())
        UnitInstructions.prepare()


class PrepareProducerDigestsProbe(RuntimeProbe[None]):
    """Only the staged runtime's installer publishes its producer declarations."""

    def run(self) -> None:
        from agent_comms.store_files import AtomicFile
        from agent_comms.typed_table import BuiltDigests, BuildProducerDigests, ProducedTable

        rows = ProducedTable.members_with(ProducedTable)
        built = BuiltDigests({row.declared_name: row.producer_digest() for row in rows})
        destination = BuildProducerDigests.current().path
        print(f"install: producer digests from {agent_comms.__file__}", file=sys.stderr)
        AtomicFile(destination).write_text(
            FieldCodec.compact(FieldCodec.encode(built), sort_keys=True) + "\n"
        )
