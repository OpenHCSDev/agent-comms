from __future__ import annotations

import sys

import agent_comms
from agent_comms.installer.interpreter_operation import RuntimeProbe, TargetProbe
from agent_comms.native_package import (
    VerifiedNativePackage,
    pinned_native_package,
)
from agent_comms.package_stamp import STAMP
from agent_comms.unit_instructions import UnitInstructions


class PrepareNativeProbe(TargetProbe, RuntimeProbe[None]):
    def run(self) -> None:
        VerifiedNativePackage.qualify(pinned_native_package())
        UnitInstructions.prepare()


class PrepareProducerDigestsProbe(TargetProbe, RuntimeProbe[None]):
    """Read the producer commitments built into this runtime's wheel."""

    def run(self) -> None:
        digests = STAMP.producer_digests({})
        print(
            f"install: {len(digests)} stamped producer digests from {agent_comms.__file__}",
            file=sys.stderr,
        )
