from __future__ import annotations

import sys

import agent_comms
from agent_comms.installer.interpreter_operation import RuntimeProbe


class PrepareNativeProbe(RuntimeProbe[None]):
    def run(self) -> None:
        from agent_comms.native_package import VerifiedNativePackage, pinned_native_package
        from agent_comms.unit_instructions import UnitInstructions

        VerifiedNativePackage.qualify(pinned_native_package())
        UnitInstructions.prepare()


class PrepareProducerDigestsProbe(RuntimeProbe[None]):
    """Read the producer commitments built into this runtime's wheel."""

    def run(self) -> None:
        from agent_comms.package_stamp import STAMP

        digests = STAMP.producer_digests({})
        print(
            f"install: {len(digests)} stamped producer digests from {agent_comms.__file__}",
            file=sys.stderr,
        )
