from __future__ import annotations

from agent_comms.installer.interpreter_operation import RuntimeProbe


class PrepareNativeProbe(RuntimeProbe[None]):
    def run(self) -> None:
        from agent_comms.native_package import VerifiedNativePackage, pinned_native_package
        from agent_comms.unit_instructions import UnitInstructions

        VerifiedNativePackage.qualify(pinned_native_package())
        UnitInstructions.prepare()

